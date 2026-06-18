# admin_router.py
# 관리자 콘솔 검토 큐 API + 페이지 (v1-1)
#   GET  /admin                          관리자 페이지(HTML)
#   GET  /admin/api/staging              대기 정책 목록
#   GET  /admin/api/staging/{policy_id}  단일 정책 필드 diff/검토 정보
#   POST /admin/api/staging/{id}/apply   선택 필드 반영(+자동 ingest)
#   POST /admin/api/staging/{id}/reject  staging 폐기
import sys
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import select, desc, func as safunc

_PDB = Path(__file__).resolve().parent / "policy_db"
if str(_PDB) not in sys.path:
    sys.path.insert(0, str(_PDB))
from crawler import review_core as rc  # noqa: E402
from crawler import policy_core as pc  # noqa: E402
from database import AsyncSessionLocal  # noqa: E402
import models  # noqa: E402
import scheduler as ops  # noqa: E402

router = APIRouter(tags=["admin"])


@router.get("/admin/api/staging")
def staging_list():
    return rc.list_pending()


@router.get("/admin/api/staging/{policy_id}")
def staging_review(policy_id: str):
    r = rc.get_review(policy_id)
    if r.get("error"):
        raise HTTPException(status_code=404, detail=r["error"])
    return r


@router.post("/admin/api/staging/{policy_id}/apply")
def staging_apply(policy_id: str, payload: dict = Body(default={})):
    r = rc.apply_selected(
        policy_id,
        payload.get("selected_keys", []),
        bool(payload.get("reingest", True)),
    )
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    return r


@router.post("/admin/api/staging/{policy_id}/reject")
def staging_reject(policy_id: str):
    r = rc.reject(policy_id)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    return r


@router.post("/admin/api/staging/{policy_id}/triage")
def staging_triage(policy_id: str, payload: dict = Body(default={})):
    r = rc.set_triage(policy_id, priority=payload.get("priority"),
                      hold=payload.get("hold"), note=payload.get("note"))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    return r


# ── 정책 관리 (CRUD + soft delete) ──
@router.get("/admin/api/policies")
def policies_list():
    return pc.list_policies()


@router.get("/admin/api/policy/{policy_id}")
def policy_get(policy_id: str):
    r = pc.get_policy(policy_id)
    if r.get("error"):
        raise HTTPException(status_code=404, detail=r["error"])
    return r


@router.put("/admin/api/policy/{policy_id}")
def policy_update(policy_id: str, payload: dict = Body(...)):
    r = pc.update_policy(policy_id, payload)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    return r


@router.post("/admin/api/policy")
def policy_create(payload: dict = Body(...)):
    data = payload.get("data") or payload
    r = pc.create_policy(data, slug=payload.get("slug"))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    return r


@router.post("/admin/api/policy/{policy_id}/deactivate")
def policy_deactivate(policy_id: str):
    r = pc.deactivate(policy_id)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    return r


@router.post("/admin/api/policy/{policy_id}/reactivate")
def policy_reactivate(policy_id: str):
    r = pc.reactivate(policy_id)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    return r


# ── 미답변 질의 조회 (읽기 전용) ──
_FALLBACK_REASONS = ["low_similarity", "empty_result", "category_mismatch",
                     "explicit_no_info", "google_search", "tool_error", "unknown"]


def _ser_unresolved(r):
    fr = r.fallback_reason
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "user_query": r.user_query,
        "fallback_reason": getattr(fr, "value", str(fr)),
        "ai_final_answer": (r.ai_final_answer or "")[:600],
        "session_id": str(r.session_id),
        "intent_group_id": str(r.intent_group_id),
        "turn_in_group": r.turn_in_group,
        "embedded": r.embedded_at is not None,
        "has_grounding": bool(r.grounding_info),
    }


@router.get("/admin/api/unresolved/summary")
async def unresolved_summary():
    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(safunc.count()).select_from(models.UnresolvedQuery))).scalar_one()
        rows = (await db.execute(
            select(models.UnresolvedQuery.fallback_reason, safunc.count())
            .group_by(models.UnresolvedQuery.fallback_reason)
        )).all()
        by = {getattr(k, "value", str(k)): v for k, v in rows}
    return {"total": total, "by_reason": by, "reasons": _FALLBACK_REASONS}


@router.get("/admin/api/unresolved")
async def unresolved_list(limit: int = 50, offset: int = 0,
                          fallback_reason: Optional[str] = None,
                          days: Optional[int] = None):
    lim = min(max(limit, 1), 200)
    off = max(offset, 0)
    conds = []
    if fallback_reason:
        try:
            conds.append(models.UnresolvedQuery.fallback_reason == models.FallbackReason(fallback_reason))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"잘못된 fallback_reason: {fallback_reason}")
    if days:
        conds.append(models.UnresolvedQuery.created_at >= datetime.now(timezone.utc) - timedelta(days=int(days)))
    async with AsyncSessionLocal() as db:
        base = select(models.UnresolvedQuery)
        if conds:
            base = base.where(*conds)
        total = (await db.execute(select(safunc.count()).select_from(base.subquery()))).scalar_one()
        rows = (await db.execute(
            base.order_by(desc(models.UnresolvedQuery.created_at)).limit(lim).offset(off)
        )).scalars().all()
    return {"total": total, "count": len(rows), "limit": lim, "offset": off,
            "items": [_ser_unresolved(r) for r in rows]}


# ── 운영(크롤/백업 지금 실행 + 상태) ──
@router.get("/admin/api/ops/status")
def ops_status():
    return ops.get_status()


@router.post("/admin/api/ops/crawl/run")
def ops_crawl_run():
    return ops.run_crawl_now()


@router.post("/admin/api/ops/backup/run")
def ops_backup_run():
    return ops.run_backup_now()


@router.get("/admin", response_class=HTMLResponse)
def admin_page():
    html = (Path(__file__).resolve().parent / "static" / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)
