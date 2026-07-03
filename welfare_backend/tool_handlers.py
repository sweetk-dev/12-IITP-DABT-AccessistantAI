# tool_handlers.py
# Gemini Live API 의 Function Calling 핸들러.
# main.py 의 5종 FastAPI 엔드포인트와 동일 로직을 "일반 async 함수" 형태로 재구현해
# Gemini SDK 가 직접 호출 가능하도록 합니다.
#
# FastAPI 엔드포인트는 Depends(get_db) 의존성 주입 때문에 Gemini Live tools 에
# 그대로 넣을 수 없어, 같은 DB 세션 헬퍼를 받는 일반 함수로 분리했습니다.
import logging
import re
from typing import Optional

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal
import models

logger = logging.getLogger(__name__)


async def _with_session(handler):
    """도구 호출 1회마다 새 DB 세션을 빌려준다 (WebSocket 라이프사이클과 분리)."""
    async with AsyncSessionLocal() as db:
        return await handler(db)


# ─────────────────────────────────────────────────────────────
# 도구 #1
# ─────────────────────────────────────────────────────────────
def _top_sources_from_fd(fd, n: int = 3) -> list:
    """정책 full_data 의 sources 에서 화면 표시용 출처(기관명+URL) top-N 추출."""
    out, seen = [], set()
    for sc in (fd or {}).get("sources", []) or []:
        if not isinstance(sc, dict):
            continue
        url = (sc.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"publisher": sc.get("publisher") or "출처", "url": url, "priority": sc.get("priority")})
        if len(out) >= n:
            break
    return out


async def tool_search_policies_by_metadata(
    category: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 5,
) -> dict:
    """카테고리·중증도 메타데이터로 정책 후보를 빠르게 좁힙니다.

    Args:
        category: 정책 카테고리. 교통/통신/의료/세제/소득지원/활동지원/문화·체육/보육·교육/주거/공공시설/기타
        severity: 장애 정도. '심한 장애(중증)' 또는 '심하지 않은 장애(경증)'
        limit: 최대 반환 개수 (1~20)
    """
    async def run(db: AsyncSession):
        stmt = select(models.WelfarePolicy).where(models.WelfarePolicy.active.isnot(False))
        if category:
            stmt = stmt.where(models.WelfarePolicy.category == category)
        if severity:
            stmt = stmt.where(models.WelfarePolicy.severity_levels.contains([severity]))
        stmt = stmt.limit(min(max(limit, 1), 20))
        rows = (await db.execute(stmt)).scalars().all()
        return {
            "matched_count": len(rows),
            "sources_top3": _top_sources_from_fd(rows[0].full_data) if rows else [],
            "results": [
                {
                    "policy_id": p.id,
                    "title": p.title,
                    "summary": p.short_summary,
                    "category": p.category,
                    "benefit_type": p.benefit_type,
                    "severity_levels": p.severity_levels or [],
                    "has_companion_benefit": p.has_companion_benefit,
                    "has_income_criteria": p.has_income_criteria,
                }
                for p in rows
            ],
        }
    return await _with_session(run)


# ─────────────────────────────────────────────────────────────
# 도구 #2 (벡터 검색)
# ─────────────────────────────────────────────────────────────
def _kw_tokens(q: str) -> list:
    toks = [t for t in re.split(r"[\s,./?!()·:;]+", q or "") if len(t) >= 2]
    return toks[:6] or ([q] if q else [])


async def _keyword_text_search(query: str, top_k: int = 5) -> dict:
    """임베딩(벡터) 사용 불가 시(예: Gemini 크레딧 소진) 키워드 ILIKE 텍스트 검색 폴백."""
    toks = _kw_tokens(query)

    async def run(db: AsyncSession):
        conds = []
        for t in toks:
            like = f"%{t}%"
            conds.append(models.WelfarePolicy.title.ilike(like))
            conds.append(models.WelfarePolicy.short_summary.ilike(like))
        stmt = (select(models.WelfarePolicy)
                .where(models.WelfarePolicy.active.isnot(False))
                .where(or_(*conds))
                .limit(min(max(top_k, 1), 15)))
        rows = (await db.execute(stmt)).scalars().all()
        if not rows and toks:
            cconds = [models.PolicyChunk.content.ilike(f"%{t}%") for t in toks]
            cstmt = (select(models.WelfarePolicy)
                     .join(models.PolicyChunk, models.PolicyChunk.policy_id == models.WelfarePolicy.id)
                     .where(models.WelfarePolicy.active.isnot(False))
                     .where(or_(*cconds)).distinct()
                     .limit(min(max(top_k, 1), 15)))
            rows = (await db.execute(cstmt)).scalars().all()
        return {
            "query": query,
            "search_mode": "keyword_text_fallback",
            "ai_instruction": "벡터 검색을 쓸 수 없어 키워드 매칭으로 찾은 결과입니다. 관련성이 낮을 수 있으니 확실치 않으면 보건복지부 129 안내를 덧붙이세요.",
            "sources_top3": _top_sources_from_fd(rows[0].full_data) if rows else [],
            "results": [
                {
                    "policy_id": p.id,
                    "title": p.title,
                    "category": p.category,
                    "policy_summary": p.short_summary,
                    "matched_chunk_type": "text_match",
                    "matched_content": p.short_summary,
                }
                for p in rows
            ],
        }
    return await _with_session(run)


async def tool_search_by_keyword(query: str, top_k: int = 5, *, embed_fn) -> dict:
    """자연어 질문을 768차원 벡터로 변환한 뒤 모든 청크에서 의미적으로 가까운 결과를 찾습니다.

    Args:
        query: 자연어 질문
        top_k: 반환 개수
        embed_fn: 임베딩 함수 (main.py 의 _embed)
    """
    try:
        qvec = embed_fn(query)
    except Exception as e:
        logger.warning("임베딩 실패 — 키워드 텍스트 검색 폴백: %s", str(e)[:120])
        return await _keyword_text_search(query, top_k)

    async def run(db: AsyncSession):
        stmt = (
            select(
                models.PolicyChunk.policy_id,
                models.PolicyChunk.chunk_type,
                models.PolicyChunk.content,
                models.WelfarePolicy.title,
                models.WelfarePolicy.short_summary,
                models.WelfarePolicy.category,
                models.WelfarePolicy.full_data,
            )
            .join(models.WelfarePolicy, models.PolicyChunk.policy_id == models.WelfarePolicy.id)
            .order_by(models.PolicyChunk.embedding.cosine_distance(qvec))
            .limit(min(max(top_k, 1), 15))
        )
        rows = (await db.execute(stmt)).all()
        return {
            "query": query,
            "sources_top3": _top_sources_from_fd(rows[0].full_data) if rows else [],
            "results": [
                {
                    "policy_id": r.policy_id,
                    "title": r.title,
                    "category": r.category,
                    "policy_summary": r.short_summary,
                    "matched_chunk_type": r.chunk_type,
                    "matched_content": r.content,
                }
                for r in rows
            ],
        }
    return await _with_session(run)


# ─────────────────────────────────────────────────────────────
# 도구 #3
# ─────────────────────────────────────────────────────────────
async def tool_get_policy_details(policy_id: str) -> dict:
    """특정 정책의 전체 정보(지원 금액·신청 방법·출처)를 한 번에 반환합니다.

    Args:
        policy_id: 정책 ID (예: 'B001')
    """
    async def run(db: AsyncSession):
        p = (await db.execute(
            select(models.WelfarePolicy).where(models.WelfarePolicy.id == policy_id, models.WelfarePolicy.active.isnot(False))
        )).scalar_one_or_none()
        if not p:
            return {"error": f"정책 {policy_id} 없음"}
        fd = p.full_data or {}
        sources_top3 = (fd.get("sources") or [])[:3]
        return {
            "policy_id": p.id,
            "title": p.title,
            "summary": p.short_summary,
            "supported_amount": fd.get("supported_amount"),
            "how_to_use": fd.get("how_to_use"),
            "application": fd.get("application"),
            "key_contact": (fd.get("contact") or [None])[0],
            "sources_top3": [
                {"publisher": s.get("publisher"), "url": s.get("url"), "priority": s.get("priority")}
                for s in sources_top3
            ],
        }
    return await _with_session(run)


# ─────────────────────────────────────────────────────────────
# 도구 #4
# ─────────────────────────────────────────────────────────────
async def tool_check_eligibility_criteria(policy_id: str) -> dict:
    """특정 정책의 자격 요건을 구조화 메타와 본문 청크로 동시에 반환.

    Args:
        policy_id: 정책 ID (예: 'B001')
    """
    async def run(db: AsyncSession):
        p = (await db.execute(
            select(models.WelfarePolicy).where(models.WelfarePolicy.id == policy_id, models.WelfarePolicy.active.isnot(False))
        )).scalar_one_or_none()
        if not p:
            return {"error": f"정책 {policy_id} 없음"}
        chunks = (await db.execute(
            select(models.PolicyChunk.content)
            .where(models.PolicyChunk.policy_id == policy_id)
            .where(models.PolicyChunk.chunk_type == "eligibility")
        )).scalars().all()
        fd = p.full_data or {}
        return {
            "policy_id": policy_id,
            "title": p.title,
            "structured": {
                "severity_levels": p.severity_levels or [],
                "has_companion_benefit": p.has_companion_benefit,
                "has_income_criteria": p.has_income_criteria,
                "age_min": p.age_min,
                "age_max": p.age_max,
                "income_criteria": (fd.get("eligibility") or {}).get("income_criteria"),
                "residency_criteria": (fd.get("eligibility") or {}).get("residency_criteria"),
            },
            "eligibility_details": "\n\n".join(chunks) if chunks else "자격 요건 상세 청크 없음.",
        }
    return await _with_session(run)


# ─────────────────────────────────────────────────────────────
# 도구 #5 (벡터 검색)
# ─────────────────────────────────────────────────────────────
async def tool_find_operating_agencies(query: str, limit: int = 3, *, embed_fn) -> dict:
    """지역명·기관명 관련 자연어 질문으로 운영기관·연락처 청크를 찾습니다.

    Args:
        query: 자연어 질문 (예: '부산에서 어디서 신청해요?')
        limit: 반환 개수
        embed_fn: 임베딩 함수
    """
    try:
        qvec = embed_fn(query)
    except Exception as e:
        logger.warning("임베딩 실패 — 기관 키워드 텍스트 검색 폴백: %s", str(e)[:120])
        return await _keyword_text_search(query, limit)

    async def run(db: AsyncSession):
        stmt = (
            select(
                models.PolicyChunk.policy_id,
                models.PolicyChunk.chunk_type,
                models.PolicyChunk.content,
                models.PolicyChunk.metadata_,
                models.WelfarePolicy.title,
            )
            .join(models.WelfarePolicy, models.PolicyChunk.policy_id == models.WelfarePolicy.id)
            .where(models.PolicyChunk.chunk_type.in_(["agency_specific", "contact"]))
            .order_by(models.PolicyChunk.embedding.cosine_distance(qvec))
            .limit(min(max(limit, 1), 10))
        )
        rows = (await db.execute(stmt)).all()
        return {
            "query": query,
            "results": [
                {
                    "policy_id": r.policy_id,
                    "policy_title": r.title,
                    "chunk_type": r.chunk_type,
                    "agency_info": r.content,
                    "metadata": r.metadata_,
                }
                for r in rows
            ],
        }
    return await _with_session(run)


# ─────────────────────────────────────────────────────────────
# 도구 디스패처 (Gemini 함수명 → 실제 핸들러)
# ─────────────────────────────────────────────────────────────
def get_tool_dispatcher(embed_fn):
    """embed_fn 을 주입한 디스패처 dict 를 반환."""
    return {
        "search_policies_by_metadata": tool_search_policies_by_metadata,
        "search_by_keyword": lambda **kw: tool_search_by_keyword(embed_fn=embed_fn, **kw),
        "get_policy_details": tool_get_policy_details,
        "check_eligibility_criteria": tool_check_eligibility_criteria,
        "find_operating_agencies": lambda **kw: tool_find_operating_agencies(embed_fn=embed_fn, **kw),
    }
