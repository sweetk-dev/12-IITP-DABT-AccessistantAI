# crawler/target_sync.py
# 정책 항목의 sources[] → 크롤 타겟 파생·등록 (#235)
#
# 배경:
#   crawl_targets.json 의 description 은 "항목별 JSON의 sources 배열과 1:1 동기화"를
#   전제하지만, 그 동기화를 수행하는 코드가 없었다. 콘솔에서 승인해 만든 정책은
#   크롤 대상에 등록되지 않아 이후 어떤 변경 감지도 받지 못하는 상태가 된다.
#
# ⚠️ 경로 주의:
#   crawl_targets.json 은 코드 경로(policy_db/)에 있어 컨테이너 이미지에 포함된다.
#   여기에 런타임으로 쓰면 컨테이너를 다시 만들 때 사라진다. 그래서 scheduler 가
#   admin_schedule.json 으로 쓰는 방식과 동일하게, 영속 볼륨에 오버레이 파일을 두고
#   읽는 시점에 병합한다.
#
#       기준: policy_db/crawl_targets.json          (읽기 전용, 레포 관리)
#       확장: $POLICY_DATA_DIR/crawl_targets.local.json (쓰기, 영속 볼륨)
import json
import logging
import re
import shutil
from datetime import date
from urllib.parse import urlparse

try:
    from . import confirm_apply as ca
except ImportError:
    import confirm_apply as ca  # type: ignore

logger = logging.getLogger(__name__)

BASE_TARGETS = ca.ROOT / "crawl_targets.json"
LOCAL_TARGETS = ca.DATA_ROOT / "crawl_targets.local.json"
# 타겟별 감지 스냅샷(비교 기준) 위치 — crawler.py 의 SNAPSHOTS_DIR 과 같은 경로여야 한다.
SNAPSHOTS_DIR = ca.DATA_ROOT / "crawler" / "snapshots"

# crawl_targets.json 의 change_detection_methods 키와 일치해야 한다.
VALID_METHODS = {"page_hash", "pdf_hash", "last_modified_field",
                 "css_selector_text", "manual_review"}
VALID_FREQUENCIES = {"daily", "weekly", "monthly", "quarterly", "on_demand"}

# 이 도메인들은 '시행일/수정일' 필드가 안정적으로 노출되어 해시보다 정확하다.
_LAST_MODIFIED_HOSTS = ("law.go.kr", "easylaw.go.kr")


class TargetsFileError(ValueError):
    """크롤 타겟 파일이 존재하지만 읽거나 해석할 수 없음."""


def _load_json(path, default=None):
    """JSON 파일을 읽는다.

    반환: 파일이 없으면 default(없으면 {}), 있으면 파싱한 객체(dict).
    실패: 파일이 있는데 읽기·파싱에 실패하거나 최상위가 객체가 아니면 TargetsFileError.

    파일이 없는 것과 깨진 것을 구분하는 이유: 깨진 경우에도 빈 값을 돌려주면
    register_policy 가 그 빈 값에 새 타겟만 얹어 저장하므로, 오버레이에 쌓여 있던
    기존 등록 타겟이 전부 사라진다. 예외를 올려 쓰기까지 가지 않게 한다
    (깨진 파일은 그대로 남으므로 사람이 복구하거나 백업에서 되돌릴 수 있다).
    읽기 전용 경로(load_all → 크롤러)도 같은 예외를 받는다 — 일부 타겟이 빠진 채
    정상 종료하는 것보다 실행이 실패로 드러나는 편이 낫기 때문이다.
    """
    if not path.exists():
        return default if default is not None else {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TargetsFileError(f"{path.name} 을(를) 읽을 수 없음: {e}") from e
    if not isinstance(data, dict):
        raise TargetsFileError(f"{path.name} 형식 오류: 최상위가 객체가 아님")
    return data


def load_local() -> dict:
    """오버레이 파일. targets(신규 타겟) + used_by_patch(기존 타겟에 정책 추가)."""
    d = _load_json(LOCAL_TARGETS, {})
    d.setdefault("targets", [])
    d.setdefault("used_by_patch", {})
    return d


def load_all() -> dict:
    """기준 파일 ⊕ 오버레이 병합 결과. 크롤러가 보는 것과 동일한 최종 타겟 목록."""
    base = _load_json(BASE_TARGETS, {"targets": []})
    local = load_local()

    merged = {t.get("target_id"): dict(t) for t in base.get("targets", []) if t.get("target_id")}

    # 신규 타겟 (같은 target_id 면 오버레이가 이김)
    for t in local.get("targets", []):
        if t.get("target_id"):
            merged[t["target_id"]] = dict(t)

    # 기존 타겟에 정책만 추가하는 패치 (같은 URL 을 여러 정책이 공유하는 경우).
    # ⚠️ 반드시 오버레이 타겟을 합친 뒤에 적용해야 한다. 먼저 적용하면
    #    오버레이가 만든 타겟에 대한 연결이 그 타겟에 덮여 사라진다.
    for tid, pids in (local.get("used_by_patch") or {}).items():
        if tid in merged:
            cur = list(merged[tid].get("used_by_items") or [])
            for p in pids:
                if p not in cur:
                    cur.append(p)
            merged[tid]["used_by_items"] = cur

    out = dict(base)
    out["targets"] = list(merged.values())
    return out


# ── sources[] → 타겟 파생 ────────────────────────────────────
def _norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/")


def _is_bare_domain(url: str) -> bool:
    """경로도 쿼리도 없는 도메인 루트 — 변경 감지가 성립하지 않는다.

    예: https://www.bokjiro.go.kr, https://www.gg.go.kr
    이런 URL 에 page_hash 를 걸면 첫 화면 배너 변동으로 매번 오탐이 난다.
    """
    try:
        p = urlparse(url)
    except Exception:
        return True
    return p.path.strip("/") == "" and not p.query


def guess_method(url: str) -> str:
    """출처 URL 로 변경 감지 방식을 추정한다.

    확신이 없으면 manual_review 로 떨어뜨린다 — 잘못된 방식으로 등록하면
    매번 오탐이 나거나(page_hash on 루트) 영영 조용해진다.
    """
    u = (url or "").lower()
    if _is_bare_domain(u):
        return "manual_review"
    if u.endswith(".pdf") or ".pdf?" in u:
        return "pdf_hash"
    host = urlparse(u).netloc
    if any(h in host for h in _LAST_MODIFIED_HOSTS):
        return "last_modified_field"
    return "page_hash"


def _target_id(policy_id: str, idx: int, url: str) -> str:
    host = urlparse(url).netloc.replace("www.", "").split(".")[0] or "src"
    host = re.sub(r"[^a-z0-9]+", "", host.lower()) or "src"
    return f"auto_{policy_id.lower()}_{idx}_{host}"


def derive_targets(policy: dict) -> list:
    """정책 1건의 sources[] 를 크롤 타겟 목록으로 변환한다.

    sources[].crawl 에 값이 있으면 그것을 우선한다(사람이 지정한 값 존중).
    없으면 URL 로 추정하고, 추정이 불확실하면 manual_review 로 둔다.
    """
    pid = policy.get("id")
    out = []
    for i, s in enumerate(policy.get("sources") or [], start=1):
        url = _norm_url(s.get("url"))
        if not url:
            continue
        crawl = s.get("crawl") or {}

        method = crawl.get("change_detection_method")
        if method not in VALID_METHODS:
            method = guess_method(url)

        freq = crawl.get("frequency")
        if freq not in VALID_FREQUENCIES:
            freq = "monthly"

        notes = crawl.get("notes")
        if method == "manual_review" and not notes:
            notes = ("자동 감지 방식을 정하지 못해 수동 검토로 등록됨"
                     + (" (도메인 루트 URL — 구체 페이지로 교체 권장)" if _is_bare_domain(url) else ""))

        out.append({
            "target_id": _target_id(pid, i, url),
            "title": s.get("title") or policy.get("title"),
            "publisher": s.get("publisher"),
            "publisher_type": s.get("publisher_type"),
            "url": url,
            "fallback_url": None,
            "official_api": None,
            "frequency": freq,
            "change_detection_method": method,
            "css_selector_hint": crawl.get("css_selector_hint"),
            "priority": s.get("priority") or "secondary",
            "used_by_items": [pid],
            "notes": notes,
            "auto_registered": True,
            "registered_at": date.today().isoformat(),
        })
    return out


# ── 등록 ─────────────────────────────────────────────────────
def register_policy(policy: dict) -> dict:
    """정책의 출처를 크롤 대상에 등록한다(멱등).

    이미 같은 URL 을 감시하는 타겟이 있으면 새 타겟을 만들지 않고
    그 타겟의 used_by_items 에 정책 ID 만 추가한다.

    반환: {"ok", "policy_id",
           "added":   새로 만든 타겟 id,
           "linked":  기존 타겟에 정책만 연결한 타겟 id,
           "updated": 같은 id 인데 출처 URL 이 바뀌어 내용을 갱신한 타겟 id,
           "unlinked": URL 갱신으로 공유가 풀린 연결 [{"target_id", "policy_ids"}],
           "skipped": 변화 없는 타겟 id,
           "manual_review": 추가·갱신된 것 중 수동 검토 방식인 타겟 id}
    실패: 오버레이 파일이 깨져 있으면 TargetsFileError(아무것도 쓰지 않는다).
    """
    pid = policy.get("id")
    if not pid:
        return {"ok": False, "error": "정책 id 없음"}

    merged = load_all()
    url_to_tid = {}
    for t in merged.get("targets", []):
        u = _norm_url(t.get("url"))
        if u:
            url_to_tid.setdefault(u, t.get("target_id"))

    local = load_local()
    local_ids = {t.get("target_id") for t in local["targets"]}
    local_by_id = {t.get("target_id"): t for t in local["targets"]}

    added, linked, skipped, updated, unlinked = [], [], [], [], []
    for cand in derive_targets(policy):
        url = cand["url"]
        existing_tid = url_to_tid.get(url)
        if existing_tid:
            # 같은 출처를 이미 감시 중 — 정책 연결만 추가
            cur = list((local["used_by_patch"].get(existing_tid) or []))
            already = pid in cur or pid in _used_by(merged, existing_tid)
            if already:
                skipped.append(existing_tid)
            else:
                cur.append(pid)
                local["used_by_patch"][existing_tid] = cur
                linked.append(existing_tid)
            continue
        if cand["target_id"] in local_ids:
            # target_id 는 auto_{정책}_{순번}_{호스트 첫 라벨} 이라, 정책 편집에서 같은 순번의
            # 출처를 같은 호스트의 다른 페이지로 바꾸면 id 는 그대로이고 URL 만 달라진다.
            # (URL 이 같은 경우는 위 url_to_tid 분기에서 이미 처리되므로, 여기 도달했다면
            #  저장된 타겟의 URL 과 다르다는 뜻이다.)
            # 이때 건너뛰면 옛 URL 을 계속 감시하게 되므로 저장된 타겟을 새 출처로 갱신한다.
            cur_t = local_by_id.get(cand["target_id"]) or {}
            old_url = _norm_url(cur_t.get("url"))
            if old_url == url:
                skipped.append(cand["target_id"])
                continue
            tid = cand["target_id"]
            # 출처 항목에서 파생되는 값(제목·발행처·URL·감지 방식·힌트·메모)은 새 출처 기준으로
            # 바꾼다. 감지 방식은 URL 로 추정하는 값이라(예: .pdf → pdf_hash) URL 과 함께
            # 바뀌어야 한다. target_id·최초 등록일은 유지한다
            # (id 를 바꾸면 이미 저장된 오버레이·스냅샷 경로와 어긋난다).
            # 이 타겟에 직접 연결돼 있던 다른 정책(used_by_items)도 옛 URL 을 이유로 연결된 것이다.
            # 새 URL 의 타겟에는 이 정책만 남기고, 나머지는 아래에서 끊긴 연결로 함께 보고한다.
            prev_used = [p for p in (cur_t.get("used_by_items") or []) if p != pid]
            new_t = dict(cand)
            new_t["used_by_items"] = [pid]
            new_t["registered_at"] = cur_t.get("registered_at") or cand["registered_at"]
            new_t["url_updated_at"] = date.today().isoformat()
            cur_t.clear()
            cur_t.update(new_t)
            # 옛 URL 을 이유로 이 타겟에 연결돼 있던 다른 정책은 더 이상 같은 페이지를 보지
            # 않는다. 연결을 남겨 두면 그 정책이 무관한 페이지의 변경으로 갱신 대상이 되므로
            # 연결을 끊고 결과에 보고한다(그 정책은 다음 등록 때 자기 타겟을 새로 얻는다).
            others = [p for p in (local["used_by_patch"].pop(tid, None) or []) if p != pid]
            others = sorted(set(others) | set(prev_used))
            if others:
                unlinked.append({"target_id": tid, "policy_ids": others})
            # 같은 호출 안의 뒤 순번 출처가 옛 URL 을 쓰는 경우(순번이 밀린 경우)에
            # 이 타겟으로 잘못 연결되지 않도록 URL 색인도 함께 고친다.
            if url_to_tid.get(old_url) == tid:
                del url_to_tid[old_url]
            url_to_tid[url] = tid
            updated.append(tid)
            continue
        local["targets"].append(cand)
        local_ids.add(cand["target_id"])
        url_to_tid[url] = cand["target_id"]
        added.append(cand["target_id"])

    if added or linked or updated:
        _save_local(local)

    # URL 이 바뀐 타겟의 비교 기준(baseline) 재설정 — 오버레이 저장이 끝난 뒤에 한다
    # (저장이 실패하면 타겟은 옛 URL 그대로이므로 기준도 그대로 두어야 한다).
    for tid in updated:
        _reset_baseline(tid)

    manual = [t["target_id"] for t in local["targets"]
              if (t.get("target_id") in added or t.get("target_id") in updated)
              and t.get("change_detection_method") == "manual_review"]
    return {"ok": True, "policy_id": pid, "added": added, "linked": linked,
            "updated": updated, "unlinked": unlinked,
            "skipped": skipped, "manual_review": manual}


def _reset_baseline(target_id: str) -> bool:
    """타겟의 스냅샷 디렉터리(비교 기준 해시·청크·최근 본문)를 지운다.

    스냅샷은 crawler/snapshots/{target_id}/ 에 target_id 기준으로 저장된다. 타겟의 URL 을
    바꾼 뒤에도 남겨 두면 옛 페이지의 해시·청크가 새 페이지의 비교 기준으로 쓰인다
    (감지 방식이 바뀌면 기준 파일 이름 자체가 달라 맞지도 않는다).
    지우면 그 타겟은 새로 등록된 타겟과 같은 상태가 된다 — 다음 크롤에서 "최초 스냅샷"
    으로 잡히거나, 콘솔의 크롤 등록 경로처럼 바로 기준 확정(--init-baseline)을 돌려
    새 URL 기준으로 다시 잡는다.

    반환: 디렉터리를 지웠으면 True, 없었거나 지우지 못했으면 False(예외를 올리지 않는다 —
          오버레이는 이미 저장됐고, 기준이 남아 있어도 다음 크롤에서 "변경"으로 한 번
          잡힌 뒤 반영 시점에 새 기준으로 바뀐다).
    """
    # target_id 는 _target_id() 가 만든 값(영소문자·숫자·밑줄)이지만, 오버레이 파일은
    # 사람이 고칠 수도 있으므로 경로 구분자가 든 값으로는 지우지 않는다.
    if not target_id or not re.fullmatch(r"[A-Za-z0-9_\-]+", target_id):
        return False
    snap = SNAPSHOTS_DIR / target_id
    if not snap.is_dir():
        return False
    try:
        shutil.rmtree(snap)
        logger.info("출처 URL 변경 — 비교 기준 재설정: %s", target_id)
        return True
    except OSError as e:
        logger.warning("비교 기준 재설정 실패(%s): %s", target_id, e)
        return False


def _used_by(merged: dict, tid: str) -> list:
    for t in merged.get("targets", []):
        if t.get("target_id") == tid:
            return list(t.get("used_by_items") or [])
    return []


def _save_local(local: dict):
    local["_note"] = ("콘솔에서 자동 등록된 크롤 타겟. "
                      "policy_db/crawl_targets.json(기준)과 병합되어 사용된다.")
    local["updated_at"] = date.today().isoformat()
    LOCAL_TARGETS.parent.mkdir(parents=True, exist_ok=True)
    # 임시 파일에 쓴 뒤 os.replace 로 교체한다. write_text 로 직접 쓰면 쓰는 도중 중단됐을 때
    # 잘린 JSON 이 남고, 그 파일은 다음 읽기에서 TargetsFileError 가 된다.
    ca.atomic_write_text(LOCAL_TARGETS, json.dumps(local, ensure_ascii=False, indent=2))


# ── 누락 점검 ────────────────────────────────────────────────
def covered_policy_ids() -> set:
    ids = set()
    for t in load_all().get("targets", []):
        for p in (t.get("used_by_items") or []):
            ids.add(p)
    return ids


def coverage_map() -> dict:
    """정책 ID → 감시 중인 타겟 수. 콘솔의 '크롤 미등록' 표시에 쓴다."""
    counts = {}
    for t in load_all().get("targets", []):
        for p in (t.get("used_by_items") or []):
            counts[p] = counts.get(p, 0) + 1
    return counts


def unregistered_policies() -> list:
    """items/ 에는 있으나 크롤 대상에 한 건도 등록되지 않은 정책 목록."""
    covered = covered_policy_ids()
    out = []
    for f in sorted(ca.ITEMS_DIR.glob(f"{ca.ITEM_GLOB_PREFIX}.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = d.get("id")
        if pid and pid not in covered:
            out.append({"policy_id": pid, "title": d.get("title"),
                        "sources": len(d.get("sources") or [])})
    return out
