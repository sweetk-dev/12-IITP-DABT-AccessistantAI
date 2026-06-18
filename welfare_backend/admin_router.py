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

_PDB = Path(__file__).resolve().parent / "policy_db"
if str(_PDB) not in sys.path:
    sys.path.insert(0, str(_PDB))
from crawler import review_core as rc  # noqa: E402

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


@router.get("/admin", response_class=HTMLResponse)
def admin_page():
    html = (Path(__file__).resolve().parent / "static" / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)
