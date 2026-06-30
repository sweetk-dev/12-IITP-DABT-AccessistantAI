# crawler/review_core.py
# 관리자 콘솔용 검토 큐 로직 — CLI(confirm_apply)와 동일 안전장치를 함수로 노출.
#   - 필드(최상위 키) 단위 선택 승인
#   - 회귀 가드 + 스키마 검증 + 자동 백업 + baseline 전진 + (옵션) ingest_sync 재적재
#   - print/input 없음(웹 API 용). confirm_apply 의 상수/헬퍼를 재사용.
import json
import shutil
from datetime import datetime
from pathlib import Path

import jsonschema

try:
    from . import confirm_apply as ca
except ImportError:  # 스크립트 직접 실행 폴백
    import confirm_apply as ca  # type: ignore

# 자동 동반 반영되는 메타 키(내용 변경 선택 시 함께 갱신)
META_KEYS = {"version", "last_verified"}


def _existing_path(policy_id):
    files = list(ca.ITEMS_DIR.glob(f"{policy_id}_*.json"))
    return files[0] if files else None


def _sidecar(staged: Path, ext: str) -> Path:
    return staged.parent / staged.name.replace(".staged.json", ext)


PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}


def _read_triage(staged: Path) -> dict:
    tf = _sidecar(staged, ".triage.json")
    if tf.exists():
        try:
            return json.loads(tf.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


def _read_disc(staged: Path) -> dict:
    df = _sidecar(staged, ".disc.json")
    if df.exists():
        try:
            return json.loads(df.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


def get_triage(policy_id):
    found = ca._find_staged(policy_id)
    return _read_triage(found[0]) if found else {}


def set_triage(policy_id, priority=None, hold=None, note=None):
    found = ca._find_staged(policy_id)
    if not found:
        return {"ok": False, "error": "staging 대기 항목 없음"}
    cur = _read_triage(found[0])
    if priority is not None:
        cur["priority"] = priority if priority in ("high", "medium", "low") else None
    if hold is not None:
        cur["hold"] = bool(hold)
    if note is not None:
        cur["note"] = note
    cur["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _sidecar(found[0], ".triage.json").write_text(
        json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, **cur}


def get_review(policy_id, _staged=None):
    """단일 정책의 staging 검토 정보(필드 diff + 검토/출처 + 회귀/스키마 상태)."""
    if _staged is not None:
        staged = _staged
    else:
        found = ca._find_staged(policy_id)
        staged = found[0] if found else None
    if not staged:
        return {"policy_id": policy_id, "error": "staging 대기 항목 없음"}

    new_data = json.loads(staged.read_text(encoding="utf-8"))
    ep = _existing_path(policy_id)
    existing = json.loads(ep.read_text(encoding="utf-8")) if ep else {}
    title = new_data.get("title") or existing.get("title")

    diffs = []
    for k in sorted(set(existing) | set(new_data)):
        ov, nv = existing.get(k), new_data.get(k)
        if ov == nv:
            continue
        diffs.append({"key": k, "old": ov, "new": nv, "is_meta": k in META_KEYS})

    review = []
    rf = _sidecar(staged, ".review.json")
    if rf.exists():
        try:
            review = json.loads(rf.read_text(encoding="utf-8")) or []
        except Exception:
            review = []

    sources_changed = []
    sf = _sidecar(staged, ".sources.json")
    if sf.exists():
        try:
            sources_changed = [s.get("target_id") for s in (json.loads(sf.read_text(encoding="utf-8")) or [])]
        except Exception:
            pass

    regression = ca._regression_check(existing, new_data) if existing else []
    schema_ok, schema_errors = True, []
    if ca.SCHEMA.exists():
        v = jsonschema.Draft7Validator(json.loads(ca.SCHEMA.read_text(encoding="utf-8")))
        errs = list(v.iter_errors(new_data))
        schema_ok = not errs
        schema_errors = [f"{list(e.path)}: {e.message[:120]}" for e in errs[:5]]

    return {
        "policy_id": policy_id, "title": title, "staged_name": staged.name,
        "is_new": ep is None,
        "diffs": diffs, "review": review, "sources_changed": sources_changed,
        "regression": regression, "schema_ok": schema_ok, "schema_errors": schema_errors,
        "triage": _read_triage(staged),
        "discovery": _read_disc(staged),
    }


def list_pending():
    """staging 대기 정책 목록(정책별 최신 1건)."""
    out = []
    if not ca.STAGING_DIR.exists():
        return out
    files = sorted(ca.STAGING_DIR.glob("B0*.staged.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    seen = set()
    for f in files:
        pid = f.name.split("_")[0]
        if pid in seen:
            continue
        seen.add(pid)
        info = get_review(pid, _staged=f)
        if info.get("error"):
            continue
        tri = info.get("triage", {}) or {}
        out.append({
            "policy_id": pid,
            "title": info.get("title"),
            "staged_name": f.name,
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
            "n_changes": len([d for d in info["diffs"] if not d["is_meta"]]),
            "review_count": len(info["review"]),
            "regression": info["regression"],
            "schema_ok": info["schema_ok"],
            "is_new": info["is_new"],
            "sources_changed": info["sources_changed"],
            "priority": tri.get("priority"),
            "hold": bool(tri.get("hold")),
            "note": tri.get("note"),
        })
    # 정렬: 비보류 우선 → 우선순위(high→low) → 최신순
    out.sort(key=lambda x: x["mtime"], reverse=True)
    out.sort(key=lambda x: (1 if x["hold"] else 0, PRIORITY_RANK.get(x["priority"], 3)))
    return out


def apply_selected(policy_id, selected_keys, reingest=True):
    """선택한 최상위 키만 기존 항목에 반영. 안전장치 모두 통과해야 기록."""
    found = ca._find_staged(policy_id)
    if not found:
        return {"ok": False, "error": "staging 대기 항목 없음"}
    staged = found[0]
    new_data = json.loads(staged.read_text(encoding="utf-8"))
    ep = _existing_path(policy_id)
    if not ep:
        return {"ok": False, "error": "기존 항목 없음 — 신규 정책은 정책 추가 기능 사용"}
    existing = json.loads(ep.read_text(encoding="utf-8"))

    diff_keys = {k for k in set(existing) | set(new_data) if existing.get(k) != new_data.get(k)}
    sel = set(selected_keys or []) & diff_keys
    if not sel:
        return {"ok": False, "error": "선택된 변경 없음"}
    apply_keys = set(sel) | {mk for mk in META_KEYS if mk in diff_keys}

    merged = json.loads(json.dumps(existing, ensure_ascii=False))  # deepcopy
    for k in apply_keys:
        if k in new_data:
            merged[k] = new_data[k]

    if ca.SCHEMA.exists():
        v = jsonschema.Draft7Validator(json.loads(ca.SCHEMA.read_text(encoding="utf-8")))
        errs = list(v.iter_errors(merged))
        if errs:
            return {"ok": False, "error": "schema 검증 실패",
                    "details": [f"{list(e.path)}: {e.message[:120]}" for e in errs[:5]]}

    reg = ca._regression_check(existing, merged)
    if reg:
        return {"ok": False, "error": "회귀 가드 차단", "details": reg}

    ca.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_name = f"{ep.stem}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak.json"
    shutil.copy2(ep, ca.BACKUPS_DIR / backup_name)
    ep.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    ca._advance_baselines(staged)
    applied = ca.STAGING_DIR / ".applied"
    applied.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), str(applied / staged.name))
    for ext in (".sources.json", ".review.json", ".triage.json"):
        side = _sidecar(staged, ext)
        if side.exists():
            shutil.move(str(side), str(applied / side.name))

    result = {"ok": True, "applied_keys": sorted(apply_keys), "backup": backup_name}
    if reingest:
        try:
            ca._trigger_reingest([policy_id])
            result["reingested"] = True
        except Exception as e:
            result["reingested"] = False
            result["reingest_error"] = str(e)
    return result


def reject(policy_id):
    found = ca._find_staged(policy_id)
    if not found:
        return {"ok": False, "error": "staging 대기 항목 없음"}
    staged = found[0]
    disc = _read_disc(staged)
    reopen_ids = disc.get("query_ids") or []
    rej = ca.STAGING_DIR / ".rejected"
    rej.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), str(rej / staged.name))
    for ext in (".sources.json", ".review.json", ".triage.json", ".disc.json"):
        side = _sidecar(staged, ext)
        if side.exists():
            shutil.move(str(side), str(rej / side.name))
    return {"ok": True, "policy_id": policy_id, "reopen_query_ids": reopen_ids}
