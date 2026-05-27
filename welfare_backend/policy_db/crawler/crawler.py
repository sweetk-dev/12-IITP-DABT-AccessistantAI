# crawler/crawler.py
# 정기 크롤러 메인 오케스트레이션 — 매월 2일·16일 실행 권장.
#
# 흐름:
#   1) crawl_targets.json 312개 타겟 로드
#   2) 각 타겟별 change_detection_method 에 따라 detectors.DETECTORS[m] 호출
#   3) 변경 감지된 타겟의 used_by_items[] 항목 ID 수집 (영향 정책 식별)
#   4) 영향 정책마다 claude_updater.update_item_via_claude() 호출
#      → 기존 items/B0XX_*.json + 변경된 출처 본문 → Claude API → 갱신 JSON 생성
#   5) staging/B0XX_*.json 저장 + reports/YYYY-MM-DD.md / .json 리포트
#   6) 사람이 검토 후 confirm_apply.py 실행 → items/ 반영
#
# 사용법:
#   python -m crawler.crawler                       # 전체 312 타겟 점검
#   python -m crawler.crawler --dry-run             # 다운로드/Claude 호출 없이 변경 감지만
#   python -m crawler.crawler --only law            # target_id 에 'law' 포함된 것만
#   python -m crawler.crawler --skip-claude         # Claude API 호출 생략 (감지 + 리포트만)
import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ── 경로 설정 ────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent          # policy_db/
BACKEND_ROOT = ROOT.parent                              # welfare_backend/
CRAWL_TARGETS = ROOT / "crawl_targets.json"
ITEMS_DIR = ROOT / "items"
SCHEMA = ROOT / "schema.json"
SNAPSHOTS_DIR = ROOT / "crawler" / "snapshots"
STAGING_DIR = ROOT / "crawler" / "staging"
REPORTS_DIR = ROOT / "crawler" / "reports"

load_dotenv(BACKEND_ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("crawler")

# ── 동적 import (패키지/스크립트 양쪽 지원) ──────────────────
try:
    from .detectors import DETECTORS, save_snapshot, ChangeResult
except ImportError:
    sys.path.insert(0, str(ROOT))
    from crawler.detectors import DETECTORS, save_snapshot, ChangeResult  # type: ignore


def _load_targets() -> dict:
    return json.loads(CRAWL_TARGETS.read_text(encoding="utf-8"))


def _items_index() -> dict:
    """B0XX → 파일 경로 매핑."""
    idx = {}
    for jf in sorted(ITEMS_DIR.glob("B0*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            pid = data.get("id")
            if pid:
                idx[pid] = jf
        except Exception:
            pass
    return idx


async def _process_target(target: dict, client: httpx.AsyncClient, args) -> ChangeResult:
    """단일 타겟 변경 감지 + 결과 반환."""
    method = target.get("change_detection_method", "manual_review")
    detector = DETECTORS.get(method)
    if not detector:
        return ChangeResult(False, f"unknown method: {method}", fetched_url=target["url"])

    target_id = target["target_id"]
    snapshot_dir = SNAPSHOTS_DIR / target_id
    result = await detector(target, snapshot_dir, client=client)

    # 변경 감지된 경우 스냅샷 갱신 (다음 회차 비교 기준)
    if result.changed and not args.dry_run:
        save_snapshot(snapshot_dir, method, result)

    return result


async def run(args):
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    ct = _load_targets()
    targets = ct["targets"]

    # 필터 적용
    if args.only:
        targets = [t for t in targets if args.only.lower() in t["target_id"].lower()]
        logger.info("필터 적용 — %d개 타겟만 검사", len(targets))

    logger.info("🕷 크롤러 시작 — %d개 타겟, dry_run=%s, skip_claude=%s",
                len(targets), args.dry_run, args.skip_claude)

    changes = []          # 변경 감지된 타겟 리스트
    failures = []         # 실패한 타겟
    affected_items = set()  # 영향받는 정책 ID

    # ── 1) 변경 감지 동시 실행 (rate limit 위해 동시성 5 제한) ──
    sem = asyncio.Semaphore(5)
    async with httpx.AsyncClient() as client:
        async def task(t):
            async with sem:
                try:
                    r = await _process_target(t, client, args)
                    return t, r
                except Exception as e:
                    logger.exception("타겟 처리 실패 %s: %s", t.get("target_id"), e)
                    return t, ChangeResult(False, f"exception: {e}", fetched_url=t.get("url"))

        results = await asyncio.gather(*[task(t) for t in targets])

    for t, r in results:
        tid = t["target_id"]
        if "fetch_failed" in r.reason or "exception" in r.reason:
            failures.append({"target_id": tid, "reason": r.reason, "url": t.get("url")})
            continue
        if r.changed:
            changes.append({
                "target_id": tid,
                "title": t.get("title"),
                "url": t.get("url"),
                "publisher": t.get("publisher"),
                "method": t.get("change_detection_method"),
                "reason": r.reason,
                "used_by_items": t.get("used_by_items", []),
                "snapshot_dir": str((SNAPSHOTS_DIR / tid).relative_to(ROOT)),
            })
            for pid in t.get("used_by_items", []):
                affected_items.add(pid)
            logger.info("⚠️ 변경: %s — %s [%s]", tid, r.reason, ", ".join(t.get("used_by_items", [])))

    logger.info("📊 감지 결과: 변경 %d건 / 실패 %d건 / 영향 정책 %d개",
                len(changes), len(failures), len(affected_items))

    # ── 2) 변경된 영향 정책에 대해 Claude API 호출 (옵션) ──
    updated_items = []
    if changes and not args.dry_run and not args.skip_claude:
        try:
            from .claude_updater import update_item_via_claude
        except ImportError:
            from crawler.claude_updater import update_item_via_claude  # type: ignore

        items_idx = _items_index()
        # 영향 정책별로 그 정책의 변경된 출처들을 모음
        item_to_changes = defaultdict(list)
        for ch in changes:
            for pid in ch["used_by_items"]:
                item_to_changes[pid].append(ch)

        for pid, related_changes in item_to_changes.items():
            jf = items_idx.get(pid)
            if not jf:
                logger.warning("항목 파일 없음: %s — 스킵", pid)
                continue
            try:
                logger.info("🧠 Claude 갱신 호출: %s (%d개 변경 출처 반영)", pid, len(related_changes))
                staged_path, diff_summary = await update_item_via_claude(
                    item_path=jf,
                    related_changes=related_changes,
                    staging_dir=STAGING_DIR,
                    schema_path=SCHEMA,
                )
                if staged_path:
                    updated_items.append({
                        "policy_id": pid,
                        "staged": str(staged_path.relative_to(ROOT)),
                        "diff": diff_summary,
                        "sources_changed": [c["target_id"] for c in related_changes],
                    })
            except Exception as e:
                logger.exception("Claude 갱신 실패 %s: %s", pid, e)
                updated_items.append({"policy_id": pid, "error": str(e)})

    # ── 3) 리포트 작성 ──
    today = datetime.now().strftime("%Y-%m-%d")
    report_data = {
        "date": today,
        "summary": {
            "total_targets": len(targets),
            "changes_detected": len(changes),
            "failures": len(failures),
            "affected_items": sorted(affected_items),
            "items_updated_by_claude": len(updated_items),
        },
        "changes": changes,
        "failures": failures,
        "updated_items": updated_items,
        "dry_run": args.dry_run,
        "skip_claude": args.skip_claude,
    }
    report_json = REPORTS_DIR / f"{today}.json"
    report_md = REPORTS_DIR / f"{today}.md"
    report_json.write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md.write_text(_format_report_md(report_data), encoding="utf-8")
    logger.info("📝 리포트 작성: %s", report_md)

    print(_short_console_summary(report_data))
    return 0 if not failures else 0  # 실패가 있어도 exit 0 — 리포트 확인이 더 중요


def _format_report_md(d: dict) -> str:
    lines = []
    lines.append(f"# 크롤링 리포트 — {d['date']}")
    lines.append("")
    s = d["summary"]
    lines.append(f"- 총 타겟: {s['total_targets']}")
    lines.append(f"- 변경 감지: **{s['changes_detected']}건**")
    lines.append(f"- 영향 정책: {len(s['affected_items'])}개 — {', '.join(s['affected_items']) or '없음'}")
    lines.append(f"- 실패: {s['failures']}건")
    lines.append(f"- Claude 갱신: {s['items_updated_by_claude']}개")
    if d.get("dry_run"):
        lines.append("- (DRY RUN: 다운로드/저장만 시도, 스냅샷 갱신 안 함)")
    if d.get("skip_claude"):
        lines.append("- (Claude API 호출 생략됨)")
    lines.append("")
    lines.append("## 변경 감지된 출처")
    if not d["changes"]:
        lines.append("- 변경 사항 없음 — 모든 출처가 안정 상태입니다.")
    for c in d["changes"]:
        lines.append(f"- **{c['target_id']}** ({c['publisher']}) — {c['reason']}")
        lines.append(f"  - URL: {c['url']}")
        lines.append(f"  - 영향 정책: {', '.join(c['used_by_items']) or '(없음)'}")
        lines.append(f"  - 스냅샷: `{c['snapshot_dir']}`")
    lines.append("")
    lines.append("## Claude API 갱신 결과")
    if not d["updated_items"]:
        lines.append("- (Claude 호출 안 됨 또는 갱신 대상 없음)")
    for u in d["updated_items"]:
        if "error" in u:
            lines.append(f"- ❌ **{u['policy_id']}** — {u['error']}")
        else:
            lines.append(f"- ✅ **{u['policy_id']}** — staging: `{u['staged']}`")
            lines.append(f"  - 변경 출처: {', '.join(u['sources_changed'])}")
            lines.append(f"  - 요약: {u['diff']}")
    lines.append("")
    lines.append("## 실패 목록")
    for f in d["failures"]:
        lines.append(f"- {f['target_id']} — {f['reason']} ({f.get('url')})")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 다음 단계")
    lines.append("1. 위 `staging/` 의 갱신 JSON 을 검토하세요.")
    lines.append("2. 문제 없으면 다음 명령으로 반영:")
    lines.append("   ```")
    lines.append("   python -m crawler.confirm_apply --policy-id B001  # 항목별")
    lines.append("   python -m crawler.confirm_apply --all              # 일괄 (주의)")
    lines.append("   ```")
    return "\n".join(lines)


def _short_console_summary(d: dict) -> str:
    s = d["summary"]
    return (
        f"\n══════════════════════════════════════════\n"
        f"  📋 크롤링 요약 — {d['date']}\n"
        f"  총 타겟: {s['total_targets']}\n"
        f"  변경 감지: {s['changes_detected']}건\n"
        f"  영향 정책: {len(s['affected_items'])}개\n"
        f"  Claude 갱신: {s['items_updated_by_claude']}개\n"
        f"  실패: {s['failures']}건\n"
        f"  자세한 내용: policy_db/crawler/reports/{d['date']}.md\n"
        f"══════════════════════════════════════════\n"
    )


def main():
    p = argparse.ArgumentParser(description="정책DB 정기 크롤러")
    p.add_argument("--dry-run", action="store_true", help="다운로드/스냅샷 갱신 없이 감지만")
    p.add_argument("--skip-claude", action="store_true", help="Claude API 호출 생략, 감지·리포트만")
    p.add_argument("--only", type=str, default=None, help="target_id 부분일치 필터")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
