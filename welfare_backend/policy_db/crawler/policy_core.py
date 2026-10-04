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


# 정책 ID 형식 검증 — 구현은 confirm_apply 에 하나만 두고 여기서는 그대로 노출한다
# (admin_router 가 path 파라미터 검증에 pc.validate_policy_id 를 쓴다).
validate_policy_id = ca.validate_policy_id


def _id_error(policy_id):
    """정책 ID 형식이 틀리면 오류 문구를, 맞으면 None 을 돌려준다.
    공개 함수들이 예외 대신 {"ok": False, "error": ...} 로 응답하기 위한 보조."""
    try:
        validate_policy_id(policy_id)
        return None
    except ValueError as e:
        return str(e)


def _files():
    # "B0*" 는 B100 이상을 놓친다 → 그러면 목록에서 빠지고 next_id() 가 계속 같은
    # 값(B100)을 돌려준다. 'B + 숫자' 로 시작하는 파일 전체를 본다.
    return sorted(ca.ITEMS_DIR.glob(f"{ca.ITEM_GLOB_PREFIX}.json"))


def _path(policy_id):
    """정책 ID → items/ 의 항목 파일 경로(없으면 None).

    실패: ID 형식이 틀리면 ValueError. ID 가 glob 패턴에 그대로 들어가므로,
          검증 없이는 "*" 가 다른 정책 파일에 매칭되고 "../" 가 items/ 밖을 가리킨다.
    """
    validate_policy_id(policy_id)
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
    err = _id_error(policy_id)
    if err:
        return {"error": err, "invalid_id": True}
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
    """DB 재적재를 실행하고 (성공 여부, 오류 문구) 를 돌려준다.

    ca._trigger_reingest 는 실패 시 예외를 올린다(종료 코드 0 아님·시간 초과 포함).
    여기서 잡아 호출자가 `reingested: False` + `reingest_error` 로 응답하게 한다.
    이 시점에 items/ 파일은 이미 저장돼 있으며, 재적재 실패로 되돌리지 않는다
    (재적재만 다시 실행하면 DB 가 파일 내용을 따라잡는다).
    """
    try:
        ca._trigger_reingest([policy_id])
        return True, None
    except Exception as e:
        return False, str(e)


def deactivate(policy_id):
    err = _id_error(policy_id)
    if err:
        return {"ok": False, "error": err}
    p = _path(policy_id)
    if not p:
        return {"ok": False, "error": f"정책 {policy_id} 없음"}
    d = _load(p)
    if d.get("active", True) is False:
        return {"ok": False, "error": "이미 비활성 상태"}
    d["active"] = False
    d["deactivated_at"] = datetime.now().isoformat(timespec="seconds")
    ca.atomic_write_text(p, json.dumps(d, ensure_ascii=False, indent=2))
    ok, err = _reingest(policy_id)
    return {"ok": True, "policy_id": policy_id, "active": False,
            "deactivated_at": d["deactivated_at"], "reingested": ok, "reingest_error": err}


def reactivate(policy_id):
    err = _id_error(policy_id)
    if err:
        return {"ok": False, "error": err}
    p = _path(policy_id)
    if not p:
        return {"ok": False, "error": f"정책 {policy_id} 없음"}
    d = _load(p)
    d["active"] = True
    d["deactivated_at"] = None
    ca.atomic_write_text(p, json.dumps(d, ensure_ascii=False, indent=2))
    ok, err = _reingest(policy_id)
    return {"ok": True, "policy_id": policy_id, "active": True, "reingested": ok, "reingest_error": err}


def update_policy(policy_id, data, reingest=True):
    err = _id_error(policy_id)
    if err:
        return {"ok": False, "error": err}
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
    # 임시 파일 → os.replace (쓰는 도중 중단돼도 항목 파일이 잘린 채 남지 않게)
    ca.atomic_write_text(p, json.dumps(data, ensure_ascii=False, indent=2))
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
    # 본문에 실려 온 id 는 그대로 파일 이름(f"{pid}_{slug}.json")이 된다.
    # 형식이 틀리면(경로 구분자·와일드카드 포함) 파일을 만들기 전에 거부한다.
    err = _id_error(pid)
    if err:
        return {"ok": False, "error": err}
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
    ca.atomic_write_text(fp, json.dumps(data, ensure_ascii=False, indent=2))
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
