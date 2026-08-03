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
from datetime import date
from urllib.parse import urlparse

try:
    from . import confirm_apply as ca
except ImportError:
    import confirm_apply as ca  # type: ignore

logger = logging.getLogger(__name__)

BASE_TARGETS = ca.ROOT / "crawl_targets.json"
LOCAL_TARGETS = ca.DATA_ROOT / "crawl_targets.local.json"

# crawl_targets.json 의 change_detection_methods 키와 일치해야 한다.
VALID_METHODS = {"page_hash", "pdf_hash", "last_modified_field",
                 "css_selector_text", "manual_review"}
VALID_FREQUENCIES = {"daily", "weekly", "monthly", "quarterly", "on_demand"}

# 이 도메인들은 '시행일/수정일' 필드가 안정적으로 노출되어 해시보다 정확하다.
_LAST_MODIFIED_HOSTS = ("law.go.kr", "easylaw.go.kr")


def _load_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


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

    added, linked, skipped = [], [], []
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
            skipped.append(cand["target_id"])
            continue
        local["targets"].append(cand)
        local_ids.add(cand["target_id"])
        url_to_tid[url] = cand["target_id"]
        added.append(cand["target_id"])

    if added or linked:
        _save_local(local)

    manual = [t["target_id"] for t in local["targets"]
              if t.get("target_id") in added and t.get("change_detection_method") == "manual_review"]
    return {"ok": True, "policy_id": pid, "added": added, "linked": linked,
            "skipped": skipped, "manual_review": manual}


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
    LOCAL_TARGETS.write_text(json.dumps(local, ensure_ascii=False, indent=2), encoding="utf-8")


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
    for f in sorted(ca.ITEMS_DIR.glob("B0*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = d.get("id")
        if pid and pid not in covered:
            out.append({"policy_id": pid, "title": d.get("title"),
                        "sources": len(d.get("sources") or [])})
    return out
