# crawler/crawler.py
# 정기 크롤러 메인 오케스트레이션 — 매월 2일·16일 실행 권장.
#
# 흐름:
#   1) crawl_targets.json 의 모든 타겟 로드 (현재 382개)
#   2) 각 타겟별 change_detection_method 에 따라 detectors.DETECTORS[m] 호출
#   3) 변경 감지된 타겟의 used_by_items[] 항목 ID 수집 (영향 정책 식별)
#   4) 영향 정책마다 claude_updater.update_item_via_claude() 호출
#      → 기존 items/B0XX_*.json + 변경된 출처 본문 → Claude API → 갱신 JSON 생성
#   5) staging/B0XX_*.json 저장 + reports/YYYY-MM-DD.md / .json 리포트
#   6) 사람이 검토 후 confirm_apply.py 실행 → items/ 반영
#
# 사용법:
#   python -m crawler.crawler                       # 전체 타겟 점검 (현재 382개)
#   python -m crawler.crawler --dry-run             # 다운로드/Claude 호출 없이 변경 감지만
#   python -m crawler.crawler --only law            # target_id 에 'law' 포함된 것만
#   python -m crawler.crawler --skip-claude         # Claude API 호출 생략 (감지 + 리포트만)
#   python -m crawler.crawler --mark-reviewed all   # manual_review 타겟 검토일 기록 (크롤링 안 함)
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

# 콘솔 출력 인코딩 강제 (#33) — cp949/POSIX(ascii) 로케일에서 한글·박스문자·이모지
# 출력 시 UnicodeEncodeError 로 죽지 않도록 stdout/stderr 를 UTF-8 로 재설정.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 경로 설정 ────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent          # policy_db/
BACKEND_ROOT = ROOT.parent                              # welfare_backend/

load_dotenv(BACKEND_ROOT / ".env")

# 가변 데이터 루트 — POLICY_DATA_DIR 설정 시 그 경로, 미설정 시 ROOT (하위호환)
DATA_ROOT = Path(os.environ["POLICY_DATA_DIR"]).resolve() if os.environ.get("POLICY_DATA_DIR") else ROOT
CRAWL_TARGETS = ROOT / "crawl_targets.json"            # 설정(읽기전용) — 코드 경로 유지
SCHEMA = ROOT / "schema.json"                          # 설정(읽기전용) — 코드 경로 유지
ITEMS_DIR = DATA_ROOT / "items"
SNAPSHOTS_DIR = DATA_ROOT / "crawler" / "snapshots"
STAGING_DIR = DATA_ROOT / "crawler" / "staging"
REPORTS_DIR = DATA_ROOT / "crawler" / "reports"
MANUAL_STATE = DATA_ROOT / "crawler" / "manual_review_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("crawler")

# ── 동적 import (패키지/스크립트 양쪽 지원) ──────────────────
try:
    from .detectors import DETECTORS, save_content_snapshot, save_baseline_snapshot, ChangeResult
except ImportError:
    sys.path.insert(0, str(ROOT))
    from crawler.detectors import DETECTORS, save_content_snapshot, save_baseline_snapshot, ChangeResult  # type: ignore


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


def _load_manual_state() -> dict:
    """manual_review 타겟의 마지막 검토일 상태 로드 (#28). 없으면 빈 dict."""
    try:
        return json.loads(MANUAL_STATE.read_text(encoding="utf-8")) if MANUAL_STATE.exists() else {}
    except Exception:
        return {}


def _mark_reviewed(target_spec: str) -> int:
    """manual_review 타겟의 마지막 검토일을 오늘로 기록 (#28). target_spec='all' 이면 전체."""
    ct = _load_targets()
    manual_ids = [t["target_id"] for t in ct["targets"]
                  if t.get("change_detection_method") == "manual_review"]
    if target_spec == "all":
        ids = manual_ids
    elif target_spec in manual_ids:
        ids = [target_spec]
    else:
        logger.error("manual_review 타겟이 아님: %s (대상: %s)", target_spec, ", ".join(manual_ids))
        return 1
    state = _load_manual_state()
    today = datetime.now().strftime("%Y-%m-%d")
    for tid in ids:
        state[tid] = today
    MANUAL_STATE.parent.mkdir(parents=True, exist_ok=True)
    MANUAL_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 검토일 기록: {len(ids)}개 타겟 → {today}")
    return 0


async def _process_target(target: dict, client: httpx.AsyncClient, args) -> ChangeResult:
    """단일 타겟 변경 감지 + 결과 반환."""
    method = target.get("change_detection_method", "manual_review")
    detector = DETECTORS.get(method)
    if not detector:
        return ChangeResult(False, f"unknown method: {method}")

    target_id = target["target_id"]
    snapshot_dir = SNAPSHOTS_DIR / target_id
    result = await detector(target, snapshot_dir, client=client)

    # 변경 감지 시 본문만 저장 (baseline 은 confirm 반영 시 전진 — #27 A안)
    if result.changed and not args.dry_run:
        save_content_snapshot(snapshot_dir, method, result)

    return result


def _purge_staging(scope_pids):
    """staging 의 .staged.json(+사이드카)을 .rejected 로 이동. scope_pids 비면 전체."""
    import shutil
    rej = STAGING_DIR / ".rejected"
    rej.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in list(STAGING_DIR.glob("B0*.staged.json")):
        pid = f.name.split("_")[0]
        if scope_pids and pid not in scope_pids:
            continue
        shutil.move(str(f), str(rej / f.name))
        for ext in (".sources.json", ".review.json", ".triage.json"):
            side = f.parent / f.name.replace(".staged.json", ext)
            if side.exists():
                shutil.move(str(side), str(rej / side.name))
        n += 1
    return n


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
    if getattr(args, "policy", None):
        targets = [t for t in targets if args.policy in (t.get("used_by_items") or [])]
        logger.info("정책 필터(%s) — %d개 타겟만 검사", args.policy, len(targets))

    logger.info("🕷 크롤러 시작 — %d개 타겟, dry_run=%s, skip_claude=%s",
                len(targets), args.dry_run, args.skip_claude)

    changes = []          # 변경 감지된 타겟 리스트
    failures = []         # 실패한 타겟
    affected_items = set()  # 영향받는 정책 ID
    manual_targets = []   # manual_review 타겟 (#28)
    skipped_staged = []   # 이미 staging 대기라 LLM 생략된 정책 (#27 A안)

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
                    return t, ChangeResult(False, f"exception: {e}")

        results = await asyncio.gather(*[task(t) for t in targets])

    # ── 기준 확정(baseline 초기화) 모드 — LLM/staging 없이 비교 기준만 설정 ──
    if getattr(args, "init_baseline", False):
        n_base = 0
        for t, r in results:
            method = t.get("change_detection_method")
            if method == "manual_review" or not getattr(r, "new_hash", None):
                continue
            if save_baseline_snapshot(SNAPSHOTS_DIR / t["target_id"], method, r.new_hash):
                n_base += 1
        scope_pids = {args.policy} if getattr(args, "policy", None) else set()
        purged = _purge_staging(scope_pids)
        msg = f"기준 확정 완료 — baseline {n_base}개 설정, staging 정리 {purged}개"
        logger.info("🧱 %s", msg)
        print(msg)
        return 0

    for t, r in results:
        tid = t["target_id"]
        if "fetch_failed" in r.reason or "exception" in r.reason:
            failures.append({"target_id": tid, "reason": r.reason, "url": t.get("url")})
            continue
        if t.get("change_detection_method") == "manual_review":
            manual_targets.append({
                "target_id": tid,
                "title": t.get("title"),
                "url": t.get("url"),
                "publisher": t.get("publisher"),
                "used_by_items": t.get("used_by_items", []),
            })
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
                "snapshot_dir": str((SNAPSHOTS_DIR / tid).relative_to(DATA_ROOT)),
                "chunk_diff": r.chunk_diff,
                "new_hash": r.new_hash,
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

        # 이미 staging 대기 중인 정책은 LLM 재호출 생략 (#27 A안 — 미확정 변경 중복 비용 방지)
        pending_pids = {p.name.split("_")[0] for p in STAGING_DIR.glob("B0*.staged.json")}

        for pid, related_changes in item_to_changes.items():
            jf = items_idx.get(pid)
            if not jf:
                logger.warning("항목 파일 없음: %s — 스킵", pid)
                continue
            if pid in pending_pids:
                logger.info("⏭ %s — 이미 staging 대기 중, LLM 재호출 생략", pid)
                skipped_staged.append(pid)
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
                        "staged": str(staged_path.relative_to(DATA_ROOT)),
                        "diff": diff_summary,
                        "sources_changed": [c["target_id"] for c in related_changes],
                    })
            except Exception as e:
                logger.exception("Claude 갱신 실패 %s: %s", pid, e)
                updated_items.append({"policy_id": pid, "error": str(e)})

    # ── 3) 리포트 작성 ──
    # manual_review 타겟에 마지막 검토일 부가 (#28)
    mstate = _load_manual_state()
    today_d = datetime.now().date()
    for mt in manual_targets:
        lr = mstate.get(mt["target_id"])
        mt["last_reviewed"] = lr
        days = None
        if lr:
            try:
                days = (today_d - datetime.strptime(lr, "%Y-%m-%d").date()).days
            except Exception:
                days = None
        mt["days_since_review"] = days

    today = datetime.now().strftime("%Y-%m-%d")
    report_data = {
        "date": today,
        "summary": {
            "total_targets": len(targets),
            "changes_detected": len(changes),
            "failures": len(failures),
            "affected_items": sorted(affected_items),
            "items_updated_by_claude": len(updated_items),
            "manual_review_targets": len(manual_targets),
            "skipped_already_staged": len(skipped_staged),
        },
        "changes": changes,
        "failures": failures,
        "updated_items": updated_items,
        "manual_review_targets": manual_targets,
        "skipped_already_staged": skipped_staged,
        "dry_run": args.dry_run,
        "skip_claude": args.skip_claude,
    }
    report_json = REPORTS_DIR / f"{today}.json"
    report_md = REPORTS_DIR / f"{today}.md"
    report_json.write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md.write_text(_format_report_md(report_data), encoding="utf-8")
    logger.info("📝 리포트 작성: %s", report_md)

    print(_short_console_summary(report_data))
    return 0  # 실패가 있어도 항상 exit 0 — 리포트 확인이 더 중요


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
        cd = c.get("chunk_diff")
        if cd:
            lines.append(
                f"  - 청크 변경: 추가 {len(cd.get('added', []))} / "
                f"삭제 {len(cd.get('removed', []))} / 수정 {len(cd.get('changed', []))} "
                f"(유지 {cd.get('unchanged', 0)})"
            )
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
    lines.append("## 수동 검토 대상 (manual_review)")
    mts = d.get("manual_review_targets", [])
    if not mts:
        lines.append("- 없음")
    for mt in mts:
        lr = mt.get("last_reviewed")
        ds = mt.get("days_since_review")
        when = f"마지막 검토 {lr} ({ds}일 경과)" if lr else "검토 이력 없음"
        lines.append(f"- **{mt['target_id']}** ({mt.get('publisher') or '?'}) — {when}")
        lines.append(f"  - {mt.get('title') or ''} / {mt.get('url')}")
        lines.append(f"  - 영향 정책: {', '.join(mt.get('used_by_items', [])) or '(없음)'}")
    if d.get("skipped_already_staged"):
        lines.append("")
        lines.append("## LLM 생략 (이미 staging 대기)")
        lines.append("- " + ", ".join(d["skipped_already_staged"]))
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
        f"  수동 검토 대상: {s.get('manual_review_targets', 0)}개\n"
        f"  자세한 내용: policy_db/crawler/reports/{d['date']}.md\n"
        f"══════════════════════════════════════════\n"
    )


def main():
    p = argparse.ArgumentParser(description="정책DB 정기 크롤러")
    p.add_argument("--dry-run", action="store_true", help="다운로드/스냅샷 갱신 없이 감지만")
    p.add_argument("--skip-claude", action="store_true", help="Claude API 호출 생략, 감지·리포트만")
    p.add_argument("--only", type=str, default=None, help="target_id 부분일치 필터")
    p.add_argument("--policy", type=str, default=None,
                   help="used_by_items 에 해당 정책 ID 가 포함된 출처만 점검 (예: B001)")
    p.add_argument("--init-baseline", dest="init_baseline", action="store_true",
                   help="현재 출처 상태를 비교 baseline 으로 확정(LLM/staging 없음) + 관련 staging 정리")
    p.add_argument("--mark-reviewed", type=str, default=None,
                   help="수동 검토 완료 표시 — target_id(또는 all)의 마지막 검토일을 오늘로 기록 후 종료")
    args = p.parse_args()
    if args.mark_reviewed:
        sys.exit(_mark_reviewed(args.mark_reviewed))
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
