# admin_router.py
# 관리자 콘솔 검토 큐 API + 페이지 (v1-1)
#   GET  /admin                          관리자 페이지(HTML)
#   GET  /admin/api/staging              대기 정책 목록
#   GET  /admin/api/staging/{policy_id}  단일 정책 필드 diff/검토 정보
#   POST /admin/api/staging/{id}/apply   선택 필드 반영(+자동 ingest)
#   POST /admin/api/staging/{id}/reject  staging 폐기
#   POST /admin/api/discovery/candidate/{cid}/enrich  후보 핵심정보 보강(검토 전용)
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
from database import AsyncSessionLocal, get_iitp_db, iitp_db_configured  # noqa: E402
import models  # noqa: E402
import scheduler as ops  # noqa: E402
import discovery_core as dc  # noqa: E402

router = APIRouter(tags=["admin"])


def _reingest_bg(policy_ids):
    """DB 부분 재적재(임베딩 재생성)를 백그라운드 스레드로 실행 — 동기 응답의 nginx 타임아웃 회피."""
    ids = [i for i in (policy_ids or []) if i]
    if not ids:
        return
    import threading
    threading.Thread(target=rc.trigger_reingest, args=(ids,), daemon=True).start()


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
    want_reingest = bool(payload.get("reingest", True))
    # 파일 반영은 동기(빠름), DB 재적재(임베딩 재생성)는 느려 nginx 프록시 타임아웃(HTML 504)을
    # 유발 → 재적재는 백그라운드 스레드로 분리하고 즉시 응답.
    r = rc.apply_selected(
        policy_id,
        payload.get("selected_keys", []),
        reingest=False,
    )
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    if want_reingest:
        _reingest_bg([policy_id])
        r["reingested"] = "running"
    return r


@router.post("/admin/api/staging/{policy_id}/reject")
def staging_reject(policy_id: str):
    r = rc.reject(policy_id)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    ids = r.get("reopen_query_ids")
    if ids:
        try:
            r["reopened"] = dc._reopen_queries(ids)  # 발굴 보강 반려 → 원 질의 재분류 대기로
        except Exception:
            pass
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


@router.post("/admin/api/policy/{policy_id}/crawl")
def policy_crawl(policy_id: str):
    return ops.run_crawl_policy(policy_id)


@router.post("/admin/api/policy/{policy_id}/init-baseline")
def policy_init_baseline(policy_id: str):
    return ops.run_init_baseline(policy_id)


# ── 미답변 질의 조회 (읽기 전용) ──
_FALLBACK_REASONS = ["low_similarity", "empty_result", "category_mismatch",
                     "explicit_no_info", "no_tool_call", "google_search",
                     "tool_error", "unknown"]


def _reflect_status(user_query, processed_at, cand_idx, excluded=False):
    """반영 구분.

    reflected = 신규 후보로 분류됨 / excluded = 발굴에서 정책 무관으로 걸러짐
    reviewed  = 발굴 처리만 됨(후보 아님) / pending = 미처리
    """
    info = cand_idx.get(user_query)
    if info:
        return "reflected", info
    if excluded:
        return "excluded", None
    if processed_at is not None:
        return "reviewed", None
    return "pending", None


def _ser_unresolved(r, cand_idx=None):
    fr = r.fallback_reason
    cand_idx = cand_idx or {}
    dpa = getattr(r, "discovery_processed_at", None)
    exc = bool(getattr(r, "discovery_excluded", False))
    status, info = _reflect_status(r.user_query, dpa, cand_idx, exc)
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
        "discovery_processed_at": dpa.isoformat() if dpa else None,
        "deleted_at": r.deleted_at.isoformat() if getattr(r, "deleted_at", None) else None,
        "reflected": status,
        "candidate_id": (info or {}).get("candidate_id"),
        "candidate_status": (info or {}).get("status"),
        "candidate_topic": (info or {}).get("topic"),
    }


@router.get("/admin/api/unresolved/summary")
async def unresolved_summary():
    live = models.UnresolvedQuery.deleted_at.is_(None)   # 삭제(숨김)분은 집계에서 제외
    async with AsyncSessionLocal() as db:
        total = (await db.execute(
            select(safunc.count()).select_from(models.UnresolvedQuery).where(live)
        )).scalar_one()
        deleted = (await db.execute(
            select(safunc.count()).select_from(models.UnresolvedQuery)
            .where(models.UnresolvedQuery.deleted_at.isnot(None))
        )).scalar_one()
        rows = (await db.execute(
            select(models.UnresolvedQuery.fallback_reason, safunc.count())
            .where(live).group_by(models.UnresolvedQuery.fallback_reason)
        )).all()
        by = {getattr(k, "value", str(k)): v for k, v in rows}
        qrows = (await db.execute(
            select(models.UnresolvedQuery.user_query,
                   models.UnresolvedQuery.discovery_processed_at,
                   models.UnresolvedQuery.discovery_excluded).where(live)
        )).all()
    cand_idx = dc.candidate_query_index()
    by_reflected = {"reflected": 0, "excluded": 0, "reviewed": 0, "pending": 0}
    for uq, dpa, exc in qrows:
        st, _ = _reflect_status(uq, dpa, cand_idx, bool(exc))
        by_reflected[st] = by_reflected.get(st, 0) + 1
    return {"total": total, "deleted": deleted, "by_reason": by,
            "reasons": _FALLBACK_REASONS, "by_reflected": by_reflected}


@router.get("/admin/api/unresolved")
async def unresolved_list(limit: int = 50, offset: int = 0,
                          fallback_reason: Optional[str] = None,
                          days: Optional[int] = None,
                          include_deleted: bool = False):
    lim = min(max(limit, 1), 200)
    off = max(offset, 0)
    conds = []
    if not include_deleted:
        conds.append(models.UnresolvedQuery.deleted_at.is_(None))
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
    cand_idx = dc.candidate_query_index()
    return {"total": total, "count": len(rows), "limit": lim, "offset": off,
            "items": [_ser_unresolved(r, cand_idx) for r in rows]}


# ── 미답변 질의 삭제(숨김) / 복원 ──
#
# 물리 삭제하지 않는 이유: 신규 후보가 query_ids 로 원 질의를 참조한다.
# 행을 지우면 이미 승인된 후보의 근거가 끊기고 되돌릴 수 없다.
# deleted_at 만 찍어 목록·집계·발굴 대상에서 제외한다.
async def _set_deleted(ids: list, value):
    ids = [int(i) for i in (ids or [])][:500]
    if not ids:
        raise HTTPException(status_code=400, detail="ids 가 비어 있습니다")
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(models.UnresolvedQuery).where(models.UnresolvedQuery.id.in_(ids))
        )).scalars().all()
        for r in rows:
            r.deleted_at = value
        await db.commit()
        return len(rows)


@router.post("/admin/api/unresolved/delete")
async def unresolved_delete(payload: dict = Body(...)):
    n = await _set_deleted(payload.get("ids"), datetime.now(timezone.utc))
    return {"ok": True, "deleted": n}


@router.post("/admin/api/unresolved/restore")
async def unresolved_restore(payload: dict = Body(...)):
    n = await _set_deleted(payload.get("ids"), None)
    return {"ok": True, "restored": n}


# ── 운영(크롤/백업 지금 실행 + 상태) ──
@router.get("/admin/api/ops/status")
def ops_status():
    return ops.get_status()


@router.post("/admin/api/ops/crawl/run")
def ops_crawl_run():
    return ops.run_crawl_now()


@router.post("/admin/api/ops/crawl/hashcheck")
def ops_crawl_hashcheck():
    return ops.run_crawl_hashcheck()


@router.post("/admin/api/ops/backup/run")
def ops_backup_run():
    return ops.run_backup_now()


@router.post("/admin/api/ops/init-baseline")
def ops_init_baseline(payload: dict = Body(default={})):
    return ops.run_init_baseline(payload.get("policy_id"))


# ── 신규 발굴 (Phase 5 Track B) ──
@router.post("/admin/api/discovery/run")
def discovery_run():
    return ops.run_discovery_now()


@router.get("/admin/api/discovery/candidates")
def discovery_candidates():
    return dc.list_candidates()


@router.get("/admin/api/discovery/candidate/{cid}")
def discovery_candidate(cid: str):
    r = dc.get_candidate(cid)
    if r.get("error"):
        raise HTTPException(status_code=404, detail=r["error"])
    return r


@router.post("/admin/api/discovery/candidate/{cid}/reject")
def discovery_reject(cid: str):
    r = dc.set_status(cid, "rejected")
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    return r


@router.post("/admin/api/discovery/candidate/{cid}/approve")
def discovery_approve(cid: str, payload: dict = Body(default={})):
    cand = dc.get_candidate(cid)
    if cand.get("error"):
        raise HTTPException(status_code=404, detail=cand["error"])
    # 이미 승인된 후보의 재승인 차단 — 중복 정책 등록 방지
    if cand.get("status") == "approved":
        raise HTTPException(status_code=400, detail={
            "error": "이미 승인된 후보입니다 (중복 등록 방지)",
            "policy_id": cand.get("approved_policy_id")})
    # 관리자가 편집한 초안을 보내면 그걸 사용(저장도 갱신), 아니면 저장된 초안
    draft = payload.get("draft_item") or cand.get("draft_item")
    if not draft:
        raise HTTPException(status_code=400, detail={"error": "초안 없음 — 승인 불가"})
    # LLM 초안의 흔한 구조 불일치(문자열/enum)를 스키마 형태로 보정해 등록 검증 실패를 방지.
    draft = dc._coerce_schema_shapes(draft)
    # 파일 등록은 동기, DB 재적재는 느려 nginx 타임아웃 → 백그라운드로 분리.
    r = pc.create_policy(draft, slug=(draft.get("title") or "new"), reingest=False)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r)
    dc.set_status(cid, "approved", policy_id=r.get("policy_id"))
    _reingest_bg([r.get("policy_id")])
    return {"ok": True, "policy_id": r.get("policy_id"), "candidate": cid, "reingested": "running"}


@router.post("/admin/api/discovery/candidate/{cid}/enrich")
def discovery_enrich(cid: str, payload: dict = Body(default={})):
    """승인 전 후보 핵심 운영 정보 보강(외부검색). status 변경/등록 없음 — 검토 전용."""
    cand = dc.get_candidate(cid)
    if cand.get("error"):
        raise HTTPException(status_code=404, detail=cand["error"])
    if cand.get("status") == "approved":
        raise HTTPException(status_code=400, detail={"error": "이미 승인된 후보입니다 (보강 불가)"})
    import threading
    # 보강 grounding 은 수십~수백 초 → 동기 응답 시 nginx 프록시 타임아웃(HTML 504) 발생.
    # 백그라운드로 돌리고 즉시 반환, 프론트는 후보의 enrich_status 를 폴링.
    threading.Thread(target=dc.enrich_candidate_run,
                     args=(cid, payload.get("draft_item")), daemon=True).start()
    return {"ok": True, "state": "running"}


# ---------------------------------------------------------------------------
# 이동편의 데이터 조회 (이슈 #165) — iitp_db read-only
# 01 v1.1.0 테이블 + sys_ext_api_info 를 콘솔에서 확인. SELECT 전용.
# ---------------------------------------------------------------------------
from fastapi import Depends  # noqa: E402
from sqlalchemy import text as _sqltext  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession  # noqa: E402

_MOBILITY_SOURCES = ("GBIS", "KORAIL_CONV", "KRNA_LIFT", "KOWSI_FACL", "TOUR_BF_API")

# 테이블 화이트리스트: key -> (표시명, count SQL, preview SQL)
_MOBILITY_TABLES = {
    "bus": (
        "버스 노선·배차 (tran_bus_route_info)",
        "SELECT count(*) FROM tran_bus_route_info WHERE del_yn='N'",
        "SELECT route_name, route_type_name, region_name, admin_name,"
        " start_station_name, end_station_name, peek_alloc, npeek_alloc,"
        " up_first_time, up_last_time, to_char(base_dt,'YYYY-MM-DD') AS base_dt"
        " FROM tran_bus_route_info WHERE del_yn='N'"
        " ORDER BY route_name LIMIT :limit",
    ),
    "station": (
        "역 편의 현황 (poi_station_access_status)",
        "SELECT count(*) FROM poi_station_access_status WHERE del_yn='N'",
        "SELECT stn_name, line_name, elevator_cnt, escalator_cnt,"
        " wheelchair_lift_cnt, dis_slope_yn, dis_toilet_yn, anyang_yn,"
        " to_char(base_dt,'YYYY-MM-DD') AS base_dt"
        " FROM poi_station_access_status WHERE del_yn='N'"
        " ORDER BY anyang_yn DESC, stn_name LIMIT :limit",
    ),
    "lift": (
        "휠체어리프트 상세 (poi_station_wheelchair_lift)",
        "SELECT count(*) FROM poi_station_wheelchair_lift WHERE del_yn='N'",
        "SELECT line_name, stn_name, mng_no, exit_no, detail_loc,"
        " start_floor, end_floor, to_char(base_dt,'YYYY-MM-DD') AS base_dt"
        " FROM poi_station_wheelchair_lift WHERE del_yn='N'"
        " ORDER BY line_name, stn_name, mng_no LIMIT :limit",
    ),
    "facility": (
        "건물 편의시설 (poi_facility_accessibility)",
        "SELECT count(*) FROM poi_facility_accessibility WHERE del_yn='N'",
        "SELECT facl_name, facl_type, addr, latitude, longitude,"
        " elevator_yn, dis_toilet_yn, dis_parking_yn,"
        " to_char(base_dt,'YYYY-MM-DD') AS base_dt"
        " FROM poi_facility_accessibility WHERE del_yn='N'"
        " ORDER BY facl_name LIMIT :limit",
    ),
    "tour": (
        "무장애 관광지 (poi_tour_bf_facility)",
        "SELECT count(*) FROM poi_tour_bf_facility WHERE del_yn='N'",
        "SELECT fclt_name, sido_code, toilet_yn, elevator_yn, parking_yn,"
        " slope_yn, wheelchair_rent_yn, audio_guide_yn, addr_road,"
        " to_char(base_dt,'YYYY-MM-DD') AS base_dt"
        " FROM poi_tour_bf_facility WHERE del_yn='N'"
        " ORDER BY fclt_id DESC LIMIT :limit",
    ),
}


@router.get("/admin/api/mobility/sources")
async def mobility_sources(db: _AsyncSession = Depends(get_iitp_db)):
    """소스별 연동 정보(sys_ext_api_info) + 대상 테이블 건수."""
    rows = (await db.execute(_sqltext(
        "SELECT ext_sys, if_name, to_char(latest_sync_time,'YYYY-MM-DD HH24:MI') AS latest_sync,"
        " coalesce(memo,'') AS memo, status"
        " FROM sys_ext_api_info WHERE del_yn='N' AND ext_sys = ANY(:src)"
        " ORDER BY ext_api_id"), {"src": list(_MOBILITY_SOURCES)})).mappings().all()
    counts = {}
    for key, (label, count_sql, _p) in _MOBILITY_TABLES.items():
        counts[key] = {
            "label": label,
            "count": (await db.execute(_sqltext(count_sql))).scalar() or 0,
        }
    anyang = (await db.execute(_sqltext(
        "SELECT count(*) FROM poi_station_access_status WHERE del_yn='N' AND anyang_yn='Y'"
    ))).scalar() or 0
    return {"configured": True, "sources": [dict(r) for r in rows],
            "tables": counts, "anyang_station_count": anyang}


@router.get("/admin/api/mobility/preview")
async def mobility_preview(table: str, limit: int = 50,
                           db: _AsyncSession = Depends(get_iitp_db)):
    """화이트리스트 테이블 미리보기 (read-only)."""
    if table not in _MOBILITY_TABLES:
        raise HTTPException(status_code=400, detail=f"unknown table: {table}")
    limit = max(1, min(int(limit), 200))
    label, _c, preview_sql = _MOBILITY_TABLES[table]
    result = await db.execute(_sqltext(preview_sql), {"limit": limit})
    cols = list(result.keys())
    rows = [list(r) for r in result.fetchall()]
    return {"label": label, "columns": cols, "rows": rows}


@router.get("/admin/api/mobility/status")
def mobility_status():
    """iitp_db 연결 설정 여부 (프론트 최초 진입용, DB 미접속에도 응답)."""
    return {"configured": iitp_db_configured()}


@router.get("/admin", response_class=HTMLResponse)
def admin_page():
    html = (Path(__file__).resolve().parent / "static" / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)
