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
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
# 가변 데이터 루트 — POLICY_DATA_DIR 설정 시 그 경로, 미설정 시 ROOT (하위호환)
DATA_ROOT = Path(os.environ["POLICY_DATA_DIR"]).resolve() if os.environ.get("POLICY_DATA_DIR") else ROOT
ITEMS_DIR = DATA_ROOT / "items"
STAGING_DIR = DATA_ROOT / "crawler" / "staging"
BACKUPS_DIR = ITEMS_DIR / ".backups"
SCHEMA = ROOT / "schema.json"

try:
    from .detectors import save_baseline_snapshot
except ImportError:
    sys.path.insert(0, str(ROOT))
    from crawler.detectors import save_baseline_snapshot  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("confirm_apply")


# ── 정책 ID 형식 검증 ─────────────────────────────────────────
# 정책 ID 는 파일 경로를 만드는 데 그대로 쓰인다(조회: glob(f"{id}_*.json"),
# 생성: f"{id}_{slug}.json"). 형식을 확인하지 않으면 "*" 가 든 ID 는 다른 정책
# 파일에 매칭되고, "../" 가 든 ID 는 items/ 밖의 경로를 가리키게 된다.
# 형식: 대문자 B + 숫자 3자리 이상(B001, B051, B100 …). 자릿수 상한은 두지 않는다
# — 항목이 999건을 넘어도 같은 규칙으로 이어지게 하기 위함.
POLICY_ID_PATTERN = r"B[0-9]{3,}"
_POLICY_ID_RE = re.compile(POLICY_ID_PATTERN)

# 정책 항목 파일/스테이징 파일을 찾는 glob 접두부.
# "B0*" 로 두면 B100 부터는 목록·다음 ID 계산·크롤·적재에서 모두 빠지므로
# 'B + 숫자' 로 시작하는 이름 전체를 대상으로 한다.
ITEM_GLOB_PREFIX = "B[0-9]*"


def validate_policy_id(policy_id) -> str:
    """정책 ID 형식(^B[0-9]{3,}$)을 검증한다.

    인자: policy_id — 검증할 값(문자열이 아니어도 받는다).
    반환: 형식이 맞으면 그 ID 문자열 그대로.
    실패: 형식이 틀리면 ValueError. 호출자는 이를 400 응답 또는
          {"ok": False, "error": ...} 로 바꿔 돌려준다.
    fullmatch 를 쓰는 이유: "$" 는 끝의 개행 앞에서도 매칭되어 "B001\n" 이 통과한다.
    """
    if not isinstance(policy_id, str) or not _POLICY_ID_RE.fullmatch(policy_id):
        raise ValueError(f"정책 id 형식 오류: {policy_id!r} (B + 숫자 3자리 이상이어야 함)")
    return policy_id


# ── 원자적 파일 쓰기 ─────────────────────────────────────────
def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    """파일 내용을 원자적으로 교체한다.

    Path.write_text 는 대상 파일을 먼저 비운 뒤 쓰기 때문에, 쓰는 도중 프로세스가
    죽거나 디스크가 가득 차면 잘린 파일이 남는다. 정책 항목(items/*.json)이나
    크롤 타겟 오버레이가 그렇게 잘리면 다음 읽기에서 JSON 파싱이 실패한다.
    → 같은 디렉터리의 임시 파일에 전부 쓰고 디스크에 내린 뒤 os.replace 로 바꾼다.
      (같은 파일시스템 안의 rename 은 원자적이므로, 읽는 쪽은 항상 이전 내용 전체
       또는 새 내용 전체만 보게 된다.)

    인자: path — 대상 경로(str 또는 Path). text — 쓸 내용. encoding — 기본 utf-8.
    실패: 쓰기·교체 중 예외가 나면 임시 파일을 지우고 예외를 그대로 올린다.
          이 경우 기존 파일은 손대지 않은 상태로 남는다.
    임시 파일 이름은 "." 으로 시작하고 ".tmp" 로 끝나 항목 glob("B[0-9]*.json")에
    걸리지 않는다.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp 는 0600 으로 만든다. 기존 파일이 있으면 그 권한을 유지하고,
        # 새 파일이면 일반 파일 생성과 같은 권한(0666 & ~umask)을 준다 —
        # 백업·볼륨 공유 등 다른 계정의 읽기 권한이 바뀌지 않게 하기 위함.
        try:
            if path.exists():
                shutil.copymode(str(path), tmp_name)
            else:
                um = os.umask(0)
                os.umask(um)
                os.chmod(tmp_name, 0o666 & ~um)
        except OSError:
            pass
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _find_staged(policy_id: str) -> list[Path]:
    """staging/ 에서 해당 policy_id 의 .staged.json 파일들 (최신순)."""
    pattern = f"{policy_id}_*.staged.json"
    files = sorted(STAGING_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _list_staged():
    if not STAGING_DIR.exists():
        print("staging/ 폴더가 없습니다. 크롤러 먼저 실행: python -m crawler.crawler")
        return
    files = sorted(STAGING_DIR.glob(f"{ITEM_GLOB_PREFIX}.staged.json"), key=lambda p: p.stat().st_mtime, reverse=True)
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
        if sd and method and nh and save_baseline_snapshot(DATA_ROOT / sd, method, nh):
            n += 1
    logger.info("🔁 baseline 전진: %d개 출처", n)


def _apply_one(policy_id: str, *, diff_only: bool, reject: bool, auto_yes: bool) -> bool:
    # --policy-id 값은 아래에서 glob 패턴에 그대로 들어간다. 형식이 틀린 값
    # (와일드카드·경로 구분자 포함)은 다른 정책 파일에 매칭될 수 있어 먼저 걸러낸다.
    try:
        validate_policy_id(policy_id)
    except ValueError as e:
        logger.error("%s", e)
        return False
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
        # 동반 사이드카 3종 모두 이동 (review_core.reject / _purge_staging 와 동일 — 고아 파일 방지)
        for ext in (".sources.json", ".review.json", ".triage.json"):
            side = latest.parent / latest.name.replace(".staged.json", ext)
            if side.exists():
                shutil.move(str(side), str(rej_dir / side.name))
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
    # 임시 파일 → os.replace 로 교체(쓰는 도중 중단돼도 항목 파일이 잘리지 않음)
    atomic_write_text(target, json.dumps(new_data, ensure_ascii=False, indent=2))
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


# ── DB 재적재 ────────────────────────────────────────────────
# ingest_sync.py 실행 제한 시간(초).
# 근거: 정책 1건은 청크 15~30개, 청크당 임베딩 호출 약 1초 + 페이싱 0.2초라 보통 1분 안에
#       끝난다. 변경 감지는 파일 해시 기준이라 DB 가 뒤처져 있으면 전 항목(50여 건)이 한 번에
#       다시 적재될 수 있고, 429 응답 시에는 청크마다 최대 60초씩 대기하므로 여유를 둔다.
#       30분을 넘기면 DB·임베딩 API 가 응답하지 않는 상태로 보고 중단한다.
#       (제한이 없으면 재적재를 기다리는 요청 스레드가 무기한 붙잡힌다.)
REINGEST_TIMEOUT_SEC = 1800

# 예외 메시지에 싣는 stderr 끝부분의 줄 수·글자 수 상한.
# 이 메시지는 콘솔 응답(reingest_error)으로 그대로 나가므로 원인 파악에 필요한 만큼만 싣는다.
_ERR_TAIL_LINES = 5
_ERR_TAIL_CHARS = 600


class ReingestError(RuntimeError):
    """DB 재적재(ingest_sync.py) 실패. 메시지는 콘솔 응답에 실어도 되는 형태로 정리돼 있다."""


def _safe_err_tail(text: str) -> str:
    """하위 프로세스 stderr 의 끝부분을, 접속 정보·키를 가린 뒤 돌려준다.

    stderr 에는 DB 드라이버·HTTP 클라이언트의 오류 문구가 그대로 찍힐 수 있고,
    그 안에 접속 문자열(postgresql://계정:비밀번호@호스트/DB, "host=… password=…")이나
    API 키가 포함될 수 있다. 이 문자열은 관리자 콘솔 응답과 로그에 실리므로 다음을 가린다.
      - URL 형태의 접속 문자열 전체
      - DSN 의 key=value 조각(host, port, user, password, dbname)
      - DB 드라이버 오류 문구 속 서버 주소·포트·계정·DB 이름
        (예: connection to server at "…", port 5432 / for user "…" / database "…")
      - 환경변수 DB_PASS / GEMINI_API_KEY 의 실제 값(4자 이상일 때)
      - 쿼리스트링의 key=… , "AIza…" 형태의 API 키
    반환: 마지막 _ERR_TAIL_LINES 줄을 " / " 로 이은 문자열(최대 _ERR_TAIL_CHARS 자). 비어 있으면 "".
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    tail = " / ".join(lines[-_ERR_TAIL_LINES:])
    tail = re.sub(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/@]+@\S+", "<접속정보>", tail)
    tail = re.sub(r"\b(host|hostaddr|port|user|password|dbname)\s*=\s*('[^']*'|\S+)",
                  r"\1=***", tail, flags=re.IGNORECASE)
    tail = re.sub(r'\b(server at|host|user|role|database)\s+"[^"]*"', r'\1 "***"', tail,
                  flags=re.IGNORECASE)
    tail = re.sub(r"\bport\s+\d+", "port ***", tail, flags=re.IGNORECASE)
    tail = re.sub(r"([?&]key=)[A-Za-z0-9_\-]+", r"\1***", tail)
    tail = re.sub(r"AIza[A-Za-z0-9_\-]{10,}", "AIza***", tail)
    for env_name in ("DB_PASS", "GEMINI_API_KEY"):
        secret = os.environ.get(env_name) or ""
        if len(secret) >= 4:
            tail = tail.replace(secret, "***")
    return tail[-_ERR_TAIL_CHARS:]


def _trigger_reingest(policy_ids: list[str]):
    """반영된 항목들을 DB 에 부분 재적재.

    `ingest_sync.py` 가 파일 MD5 해시 기반 스마트 동기화를 수행하므로,
    변경된 items/B0XX.json 만 자동으로 감지하여 청크 재생성 + 임베딩 재생성.

    반환: 없음(정상 종료 시).
    실패: ReingestError 를 올린다 — 스크립트 없음 / 종료 코드 0 아님 / 제한 시간 초과 /
          프로세스 실행 자체 실패. 이 경우에 로그만 남기고 정상 반환하면 호출자가
          "재적재 성공" 으로 응답하게 되어, DB 는 옛 내용인데 콘솔에는 반영됨으로 표시된다.
    호출자 계약: 이 함수는 items/ 파일을 건드리지 않는다. 호출자는 파일 반영을 마친 뒤에
          부르고, 예외를 잡아 `reingested: False` + 오류 문구로 응답한다(파일 변경은 유지).
    """
    sync_script = ROOT / "ingest_sync.py"
    if not sync_script.exists():
        logger.warning("ingest_sync.py 없음 — DB 재적재 수동으로 실행하세요.")
        raise ReingestError("ingest_sync.py 없음 — DB 재적재가 실행되지 않았습니다")
    logger.info("🔄 DB 부분 재적재 시작 — ingest_sync.py 자동 호출 (%d개 영향)", len(policy_ids))
    try:
        # 별도 프로세스로 실행 (현재 프로세스 환경과 분리)
        result = subprocess.run(
            [sys.executable, str(sync_script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            # 디코딩 불가 바이트가 섞여도 결과 판정(종료 코드)까지 가도록 치환한다.
            errors="replace",
            timeout=REINGEST_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run 은 제한 시간 초과 시 하위 프로세스를 종료시킨 뒤 예외를 올린다.
        logger.error("❌ ingest_sync.py 제한 시간(%d초) 초과 — 중단", REINGEST_TIMEOUT_SEC)
        raise ReingestError(f"ingest_sync 제한 시간({REINGEST_TIMEOUT_SEC}초) 초과") from None
    except Exception as e:
        logger.exception("ingest_sync 호출 실패: %s", e)
        raise ReingestError(f"ingest_sync 실행 실패: {type(e).__name__}") from e

    if result.returncode != 0:
        tail = _safe_err_tail(result.stderr)
        logger.error("❌ ingest_sync.py 실패 (exit %d): %s", result.returncode, tail)
        raise ReingestError(f"ingest_sync 실패(exit {result.returncode})" + (f": {tail}" if tail else ""))

    logger.info("✅ ingest_sync.py 완료")
    # 마지막 요약 라인만 출력
    for line in (result.stdout or "").splitlines()[-6:]:
        if line.strip():
            print(f"  {line}")


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
        files = sorted(STAGING_DIR.glob(f"{ITEM_GLOB_PREFIX}.staged.json"),
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

    reingest_failed = False
    if applied and not args.diff and not args.reject and args.reingest:
        # 재적재가 실패해도 items/ 반영은 이미 끝난 상태다. 반영 결과는 그대로 보고하고,
        # 수동 실행 방법을 안내한 뒤 종료 코드로 실패를 알린다(스케줄러·스크립트가 인지하도록).
        try:
            _trigger_reingest(applied)
        except Exception as e:
            reingest_failed = True
            print(f"\n❌ DB 재적재 실패: {e}")
            print(f"수동 실행 안내: cd {ROOT} && python ingest_sync.py")

    print(f"\n총 {len(applied)}개 항목 반영 완료.")
    if reingest_failed:
        print("(items/ 파일 반영은 완료됐으나 DB 재적재는 실패 — 위 안내대로 다시 실행하세요.)")
        sys.exit(1)


if __name__ == "__main__":
    main()
