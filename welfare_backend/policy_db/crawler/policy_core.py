# crawler/policy_core.py
# 관리자 콘솔용 정책 관리(CRUD + soft delete) 로직.
#   - 목록/조회/편집/추가/비활성(soft delete)/재활성
#   - 모든 변경은 items/ 파일에 기록 후 ingest_sync 1회 재실행으로 DB 반영
#     (비활성: ingest 가 청크 삭제 → 검색/답변에서 제외)
#   - print/input 없음(웹 API 용). confirm_apply 의 상수/헬퍼 재사용.
import json
import re
from datetime import datetime

import jsonschema

try:
    from . import confirm_apply as ca
    from . import target_sync as ts
except ImportError:
    import confirm_apply as ca  # type: ignore
    import target_sync as ts  # type: ignore


def _files():
    return sorted(ca.ITEMS_DIR.glob("B0*.json"))


def _path(policy_id):
    fs = list(ca.ITEMS_DIR.glob(f"{policy_id}_*.json"))
    return fs[0] if fs else None


def _load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def _validate(data):
    if not ca.SCHEMA.exists():
        return []
    v = jsonschema.Draft7Validator(json.loads(ca.SCHEMA.read_text(encoding="utf-8")))
    return [f"{list(e.path)}: {e.message[:120]}" for e in v.iter_errors(data)][:8]


def list_policies():
    out = []
    try:
        cov = ts.coverage_map()
    except Exception:
        cov = {}
    for f in _files():
        try:
            d = _load(f)
        except Exception:
            continue
        # 근거 법령 매핑 요약 (#238) — 콘솔 목록에서 법령ID 확인용
        legal = [{
            "name": x.get("name"),
            "article": x.get("article"),
            "law_id": x.get("law_id"),
            "law_serial_no": x.get("law_serial_no"),
            "mapping_status": x.get("mapping_status"),
        } for x in (d.get("legal_basis") or [])]
        out.append({
            "policy_id": d.get("id"),
            "title": d.get("title"),
            "category": d.get("category"),
            "benefit_type": d.get("benefit_type"),
            "active": d.get("active", True),
            "deactivated_at": d.get("deactivated_at"),
            "version": d.get("version"),
            "file": f.name,
            # 이 정책의 출처를 감시 중인 크롤 타겟 수. 0 이면 갱신 사각지대.
            "crawl_targets": cov.get(d.get("id"), 0),
            "legal_basis": legal,
            "last_applied_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return sorted(out, key=lambda x: (x["policy_id"] or ""))


def get_policy(policy_id):
    p = _path(policy_id)
    if not p:
        return {"error": f"정책 {policy_id} 없음"}
    return _load(p)


def next_id():
    mx = 0
    for f in _files():
        m = re.match(r"B0*(\d+)", f.name)
        if m:
            mx = max(mx, int(m.group(1)))
    return f"B{mx + 1:03d}"


def _reingest(policy_id):
    try:
        ca._trigger_reingest([policy_id])
        return True, None
    except Exception as e:
        return False, str(e)


def deactivate(policy_id):
    p = _path(policy_id)
    if not p:
        return {"ok": False, "error": f"정책 {policy_id} 없음"}
    d = _load(p)
    if d.get("active", True) is False:
        return {"ok": False, "error": "이미 비활성 상태"}
    d["active"] = False
    d["deactivated_at"] = datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    ok, err = _reingest(policy_id)
    return {"ok": True, "policy_id": policy_id, "active": False,
            "deactivated_at": d["deactivated_at"], "reingested": ok, "reingest_error": err}


def reactivate(policy_id):
    p = _path(policy_id)
    if not p:
        return {"ok": False, "error": f"정책 {policy_id} 없음"}
    d = _load(p)
    d["active"] = True
    d["deactivated_at"] = None
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    ok, err = _reingest(policy_id)
    return {"ok": True, "policy_id": policy_id, "active": True, "reingested": ok, "reingest_error": err}


def update_policy(policy_id, data, reingest=True):
    p = _path(policy_id)
    if not p:
        return {"ok": False, "error": f"정책 {policy_id} 없음"}
    if data.get("id") != policy_id:
        return {"ok": False, "error": f"id 불일치(본문 {data.get('id')} != {policy_id})"}
    errs = _validate(data)
    if errs:
        return {"ok": False, "error": "schema 검증 실패", "details": errs}
    existing = _load(p)
    # 회귀 가드(편집도 조용한 손실 방지)
    reg = ca._regression_check(existing, data)
    if reg:
        return {"ok": False, "error": "회귀 가드 차단", "details": reg}
    # 백업 후 저장
    ca.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(p, ca.BACKUPS_DIR / f"{p.stem}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak.json")
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    res = {"ok": True, "policy_id": policy_id}
    # 편집으로 출처가 추가됐을 수 있으므로 등록도 다시 맞춘다(멱등).
    try:
        res["crawl_targets"] = ts.register_policy(data)
    except Exception as e:
        res["crawl_targets"] = {"ok": False, "error": str(e)}
    if reingest:
        ok, err = _reingest(policy_id)
        res["reingested"] = ok; res["reingest_error"] = err
    return res


def create_policy(data, slug=None, reingest=True):
    pid = data.get("id") or next_id()
    data["id"] = pid
    if _path(pid):
        return {"ok": False, "error": f"이미 존재하는 정책 id: {pid}"}
    if "active" not in data:
        data["active"] = True
    errs = _validate(data)
    if errs:
        return {"ok": False, "error": "schema 검증 실패", "details": errs}
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", (slug or "custom")).strip("_") or "custom"
    fp = ca.ITEMS_DIR / f"{pid}_{slug}.json"
    ca.ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    res = {"ok": True, "policy_id": pid, "file": fp.name}
    # 출처를 크롤 대상에 함께 등록한다. 이 단계가 없으면 새로 만든 정책은
    # 이후 어떤 변경 감지도 받지 못하고 만들어진 시점에 고정된다.
    try:
        res["crawl_targets"] = ts.register_policy(data)
    except Exception as e:
        res["crawl_targets"] = {"ok": False, "error": str(e)}
    if reingest:
        ok, err = _reingest(pid)
        res["reingested"] = ok; res["reingest_error"] = err
    return res
