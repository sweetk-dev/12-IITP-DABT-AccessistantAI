# crawler/confirm_apply.py
# staging/ 의 갱신 JSON 을 검토 후 items/ 로 반영하는 CLI 도구.
#
# 사용법:
#   python -m crawler.confirm_apply --list                 # staging 대기 목록
#   python -m crawler.confirm_apply --policy-id B001       # B001 만 반영
#   python -m crawler.confirm_apply --policy-id B001 --diff # 반영 전 diff 보기
#   python -m crawler.confirm_apply --all                  # 전체 일괄 반영 (주의)
#   python -m crawler.confirm_apply --policy-id B001 --reject  # staging 폐기
#
# 안전 장치:
#   - 기존 items/B001_*.json 은 items/.backups/ 로 백업 후 덮어씀
#   - schema 재검증 통과한 경우만 반영
#   - 반영 후 DB 재적재 안내 (수동 실행 권장 — 자동 트리거 옵션은 --reingest)
import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import jsonschema

# 콘솔 출력 인코딩 강제 (#33) — cp949/POSIX(ascii) 로케일에서 한글·박스문자·이모지
# 출력 시 UnicodeEncodeError 로 죽지 않도록 stdout/stderr 를 UTF-8 로 재설정.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent     # policy_db/
ITEMS_DIR = ROOT / "items"
STAGING_DIR = ROOT / "crawler" / "staging"
BACKUPS_DIR = ITEMS_DIR / ".backups"
SCHEMA = ROOT / "schema.json"

try:
    from .detectors import save_baseline_snapshot
except ImportError:
    sys.path.insert(0, str(ROOT))
    from crawler.detectors import save_baseline_snapshot  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("confirm_apply")


def _find_staged(policy_id: str) -> list[Path]:
    """staging/ 에서 해당 policy_id 의 .staged.json 파일들 (최신순)."""
    pattern = f"{policy_id}_*.staged.json"
    files = sorted(STAGING_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _list_staged():
    if not STAGING_DIR.exists():
        print("staging/ 폴더가 없습니다. 크롤러 먼저 실행: python -m crawler.crawler")
        return
    files = sorted(STAGING_DIR.glob("B0*.staged.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("staging 대기 중 항목 없음.")
        return
    print(f"📋 staging/ 대기 항목 ({len(files)}개)")
    print("─" * 70)
    by_pid = {}
    for f in files:
        pid = f.name.split("_")[0]
        by_pid.setdefault(pid, []).append(f)
    for pid, fs in sorted(by_pid.items()):
        latest = fs[0]
        ts = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        size_kb = latest.stat().st_size / 1024
        extra = f" (+{len(fs)-1}개 이전 버전)" if len(fs) > 1 else ""
        print(f"  {pid}  {ts}  {size_kb:6.1f}KB  {latest.name}{extra}")
    print("─" * 70)
    print("반영: python -m crawler.confirm_apply --policy-id B0XX")


def _diff_view(existing: dict, new: dict) -> str:
    """필드별 변경 사항을 사람이 읽기 쉽게."""
    lines = []
    keys = sorted(set(existing.keys()) | set(new.keys()))
    for k in keys:
        ov, nv = existing.get(k), new.get(k)
        if ov == nv:
            continue
        if isinstance(ov, (dict, list)) or isinstance(nv, (dict, list)):
            ov_s = json.dumps(ov, ensure_ascii=False)
            nv_s = json.dumps(nv, ensure_ascii=False)
            ov_short = ov_s[:200] + ("…" if len(ov_s) > 200 else "")
            nv_short = nv_s[:200] + ("…" if len(nv_s) > 200 else "")
        else:
            ov_short = str(ov)[:200]
            nv_short = str(nv)[:200]
        lines.append(f"\n[{k}]")
        lines.append(f"  - 기존: {ov_short}")
        lines.append(f"  + 갱신: {nv_short}")
    return "\n".join(lines) if lines else "(변경 사항 없음 — 갱신 대상 아님)"


# ── 반영 전 회귀 가드 (C20) — 스키마가 못 잡는 조용한 손실 탐지 ──
TOP_LEVEL_REQUIRED = ["id", "leaflet_section", "leaflet_number", "title",
                      "short_summary", "category", "benefit_type",
                      "supported_amount", "eligibility", "legal_basis",
                      "how_to_use", "application", "sources",
                      "last_verified", "version"]
WATCHED_ARRAYS = ["legal_basis", "operating_agencies", "exceptions_and_caveats",
                  "faq", "contact", "sources", "related_items"]
SIZE_SHRINK_RATIO = 0.5    # 새 문서가 기존의 50% 미만이면 차단
ARRAY_SHRINK_RATIO = 0.5   # 배열 길이가 절반 이하로 줄면 차단


def _regression_check(existing: dict, new: dict) -> list:
    """패치 적용 결과가 기존을 의도치 않게 축소·손상시키는지 검사한다.
    반환: 차단 사유 문자열 리스트(비어 있으면 안전)."""
    issues = []
    # 1) 최상위 필수 키 누락
    for k in TOP_LEVEL_REQUIRED:
        if k in existing and k not in new:
            issues.append(f"필수 키 누락: {k}")
    # 2) 전체 크기 급감
    ol = len(json.dumps(existing, ensure_ascii=False))
    nl = len(json.dumps(new, ensure_ascii=False))
    if ol > 0 and nl < ol * SIZE_SHRINK_RATIO:
        issues.append(f"문서 크기 급감: {ol}B -> {nl}B (<{int(SIZE_SHRINK_RATIO*100)}%)")
    # 3) 주요 배열 길이 급감
    for k in WATCHED_ARRAYS:
        ov, nv = existing.get(k), new.get(k)
        if isinstance(ov, list) and isinstance(nv, list) and len(ov) >= 2:
            if len(nv) < len(ov) * ARRAY_SHRINK_RATIO:
                issues.append(f"배열 급감 {k}: {len(ov)} -> {len(nv)}")
    return issues


def _advance_baselines(staged_path: Path):
    """staged 파일 동반 .sources.json 을 읽어 각 출처 비교 baseline 을 전진 (#27 A안).
    감지 시점이 아니라 반영 성공 시점에만 호출된다."""
    sources_file = staged_path.parent / staged_path.name.replace(".staged.json", ".sources.json")
    if not sources_file.exists():
        logger.info("sources 사이드카 없음 — baseline 전진 생략: %s", staged_path.name)
        return
    try:
        sources = json.loads(sources_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("sources 사이드카 파싱 실패: %s", e)
        return
    n = 0
    for s in sources or []:
        sd, method, nh = s.get("snapshot_dir"), s.get("method"), s.get("new_hash")
        if sd and method and nh and save_baseline_snapshot(ROOT / sd, method, nh):
            n += 1
    logger.info("🔁 baseline 전진: %d개 출처", n)


def _apply_one(policy_id: str, *, diff_only: bool, reject: bool, auto_yes: bool) -> bool:
    files = _find_staged(policy_id)
    if not files:
        logger.error("staging 에 %s 항목 없음.", policy_id)
        return False
    latest = files[0]
    logger.info("📂 처리 대상: %s", latest.name)

    # reject 모드: staging 파일 폐기
    if reject:
        rej_dir = STAGING_DIR / ".rejected"
        rej_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(latest), str(rej_dir / latest.name))
        rej_sidecar = latest.parent / latest.name.replace(".staged.json", ".sources.json")
        if rej_sidecar.exists():
            shutil.move(str(rej_sidecar), str(rej_dir / rej_sidecar.name))
        logger.info("🗑 reject 처리: %s → %s", latest.name, rej_dir)
        return True

    new_data = json.loads(latest.read_text(encoding="utf-8"))

    # 기존 items/ 의 동일 항목 찾기
    existing_files = list(ITEMS_DIR.glob(f"{policy_id}_*.json"))
    if not existing_files:
        logger.error("items/ 에 %s 원본 없음 — 신규 항목? 수동 처리 필요.", policy_id)
        return False
    if len(existing_files) > 1:
        logger.warning("items/ 에 %s 이 여러 개: %s", policy_id, [f.name for f in existing_files])
    target = existing_files[0]
    existing = json.loads(target.read_text(encoding="utf-8"))

    # diff 표시
    diff_text = _diff_view(existing, new_data)
    print(f"\n══════ {policy_id} 변경 사항 ══════")
    print(diff_text)
    print("═" * 50)

    if diff_only:
        return True

    # schema 검증
    if SCHEMA.exists():
        validator = jsonschema.Draft7Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
        errs = list(validator.iter_errors(new_data))
        if errs:
            logger.error("❌ schema 검증 실패 (%d건):", len(errs))
            for e in errs[:5]:
                print(f"   - {list(e.path)}: {e.message[:120]}")
            return False
        logger.info("✅ schema 검증 PASS")

    # 회귀 가드 (C21) — 스키마가 못 잡는 손실을 반영 직전에 차단
    reg = _regression_check(existing, new_data)
    if reg:
        logger.error("회귀 가드 차단 (%d건):", len(reg))
        for r in reg:
            print(f"   - {r}")
        print("   → 의도된 변경이면 검토 후 수동 처리하세요. 자동 반영을 막았습니다.")
        return False

    # 패치 단계 검토 근거 표시 (C21) — 동반 .review.json (delete 후보/검토 항목)
    review_file = latest.parent / latest.name.replace(".staged.json", ".review.json")
    if review_file.exists():
        try:
            review = json.loads(review_file.read_text(encoding="utf-8"))
            if review:
                print(f"\n검토 필요 항목 {len(review)}건 (패치 단계):")
                for it in review[:10]:
                    tag = it.get("classification") or it.get("reason", "")
                    print(f"   - [{tag}] {it.get('path', '')} {str(it.get('evidence', ''))[:60]}")
        except Exception:
            pass

    # 사용자 확인
    if not auto_yes:
        ans = input(f"\n위 변경을 {target.name} 에 반영하시겠습니까? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("(취소됨)")
            return False

    # 백업 후 덮어쓰기
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_name = f"{target.stem}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak.json"
    shutil.copy2(target, BACKUPS_DIR / backup_name)
    target.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("✅ 반영 완료: %s (백업: %s)", target.name, backup_name)

    # #27 A안 — 반영 성공 시에만 출처 baseline 전진 (감지 시점엔 전진 안 함)
    _advance_baselines(latest)

    # staging 파일은 .applied/ 로 이동 (히스토리 보관)
    applied_dir = STAGING_DIR / ".applied"
    applied_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(latest), str(applied_dir / latest.name))
    src_sidecar = latest.parent / latest.name.replace(".staged.json", ".sources.json")
    if src_sidecar.exists():
        shutil.move(str(src_sidecar), str(applied_dir / src_sidecar.name))
    return True


def _trigger_reingest(policy_ids: list[str]):
    """반영된 항목들을 DB 에 부분 재적재.

    `ingest_sync.py` 가 파일 MD5 해시 기반 스마트 동기화를 수행하므로,
    변경된 items/B0XX.json 만 자동으로 감지하여 청크 재생성 + 임베딩 재생성.
    """
    sync_script = ROOT / "ingest_sync.py"
    if not sync_script.exists():
        logger.warning("ingest_sync.py 없음 — DB 재적재 수동으로 실행하세요.")
        return
    logger.info("🔄 DB 부분 재적재 시작 — ingest_sync.py 자동 호출 (%d개 영향)", len(policy_ids))
    try:
        # 별도 프로세스로 실행 (현재 프로세스 환경과 분리)
        result = subprocess.run(
            [sys.executable, str(sync_script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode == 0:
            logger.info("✅ ingest_sync.py 완료")
            # 마지막 요약 라인만 출력
            for line in (result.stdout or "").splitlines()[-6:]:
                if line.strip():
                    print(f"  {line}")
        else:
            logger.error("❌ ingest_sync.py 실패 (exit %d):\n%s",
                         result.returncode, result.stderr)
    except Exception as e:
        logger.exception("ingest_sync 호출 실패: %s", e)
        print(f"\n수동 실행 안내: cd {ROOT} && python ingest_sync.py")


def main():
    p = argparse.ArgumentParser(description="staging/ 갱신 JSON 을 items/ 에 반영")
    p.add_argument("--list", action="store_true", help="staging 대기 목록 보기")
    p.add_argument("--policy-id", type=str, help="반영할 정책 ID (예: B001)")
    p.add_argument("--all", action="store_true", help="staging 모든 항목 일괄 반영")
    p.add_argument("--diff", action="store_true", help="반영하지 않고 diff 만 보기")
    p.add_argument("--reject", action="store_true", help="staging 폐기 (의도된 변경이 아닐 때)")
    p.add_argument("-y", "--yes", action="store_true", help="확인 프롬프트 없이 자동 yes")
    p.add_argument("--reingest", action="store_true", help="반영 후 ingest_sync.py 로 DB 부분 재적재 실행")
    args = p.parse_args()

    if args.list or (not args.policy_id and not args.all):
        _list_staged()
        return

    applied = []
    if args.all:
        files = sorted(STAGING_DIR.glob("B0*.staged.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        unique_pids = []
        seen = set()
        for f in files:
            pid = f.name.split("_")[0]
            if pid not in seen:
                seen.add(pid); unique_pids.append(pid)
        for pid in unique_pids:
            if _apply_one(pid, diff_only=args.diff, reject=args.reject, auto_yes=args.yes):
                applied.append(pid)
    elif args.policy_id:
        if _apply_one(args.policy_id, diff_only=args.diff, reject=args.reject, auto_yes=args.yes):
            applied.append(args.policy_id)

    if applied and not args.diff and not args.reject and args.reingest:
        _trigger_reingest(applied)

    print(f"\n총 {len(applied)}개 항목 반영 완료.")


if __name__ == "__main__":
    main()
