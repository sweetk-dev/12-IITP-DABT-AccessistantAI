# crawler/policy_core.py
# 관리자 콘솔용 정책 관리(CRUD + soft delete) 로직.
#   - 목록/조회/편집/추가/비활성(soft delete)/재활성
#   - 모든 변경은 items/ 파일에 기록 후 ingest_sync 1회 재실행으로 DB 반영
#     (비활성: ingest 가 청크 삭제 → 검색/답변에서 제외)
#   - print/input 없음(웹 API 용). confirm_apply 의 상수/헬퍼 재사용.
import json
import re
from datetime import datetime
from pathlib import Path

import jsonschema

try:
    from . import confirm_apply as ca
except ImportError:
    import confirm_apply as ca  # type: ignore


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
    for f in _files():
        try:
            d = _load(f)
        except Exception:
            continue
        out.append({
            "policy_id": d.get("id"),
            "title": d.get("title"),
            "category": d.get("category"),
            "benefit_type": d.get("benefit_type"),
            "active": d.get("active", True),
            "deactivated_at": d.get("deactivated_at"),
            "version": d.get("version"),
            "file": f.name,
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
    res = {"ok": True, "policy_id": pid, "file": fp.name,
           "note": "crawl_targets 출처 등록은 별도 수동(필요 시)"}
    if reingest:
        ok, err = _reingest(pid)
        res["reingested"] = ok; res["reingest_error"] = err
    return res
