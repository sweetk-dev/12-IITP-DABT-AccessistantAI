# tool_handlers.py
# Gemini Live API 의 Function Calling 핸들러.
# main.py 의 5종 FastAPI 엔드포인트와 동일 로직을 "일반 async 함수" 형태로 재구현해
# Gemini SDK 가 직접 호출 가능하도록 합니다.
#
# FastAPI 엔드포인트는 Depends(get_db) 의존성 주입 때문에 Gemini Live tools 에
# 그대로 넣을 수 없어, 같은 DB 세션 헬퍼를 받는 일반 함수로 분리했습니다.
import asyncio
import logging
import os
import re
from typing import Optional

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal
import models

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 생활 언어 → 정책 어휘 질의 확장 (#229)
#
# 정책 청크는 행정 문체라 "배고파 죽겠어" 같은 생활 언어를 그대로 임베딩하면
# 코사인 거리가 붙지 않는다. 상태 발화일 때만 확장을 발동해
# 일반 질문의 응답 지연은 그대로 두고 검색 재현율만 끌어올린다.
#
# 원칙: 확장은 검색을 돕는 보조 단계다. 실패·지연 시 원 질의로 그대로 검색한다.
# ─────────────────────────────────────────────────────────────
EXPAND_MODEL = os.environ.get("GEMINI_EXPAND_MODEL", "gemini-flash-lite-latest")
EXPAND_TIMEOUT_S = float(os.environ.get("GEMINI_EXPAND_TIMEOUT_S", "3.0"))

_EXPAND_PROMPT = """사용자가 음성 상담에서 한 말을 복지 정책 문서 검색용 질의로 바꾸세요.

사용자 말: "{utterance}"

규칙:
- 사용자가 겪는 어려움이 어떤 지원 영역에 해당하는지 판단해 그 영역의 행정 용어로 바꿉니다.
- 관련 있는 제도명·급여명·지원 항목을 쉼표로 나열합니다. 3~6개.
- 장애인 복지 정책 문서에서 실제 쓰이는 표현을 씁니다.
- 설명·따옴표·머리말 없이 질의문 한 줄만 출력합니다.

예시:
"배고파 죽겠어" -> 식생활 지원, 생계급여, 긴급복지 생계지원, 식품 지원, 저소득 급식 지원
"집이 너무 추워" -> 연료비 지원, 난방비 감면, 도시가스 요금 감면, 전기요금 할인, 주거 지원
"병원비가 없어" -> 의료비 지원, 의료급여, 건강보험료 경감, 긴급복지 의료지원, 장애인 의료비"""

_expand_client = None


def _get_expand_client():
    """확장용 경량 모델 클라이언트 (지연 초기화). 키가 없으면 None."""
    global _expand_client
    if _expand_client is None:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            return None
        from google import genai
        _expand_client = genai.Client(api_key=key)
    return _expand_client


def _expand_query_sync(utterance: str) -> Optional[str]:
    client = _get_expand_client()
    if client is None:
        return None
    from google.genai import types as _gtypes
    resp = client.models.generate_content(
        model=EXPAND_MODEL,
        contents=_EXPAND_PROMPT.format(utterance=utterance[:200]),
        config=_gtypes.GenerateContentConfig(temperature=0.0, max_output_tokens=120),
    )
    out = (getattr(resp, "text", None) or "").strip()
    out = out.strip("\"'` \n")          # 모델이 따옴표·머리말을 붙이는 경우 방어
    if not out or len(out) > 300:
        return None
    return out


async def expand_query(utterance: str) -> Optional[str]:
    """확장 질의 반환. 실패·시간 초과 시 None (호출부는 원 질의로 계속 진행)."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_expand_query_sync, utterance),
            timeout=EXPAND_TIMEOUT_S,
        )
    except Exception as e:
        logger.info("질의 확장 생략 (원 질의로 검색): %s", str(e)[:120])
        return None


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


async def tool_search_by_keyword(query: str, top_k: int = 5, expand: bool = False,
                                 *, embed_fn) -> dict:
    """자연어 질문을 768차원 벡터로 변환한 뒤 모든 청크에서 의미적으로 가까운 결과를 찾습니다.

    Args:
        query: 자연어 질문
        top_k: 반환 개수
        expand: 생활 언어 발화를 정책 어휘로 확장한 뒤 검색할지 (상태 발화일 때 true)
        embed_fn: 임베딩 함수 (main.py 의 _embed)
    """
    search_text = query
    expanded = None
    if expand:
        expanded = await expand_query(query)
        if expanded:
            search_text = expanded
            logger.info("질의 확장: %r -> %r", query[:40], expanded[:80])
    try:
        qvec = await asyncio.to_thread(embed_fn, search_text)
    except Exception as e:
        logger.warning("임베딩 실패 — 키워드 텍스트 검색 폴백: %s", str(e)[:120])
        return await _keyword_text_search(search_text, top_k)

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
            # 확장이 실제로 적용됐는지 관측 가능하게 노출 (미적용 시 None)
            "expanded_query": expanded,
            "sources_top3": _top_sources_from_fd(rows[0].full_data) if rows else [],
            "results": [
                {
                    "policy_id": r.policy_id,
                    "title": r.title,
                    "category": r.category,
                    "policy_summary": r.short_summary,
                    "matched_chunk_type": r.chunk_type,
                    "matched_content": r.content,
                    "last_verified": (r.full_data or {}).get("last_verified"),
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
        # 이 정책 정보가 언제 확인된 것인지 — 금액·기준은 해마다 바뀌므로 답변에 반드시 실어야 한다
        "last_verified": fd.get("last_verified"),
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
            "last_verified": fd.get("last_verified"),
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
        qvec = await asyncio.to_thread(embed_fn, query)
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
# 도구 #6~8 — 이동경로·무장애 관광 (02-IITP-DABT-Route 연동)
# 경로/관광은 별도 API 서비스가 담당한다. 이 비서는 호출만 하고,
# 실패해도 정책 상담이 멈추지 않도록 오류를 값으로 돌려준다.
# ─────────────────────────────────────────────────────────────
import route_client


def _fac_labels(facilities: dict) -> list:
    """01 통합DB `poi_tour_bf_facility` 실제 컬럼명 기준 (2026-07-13 실측)."""
    label = {
        "toilet_yn": "장애인 화장실",
        "elevator_yn": "엘리베이터",
        "parking_yn": "장애인 주차장",
        "slope_yn": "경사로",
        "subway_yn": "지하철 접근",
        "bus_stop_yn": "버스정류장 접근",
        "wheelchair_rent_yn": "휠체어 대여",
        "tactile_map_yn": "촉지도",
        "audio_guide_yn": "오디오 가이드",
        "nursing_room_yn": "수유실",
        "accessible_room_yn": "무장애 객실",
        "stroller_rent_yn": "유아차 대여",
    }
    return [v for k, v in label.items() if (facilities or {}).get(k)]


async def tool_find_bf_tour_spots(disabilities=None, sigungu: str = "안양",
                                  topk: int = 5,
                                  origin_lat: float = None, origin_lng: float = None,
                                  offset: int = 0) -> dict:
    """장애 유형별 무장애 관광지 추천.

    origin_lat/lng 를 주면 02 route-api 가 거리 오름차순으로 정렬하고
    offset 페이징(total/has_more)을 지원한다 — 프런트 무한스크롤용.
    """
    data = await route_client.tour_recommend(
        disabilities or ["지체장애"], sigungu=sigungu, topk=topk,
        origin_lat=origin_lat, origin_lng=origin_lng, offset=offset,
    )
    if data.get("status") == "error":
        return data

    items = []
    for it in data.get("items", []):
        items.append({
            "poi_id": it.get("poi_id"),
            "name": it.get("name"),
            "addr": it.get("addr"),
            # 지도 태그(핀) 표시용 좌표 — 목록 응답에 반드시 포함한다
            "lat": it.get("lat"),
            "lng": it.get("lng"),
            "distance_m": it.get("distance_m"),
            "facilities": _fac_labels(it.get("facilities")),
            "score": it.get("score"),
        })
    total = data.get("total", len(items))
    return {
        "status": "success",
        "tool_name": "find_bf_tour_spots",
        "count": len(items),
        "total": total,
        "offset": offset,
        "has_more": data.get("has_more", offset + len(items) < total),
        "results": items,
        "ui_action": {"action": "show_tour_spots", "items": items},
        "ai_instruction": (
            "상위 2~3곳만 이름과 대표 편의시설 위주로 짧게 안내하세요. "
            "화면에 지도와 목록이 함께 표시되므로 전부 나열하지 마세요. "
            "결과가 없으면 데이터가 아직 준비되지 않았다고 솔직히 말하세요."
        ),
    }


# 안양 소재 지하철역 7곳 — {역명: (호선, 위도, 경도)}
# 경로 안내가 가능한 지역 — 안내문이 "장애"가 아니라 "범위"를 말하도록 한다
SERVICE_AREA = route_client.SERVICE_AREA

_ANYANG_STATIONS = {
    "안양":   ("1호선", 37.4016302, 126.9228826),
    "명학":   ("1호선", 37.3843939, 126.9356089),
    "석수":   ("1호선", 37.4351332, 126.9023059),
    "관악":   ("1호선", 37.4187236, 126.9091539),
    "범계":   ("4호선", 37.3899129, 126.95091),
    "평촌":   ("4호선", 37.394288,  126.9638795),
    "인덕원": ("4호선", 37.4016323, 126.9769656),
}


_SERVICE_BBOX = {"value": None, "checked": False}


async def _service_bbox() -> Optional[dict]:
    """경로 서비스의 실제 공간 범위(보행망 bbox). 한 번만 조회해 캐시한다.

    "지역 밖"이라는 안내는 이 범위로만 판정한다 — 이름을 못 찾은 것과 범위를
    벗어난 것은 다른 사유이고, 이용자에게 다르게 들려야 한다.
    """
    if _SERVICE_BBOX["checked"]:
        return _SERVICE_BBOX["value"]
    _SERVICE_BBOX["checked"] = True
    try:
        meta = await route_client.meta_network()
        bb = (meta or {}).get("bbox") or {}
        if bb.get("min_lat") is not None:
            _SERVICE_BBOX["value"] = bb
    except Exception:
        logger.exception("서비스 범위 조회 실패 — 범위 판정을 생략한다")
    return _SERVICE_BBOX["value"]


async def _outside_place(hit: dict) -> bool:
    """해석된 장소가 서비스 범위 밖인지 — 검색이 알려준 판정을 우선한다."""
    if hit.get("in_service_area") is False:
        return True
    return await _outside_service_area(hit["lat"], hit["lng"])


async def _outside_service_area(lat: float, lng: float) -> bool:
    """좌표가 서비스 범위 밖인지. 범위를 모르면 '밖'이라고 단정하지 않는다."""
    bb = await _service_bbox()
    if not bb:
        return False
    return not (bb["min_lat"] <= lat <= bb["max_lat"]
                and bb["min_lng"] <= lng <= bb["max_lng"])


def _search_hit(found: dict, q: str) -> Optional[dict]:
    """`/poi/search` 응답의 첫 유효 항목을 해석 결과로 바꾼다.

    ``in_service_area`` 는 그대로 실어 보낸다 — 호출부가 "찾지 못함"과 "범위 밖"을
    구분해 답하는 근거다.
    """
    for it in ((found or {}).get("items") or []):
        if it.get("lat") is None or it.get("lng") is None:
            continue
        kind = it.get("type") or "building"
        out = {"lat": float(it["lat"]), "lng": float(it["lng"]),
               "label": it.get("name") or q,
               "kind": "tour" if kind == "tour" else (
                   "station" if kind == "transit_station" else "building"),
               "in_service_area": it.get("in_service_area", True)}
        if kind == "tour" and it.get("poi_id"):
            out["poi_id"] = str(it["poi_id"])
        return out
    return None


async def _resolve_place(place: str) -> Optional[dict]:
    """말로 지정한 장소 이름을 좌표로 해석한다.

    (1) 안양 지하철역 정적 매핑 (2) 02 `/poi/search`(관광지·역·건물) (3) 무장애 관광지 이름

    (2)가 없던 동안 해석 가능한 장소는 지하철역 7곳과 무장애 관광지뿐이었다. 그래서
    안양시청·복지관·도서관처럼 서비스 지역 한복판에 있는 시설조차 좌표를 얻지 못해
    "지역 밖"으로 잘못 안내됐다. 02 v1.18.0 의 건물 이름 인덱스가 그 공백을 메운다.

    반환: {"lat","lng","label","kind"(station|tour|building), "poi_id"?}
    """
    q = (place or "").strip()
    if not q:
        return None
    # (1) 지하철역 — 역명은 '역' 접미사를 떼고 비교.
    #     역 접근성 테이블(poi_station_access_status)은 이동편의 DB(iitp_db) 소속이라
    #     이 백엔드의 정책 DB 세션으로는 조회할 수 없어, 변동 없는 안양 소재 7역을
    #     정적 매핑으로 둔다 (좌표 출처: iitp_db poi_station_access_status, 2026-07-14).
    stn = re.sub(r"\s+", "", q)
    stn = re.sub(r"(지하철)?역$", "", stn) or stn
    hit = _ANYANG_STATIONS.get(stn)
    if hit:
        return {"lat": hit[1], "lng": hit[2], "kind": "station",
                "label": "%s역(%s)" % (stn, hit[0])}

    # (2) 장소 이름 검색 — 관광지·역·건물을 한 번에 본다
    try:
        found = await route_client.poi_search(q, sigungu="안양", limit=5)
        hit = _search_hit(found, q)
        if hit is not None:
            return hit
        # 범위 안에서 못 찾았다 — 범위를 넓혀 한 번 더 본다. 찾히면 "밖이라서 안 된다"고
        # 정확히 말할 수 있고, 그래도 없으면 "이름을 못 찾았다"가 사실 그대로가 된다.
        wide = await route_client.poi_search(q, sigungu="", limit=5, include_outside=True)
        hit = _search_hit(wide, q)
        if hit is not None:
            return hit
    except Exception:
        logger.exception("장소 검색 실패: %s", q)

    # (3) 폴백 — 02 가 구버전이라 /poi/search 가 없을 때도 기존 동작은 유지한다
    try:
        data = await route_client.tour_spots(sigungu="안양", limit=60)
        for it in (data.get("items") or []):
            nm = (it.get("name") or "").strip()
            if nm and (q in nm or nm in q) and it.get("lat") is not None:
                return {"lat": float(it["lat"]), "lng": float(it["lng"]),
                        "label": nm, "kind": "tour", "poi_id": it.get("poi_id")}
    except Exception:
        logger.exception("장소 POI 해석 실패: %s", q)
    return None


# 이전 이름 유지 (호출부 호환)
_resolve_origin_place = _resolve_place


def _route_unavailable_ui(reason: str, kind: str, place: str) -> dict:
    """화면에도 같은 사실을 알린다.

    실패는 말로만 전해지고 화면은 그대로였다 — 이전 경로가 지도와 시트에 남아 있는
    채로 "안내할 수 없다"고 말하니, 이용자에게는 말과 화면이 어긋나 보였다.
    """
    return {"action": "route_unavailable", "reason": reason,
            "kind": kind, "place": place, "service_area": SERVICE_AREA}


def _place_not_found(kind: str, place: str) -> dict:
    """이름으로 장소를 찾지 못함 — 서비스 범위와는 무관한 사유다."""
    return {
        "status": "place_not_found",
        "tool_name": "plan_accessible_route",
        "service_area": SERVICE_AREA,
        "kind": kind,
        "place": place,
        "message": "%s '%s'의 위치를 찾지 못했습니다" % (kind, place),
        "ui_action": _route_unavailable_ui("place_not_found", kind, place),
        "ai_instruction": (
            "말씀하신 %s '%s'의 위치를 찾지 못했다고 안내하세요. %s 밖이라고 말하지 "
            "마세요 — 범위 문제가 아니라 그 이름을 찾지 못한 것입니다. 조금 더 정확한 "
            "이름(예: '안양시청', '안양시노인종합복지관')을 말씀해 주시거나, 이동·관광 "
            "화면 지도에서 그 지점을 직접 눌러 지정해 달라고 요청하세요. 다만 이용자가 말한 곳이 "
            "다른 시·도(예: 서울)임이 발화 자체로 분명하다면, 아직 %s 안에서만 안내할 수 있다는 "
            "점을 함께 알려도 됩니다 — 도구가 못 찾았다는 이유만으로 범위 밖이라고 단정하지는 "
            "마세요. 서비스 장애나 일시적인 오류라고 말하지 말고, 좌표나 경로를 추측하지 마세요."
            % (kind, place, SERVICE_AREA, SERVICE_AREA)
        ),
    }


def _out_of_service_area(kind: str, place: str) -> dict:
    """좌표는 찾았지만 경로를 만들 수 있는 범위 밖 — 기능 범위임을 분명히 한다."""
    return {
        "status": "out_of_service_area",
        "tool_name": "plan_accessible_route",
        "service_area": SERVICE_AREA,
        "kind": kind,
        "place": place,
        "message": "%s '%s'는 경로 안내가 가능한 지역(%s) 밖입니다"
                   % (kind, place, SERVICE_AREA),
        "ui_action": _route_unavailable_ui("out_of_service_area", kind, place),
        "ai_instruction": (
            "말씀하신 %s '%s'는 찾았지만 경로 안내가 가능한 지역(%s) 밖이라 길을 안내할 수 "
            "없다고 정확히 안내하세요. 현재 경로 안내는 %s 안에서만 가능하다는 점을 분명히 "
            "밝히고, %s 안의 장소를 말씀해 달라고 요청하세요. 서비스 장애나 일시적인 "
            "오류라고 말하지 말고, 경로를 추측하지 마세요."
            % (kind, place, SERVICE_AREA, SERVICE_AREA, SERVICE_AREA)
        ),
    }


AUTO_TRANSIT_MIN_M = 700     # 이 직선거리 미만이면 자동 모드는 도보를 쓴다


def _mode_label(mode: str) -> str:
    return {"walk": "도보", "walk_bus": "도보+버스",
            "walk_bus_subway": "도보+버스+지하철"}.get(mode, mode)


async def tool_plan_accessible_route(destination_poi_id: str = "",
                                     destination_type: str = "tour",
                                     profile: str = "wheelchair_manual",
                                     origin_lat: float = None,
                                     origin_lng: float = None,
                                     origin_place: str = "",
                                     destination_place: str = "",
                                     destination_lat: float = None,
                                     destination_lng: float = None,
                                     mode: str = "") -> dict:
    """현재 위치(또는 말로 지정한 출발지)에서 목적지까지 무장애 경로.

    origin_lat/lng 은 프런트가 보낸 현위치가 주입되고,
    사용자가 출발지를 말로 밝히면 origin_place 가 우선한다.
    목적지는 poi_id > 지도에서 찍은 좌표(destination_lat/lng) > 이름(destination_place)
    순으로 정한다. 이름을 좌표로 바꾸지 못하면 경로 API 를 부르지 않고 즉시 답하되,
    "이름을 못 찾음"과 "서비스 범위 밖"을 구분해서 답한다 — 두 사유를 한 문장으로
    묶으면 안양시 한복판 시설도 "안양시 밖"으로 안내되어 범위를 오해하게 된다.
    """
    dest_label = None
    dest_coord = None
    if not destination_poi_id and destination_lat is not None and destination_lng is not None:
        # 지도에서 직접 지정한 지점 — 이용자가 콕 집은 좌표이므로 그대로 쓴다
        dest_coord = {"lat": float(destination_lat), "lng": float(destination_lng)}
        if (destination_type or "").strip() != "building":
            destination_type = "coord"
        dest_label = (destination_place or "").strip() or "지도에서 지정한 지점"
        if await _outside_service_area(dest_coord["lat"], dest_coord["lng"]):
            return _out_of_service_area("목적지", dest_label)
    elif not destination_poi_id and destination_place:
        dhit = await _resolve_place(destination_place)
        if dhit is None:
            return _place_not_found("목적지", destination_place)
        dest_label = dhit["label"]
        if await _outside_place(dhit):
            # 범위 밖 안내에는 이용자가 말한 이름을 그대로 쓴다. 넓힌 재검색은 전국을
            # 대상으로 하므로 느슨하게 매칭된 상호명("○○ 서울시청점")이 잡힐 수 있고,
            # 그 이름을 되읽으면 이용자는 자기가 말한 곳 이야기가 아니라고 느낀다.
            return _out_of_service_area("목적지", destination_place.strip() or dest_label)
        if dhit.get("poi_id"):
            destination_poi_id, destination_type = dhit["poi_id"], "tour"
        else:
            # 건물·역은 시설 대표점이다 — 02 가 출입구 접근점을 다시 잡도록 building 으로 넘긴다
            destination_poi_id = ""
            destination_type = "building"
            dest_coord = {"lat": dhit["lat"], "lng": dhit["lng"]}
    if not destination_poi_id and dest_coord is None:
        return {
            "status": "need_destination",
            "tool_name": "plan_accessible_route",
            "service_area": SERVICE_AREA,
            "ui_action": _route_unavailable_ui("need_destination", "목적지", ""),
            "ai_instruction": (
                "어디로 가시는지 목적지를 알 수 없다고 짧게 되묻고, 경로 안내는 %s 안에서만 "
                "가능하다는 점을 함께 알리세요. 경로를 추측하지 마세요." % SERVICE_AREA
            ),
        }

    origin_label = None
    if origin_place:
        hit = await _resolve_place(origin_place)
        if hit is None:
            return _place_not_found("출발지", origin_place)
        if await _outside_place(hit):
            return _out_of_service_area("출발지", origin_place.strip() or hit["label"])
        origin_lat, origin_lng = hit["lat"], hit["lng"]
        origin_label = hit["label"]

    if origin_lat is None or origin_lng is None:
        return {
            "status": "need_location",
            "tool_name": "plan_accessible_route",
            "ui_action": _route_unavailable_ui("need_location", "출발지", ""),
            "ai_instruction": (
                "현재 위치를 알 수 없다고 안내하고, 화면의 위치 권한을 허용하거나 "
                "출발지 이름(예: 안양역)을 말씀해 주시거나, 이동·관광 화면 지도에서 출발지를 "
                "지정해 달라고 짧게 요청하세요. 경로를 추측하지 마세요."
            ),
        }

    dest = ({"type": destination_type or "coord",
             "lat": dest_coord["lat"], "lng": dest_coord["lng"]}
            if dest_coord is not None
            else {"type": destination_type, "poi_id": destination_poi_id})

    # ── 모드 결정 (#251) ──
    # ""/auto = 자동 추천: 도보 경로를 먼저 만들고, 도보가 멀면(기준 이상)
    # 대중교통 조합(walk_bus_subway)으로 승격을 시도한다. 조합이 없으면 도보 유지.
    req_mode = (mode or "").strip().lower()
    auto = req_mode in ("", "auto", "recommend")
    if not auto and req_mode not in ("walk", "walk_bus", "walk_bus_subway"):
        return {"status": "error",
                "message": "지원하지 않는 이동 방식입니다: %s" % mode,
                "ai_instruction": "이동 방식은 도보/도보+버스/도보+버스+지하철 중에서만 "
                                  "고를 수 있다고 짧게 안내하세요."}

    origin_pt = {"lat": origin_lat, "lng": origin_lng}
    if auto:
        data = await route_client.plan_route(origin_pt, dest, profile=profile, mode="walk")
        if data.get("status") == "error":
            return data
        mode_used = "walk"
        walk_dist = ((data.get("routes") or [{}])[0].get("summary") or {}).get(
            "total_distance_m") or 0
        if walk_dist >= AUTO_TRANSIT_MIN_M:
            upgraded = await route_client.plan_route(
                origin_pt, dest, profile=profile, mode="walk_bus_subway", realtime=True)
            if upgraded.get("status") != "error" and (upgraded.get("routes") or []):
                data, mode_used = upgraded, "walk_bus_subway"
    else:
        data = await route_client.plan_route(origin_pt, dest, profile=profile, mode=req_mode,
                                             realtime=True)
        if data.get("status") == "error":
            return data
        mode_used = req_mode

    routes = data.get("routes") or []
    if not routes:
        return {"status": "error", "message": "경로를 찾지 못했습니다"}

    primary = routes[0]
    summary = primary.get("summary", {})
    legs = primary.get("legs") or []
    transit_brief = []
    low_floor_note = None
    for leg in legs:
        if leg.get("kind") == "bus":
            r = leg.get("route") or {}
            live = leg.get("realtime") or {}
            nlf = live.get("next_low_floor") if isinstance(live, dict) else None
            item = {
                "kind": "bus", "route_name": r.get("name"), "route_type": r.get("type"),
                "end_station": r.get("end_station"),
                "board": (leg.get("board") or {}).get("name"),
                "board_seq": (leg.get("board") or {}).get("station_seq"),
                "board_station_id": (leg.get("board") or {}).get("poi_id"),
                "route_id": r.get("route_id"),
                "alight": (leg.get("alight") or {}).get("name"),
                "stop_cnt": leg.get("stop_cnt"),
                # 실시간(02 v1.19.0): success 면 확인된 사실, unavailable 이면 미확인
                "realtime_status": live.get("status") if isinstance(live, dict) else None,
                "next_low_floor": _brief_low_floor(nlf) if nlf else None,
            }
            if item["realtime_status"] == "success" and low_floor_note is None:
                low_floor_note = ("승차 정류장에 저상버스 %s번이 약 %d분 뒤 도착 예정"
                                  % (nlf.get("route_name") or r.get("name"), nlf["predict_min"])
                                  if nlf else
                                  "지금 승차 정류장에 오는 차량은 저상버스가 아닙니다")
            transit_brief.append(item)
        elif leg.get("kind") == "subway":
            transit_brief.append({
                "kind": "subway", "line": leg.get("line"),
                "board": (leg.get("board") or {}).get("name"),
                "alight": (leg.get("alight") or {}).get("name"),
                "station_cnt": leg.get("station_cnt"),
                # 역 설비 요약(02 v1.19.0): 승차 역 승강기 출입구·장애인화장실 3상태
                "board_facilities": _brief_station((leg.get("board") or {}).get("facilities")),
                "alight_facilities": _brief_station((leg.get("alight") or {}).get("facilities")),
            })
    return {
        "status": "success",
        "tool_name": "plan_accessible_route",
        "mode_used": mode_used,
        "mode_label": _mode_label(mode_used),
        "auto_mode": auto,
        "transit": transit_brief,
        "low_floor_note": low_floor_note,
        "eta_note": summary.get("eta_note"),
        "origin_label": origin_label,
        "destination_label": dest_label,
        "route_id": data.get("route_id"),
        "summary": {
            "distance_m": summary.get("total_distance_m"),
            "duration_min": round((summary.get("duration_sec") or 0) / 60),
            "walk_distance_m": summary.get("walk_distance_m"),
            "max_slope_deg": summary.get("max_slope_deg"),
            "stairs_cnt": summary.get("stairs_cnt"),
            "crossing_cnt": summary.get("crossing_cnt"),
        },
        "warnings": summary.get("warnings", []),
        "fallback": data.get("fallback", {}),
        "first_steps": [s.get("instruction") for s in (primary.get("steps") or [])[:2]],
        # 프런트가 지도·경로·턴바이턴을 그리도록 원본 경로를 그대로 전달
        "ui_action": {"action": "show_route", "route": data},
        "ai_instruction": (
            ("출발지는 %s 기준임을 먼저 밝히세요. " % origin_label if origin_label else "")
            + ("목적지는 %s 입니다. " % dest_label if dest_label else "")
            + ("이동 방식은 %s 입니다%s. " % (_mode_label(mode_used),
               " (자동 추천)" if auto else "") if mode_used else "")
            + ("대중교통 구간이 있으면 transit 의 노선 번호·유형·방면(end_station)·"
               "정거장 수를 함께 말하세요. "
               + ("저상버스 실시간 확인 결과(low_floor_note)를 그대로 한 문장으로 전하고, "
                  "실시간 정보라 변동될 수 있다고 덧붙이세요. "
                  if low_floor_note else
                  "저상버스 정차는 보장되지 않으니 실시간 도착정보 확인이 필요하다고 알리고, "
                  "'저상버스 언제 와' 라고 물으면 확인해 드릴 수 있다고 안내하세요. ")
               + "지하철 구간이 있고 board_facilities.elevators 가 있으면 승차 역 승강기 "
                 "출입구를 한 마디로 알리세요(예: '안양역은 1번 출구 옆 엘리베이터'). "
                 "소요시간은 대기 미포함 추정임을 밝히세요. "
               if transit_brief else "")
            + "총 거리·예상 시간·최대 경사·계단 수를 한 문장으로 요약하고, 첫 안내 한 문장을 덧붙이세요. "
            "경고(warnings)나 제약 완화(fallback.used=true)가 있으면 반드시 함께 알리세요. "
            "전체 경로를 단계별로 읽지 마세요 — 화면과 안내 음성이 따로 진행합니다. "
            "화면의 이동 방식 카드로 다른 방식을 고를 수 있다는 점을 필요할 때만 짧게 안내하세요."
        ),
    }


async def tool_explain_route_segment(route_id: str, step_idx: int = None) -> dict:
    """직전 경로의 특정 구간(또는 전체)이 왜 그렇게 안내되었는지 설명."""
    data = await route_client.get_route(route_id)
    if data.get("status") == "error":
        return data

    routes = data.get("routes") or []
    if not routes:
        return {"status": "error", "message": "경로를 찾을 수 없습니다"}

    primary = routes[0]
    steps = primary.get("steps") or []
    if step_idx is not None and 0 <= step_idx < len(steps):
        target = [steps[step_idx]]
    else:
        target = [s for s in steps if s.get("warnings")][:3] or steps[:1]

    return {
        "status": "success",
        "tool_name": "explain_route_segment",
        "profile": data.get("profile"),
        "fallback": data.get("fallback", {}),
        "data_quality": data.get("data_quality", {}),
        "segments": [
            {
                "idx": s.get("idx"),
                "instruction": s.get("instruction"),
                "link_type": s.get("link_type"),
                "warnings": s.get("warnings", []),
            }
            for s in target
        ],
        "ai_instruction": (
            "해당 구간이 선택된(또는 우회한) 이유를 경사·계단·턱낮춤 관점에서 설명하세요. "
            "fallback.used 가 true 면 권장 경사를 만족하는 경로가 없어 완화했다는 사실을 반드시 알리고, "
            "data_quality.slope_coverage 가 낮으면 경사 데이터가 부족할 수 있다고 덧붙이세요."
        ),
    }


# ─────────────────────────────────────────────────────────────
# 도구 #10 — 주변 정류장·역 (#248, 02 v1.11.1 /transit/access-points)
# ─────────────────────────────────────────────────────────────
async def tool_find_nearby_transit(lat: float = None, lng: float = None,
                                   place: str = "", radius_m: int = 500,
                                   profile: str = "wheelchair_manual") -> dict:
    """현재 위치(또는 말한 기준 장소) 주변의 버스 정류장·지하철역.

    lat/lng 은 live_bridge 가 프런트의 현재 위치를 주입한다(place 미지정 시).
    ⚠️ 정류장의 accessible 은 None(미판정)일 수 있다 — 저상버스 정차 여부가
    정적 데이터에 없어 판정하지 않은 것이지 "이용 불가"가 아니다.
    소비 측은 accessible_status(yes/no/unknown) 로만 판단해야 한다.
    """
    base_label = None
    if place:
        hit = await _resolve_place(place)
        if hit is None:
            return _out_of_service_area("기준 위치", place)
        lat, lng, base_label = hit["lat"], hit["lng"], hit["label"]
    if lat is None or lng is None:
        return {
            "status": "need_location",
            "tool_name": "find_nearby_transit",
            "ai_instruction": (
                "현재 위치를 알 수 없다고 안내하고, 화면의 위치 권한을 허용하거나 "
                "기준 장소 이름(예: 안양역 근처)을 말씀해 달라고 짧게 요청하세요."
            ),
        }
    try:
        radius_m = max(100, min(int(radius_m or 500), 2000))
    except (TypeError, ValueError):
        radius_m = 500

    data = await route_client.transit_access(lat, lng, radius_m=radius_m, profile=profile)
    if isinstance(data, dict) and data.get("status") == "error":
        return data
    items = (data.get("items") if isinstance(data, dict) else data) or []

    out = []
    for it in items[:8]:
        if not isinstance(it, dict):
            continue
        routes = []
        for r in (it.get("routes") or [])[:12]:
            if isinstance(r, dict):
                routes.append({
                    "name": r.get("name"),
                    "type": r.get("type"),
                    "end_station": r.get("end_station"),
                    "station_seq": r.get("station_seq"),
                })
            else:
                routes.append({"name": r})
        item = {
            "type": it.get("type"),
            "name": it.get("name"),
            "poi_id": it.get("poi_id"),
            "dist_m": it.get("dist_m"),
            "mobile_no": it.get("mobile_no"),
            "center_yn": it.get("center_yn"),
            "accessible": it.get("accessible"),
            "accessible_status": it.get("accessible_status")
                                 or ("unknown" if it.get("accessible") is None
                                     else ("yes" if it.get("accessible") else "no")),
            "warnings": it.get("warnings") or [],
            "routes": routes,
        }
        if it.get("type") == "transit_station":
            # 02 가 주는 역 설비 요약을 버리지 않는다 — 개수·리프트·장애인화장실(3상태)
            item.update({
                "line": it.get("line"),
                "elevator_cnt": it.get("elevator_cnt"),
                "wheelchair_lift_cnt": it.get("wheelchair_lift_cnt"),
                "dis_toilet_status": it.get("dis_toilet_status")
                                     or ("yes" if it.get("dis_toilet_yn") else "unknown"),
            })
        out.append(item)

    return {
        "status": "success",
        "tool_name": "find_nearby_transit",
        "base_label": base_label,
        "radius_m": radius_m,
        "count": len(out),
        "items": out,
        "ai_instruction": (
            ("기준 위치는 %s 입니다. " % base_label if base_label else "")
            + "가까운 순으로 2~3곳만 이름·거리와 함께 말하고, 나머지는 생략하세요. "
            "accessible_status 가 unknown 인 정류장은 '이용 불가'가 아니라 "
            "'저상버스 정차 여부는 실시간 도착정보로 확인이 필요하다'고 말하세요. "
            "버스 방면은 end_station(종점명)으로 안내하되, 양방향 종점명이 같은 순환 "
            "노선은 station_seq(경유 순번) 차이로 구분됨을 알리고 정류장 이름·위치로 "
            "확인을 권하세요. 같은 번호라도 유형(마을버스/일반형시내버스)이 다르면 "
            "다른 노선이므로 유형을 함께 말하세요. warnings 가 있으면 반드시 알리세요. "
            "지하철역은 elevator_cnt(승강기 수)와 dis_toilet_status 를 짧게 덧붙이되, "
            "dis_toilet_status 가 unknown 이면 '자료 없음'이지 '없음'이 아닙니다. "
            "정류장의 실시간 도착·저상 여부는 get_bus_arrivals 로 확인할 수 있다고 안내하세요."
        ),
    }


# ─────────────────────────────────────────────────────────────
# 도구 #12 — 정류장 실시간 도착·저상버스 (v1.39.0, 02 v1.19.0 /transit/bus/arrivals)
# ─────────────────────────────────────────────────────────────
def _brief_low_floor(nlf: dict) -> dict:
    return {"route_name": nlf.get("route_name"), "route_type": nlf.get("route_type"),
            "end_station": nlf.get("end_station"), "predict_min": nlf.get("predict_min"),
            "stops_away": nlf.get("stops_away"), "plate_no": nlf.get("plate_no")}


def _brief_station(fac) -> Optional[dict]:
    """02 지하철 leg 의 역 설비 요약을 모델이 읽기 좋게 줄인다."""
    if not isinstance(fac, dict) or not fac:
        return None
    return {
        "elevators": [_loc_text(e) for e in (fac.get("elevators") or [])[:3]],
        "lifts": [_loc_text(l) for l in (fac.get("lifts") or [])[:2]],
        "dis_toilet": fac.get("dis_toilet") or "unknown",
        "safety_plate": fac.get("safety_plate") or "unknown",
        "elevator_cnt": fac.get("elevator_cnt"),
        "wheelchair_lift_cnt": fac.get("wheelchair_lift_cnt"),
    }


def _loc_text(u: dict) -> str:
    ex = (u.get("exit_no") or "").strip()
    loc = (u.get("detail_loc") or "").strip()
    if ex and ex != "내부":
        head = "%s번 출입구" % ex if ex.replace("~", "").replace(" ", "").isdigit() else "출입구 " + ex
    else:
        head = "역사 내부"
    return ("%s — %s" % (head, loc)) if loc else head


async def _nearest_stop(lat: float, lng: float, profile: str) -> Optional[dict]:
    data = await route_client.transit_access(lat, lng, radius_m=400, profile=profile)
    if isinstance(data, dict) and data.get("status") == "error":
        return None
    items = (data.get("items") if isinstance(data, dict) else data) or []
    for it in items:
        if isinstance(it, dict) and it.get("type") == "transit_stop" and it.get("poi_id"):
            return it
    return None


def _arrival_line(it: dict) -> dict:
    vs = it.get("vehicles") or []
    return {
        "route_name": it.get("route_name"), "route_type": it.get("route_type"),
        "end_station": it.get("end_station"),
        "vehicles": [{"predict_min": v.get("predict_min"), "stops_away": v.get("stops_away"),
                      "low_floor": v.get("low_floor")} for v in vs[:2]],
        "low_floor_soon": any(v.get("low_floor") for v in vs),
    }


async def tool_get_bus_arrivals(station_id: str = "", route_id: str = "", place: str = "",
                                station_name: str = "", lat: float = None, lng: float = None,
                                profile: str = "wheelchair_manual") -> dict:
    """정류장의 실시간 도착정보 — "저상버스 언제 와", "다음 버스 저상이야?".

    정류장은 (1) station_id (2) 안내 중 버스 구간의 승차 정류장(세션 주입)
    (3) place 로 말한 장소 근처 (4) 현재 위치 근처 순으로 정한다.
    저상버스가 잡히지 않은 것은 "없다"가 아니라 "도착정보의 두 대 안에 없다"이다 —
    그 구분을 ai_instruction 에 그대로 싣는다.
    """
    base_label = station_name or None
    if not station_id:
        if place:
            hit = await _resolve_place(place)
            if hit is None:
                return _place_not_found("기준 위치", place)
            lat, lng = hit["lat"], hit["lng"]
            base_label = hit["label"]
        if lat is None or lng is None:
            return {
                "status": "need_location",
                "tool_name": "get_bus_arrivals",
                "ai_instruction": (
                    "어느 정류장인지 알 수 없다고 짧게 안내하고, 위치 권한을 허용하거나 "
                    "정류장·장소 이름을 말씀해 달라고 요청하세요."
                ),
            }
        stop = await _nearest_stop(lat, lng, profile)
        if stop is None:
            return {
                "status": "no_stop_nearby",
                "tool_name": "get_bus_arrivals",
                "base_label": base_label,
                "ai_instruction": "주변 400m 안에 버스 정류장을 찾지 못했다고 짧게 안내하세요.",
            }
        station_id = str(stop["poi_id"])
        base_label = "%s 정류장%s" % (stop.get("name"),
                                   "(%s)" % stop["mobile_no"] if stop.get("mobile_no") else "")

    data = await route_client.bus_arrivals(station_id, route_id=route_id or "")
    if isinstance(data, dict) and data.get("status") == "error":
        return data
    if data.get("status") != "success":
        return {
            "status": "unavailable",
            "tool_name": "get_bus_arrivals",
            "station_id": station_id,
            "base_label": base_label,
            "reason": data.get("reason"),
            "ai_instruction": (
                "지금은 실시간 도착정보를 받아오지 못했다고 짧게 알리고, 정류장 안내판이나 "
                "잠시 뒤 다시 물어봐 달라고 안내하세요. 저상 여부를 추측하지 마세요."
            ),
        }
    items = [_arrival_line(it) for it in (data.get("items") or [])]
    nlf = data.get("next_low_floor")
    return {
        "status": "success",
        "tool_name": "get_bus_arrivals",
        "station_id": station_id,
        "route_id": route_id or None,
        "base_label": base_label,
        "count": len(items),
        "items": items[:6],
        "next_low_floor": _brief_low_floor(nlf) if nlf else None,
        "ai_instruction": (
            ("%s 기준입니다. " % base_label if base_label else "")
            + ("가장 빨리 오는 저상버스를 먼저 말하세요: next_low_floor 의 노선 번호·유형·"
               "방면(end_station)·도착 예정(분)·몇 정거장 전. "
               if nlf else
               "지금 도착정보에 잡힌 차량(노선당 최대 2대) 중에는 저상버스가 없다고 말하되, "
               "'저상버스가 없다'가 아니라 '지금 오는 차량은 저상이 아니다'로 표현하고 "
               "잠시 뒤 다시 확인해 드릴 수 있다고 안내하세요. ")
            + ("특정 노선(route_id)만 조회한 결과입니다. " if route_id else
               "다른 노선은 필요할 때만 1~2개 덧붙이세요. ")
            + "실시간 정보라 변동될 수 있음을 한 마디로 알리고, 2~3문장 안에서 끝내세요."
        ),
    }


# ─────────────────────────────────────────────────────────────
# 도구 #13 — 역 편의시설 (v1.39.0, 02 v1.19.0 /transit/station/facilities)
# ─────────────────────────────────────────────────────────────
def _station_query_name(text: str) -> str:
    q = re.sub(r"\s+", "", (text or "").strip())
    q = re.sub(r"(지하철)?역$", "", q) or q
    return q


async def tool_get_station_facilities(station: str = "") -> dict:
    """역의 교통약자 편의시설 — "범계역 엘리베이터 어디 있어", "안양역 장애인 화장실 있어?".

    승강기·리프트는 출입구별 위치를, 화장실은 게이트 안/밖·출구를, 승강장은 안전발판·
    열차 이격거리를 답한다. 유무는 3상태다 — unknown 을 "없음"으로 말하면 틀린다.
    실시간 가동 여부는 제공기관 자료가 없어 답하지 않는다.
    """
    name = _station_query_name(station)
    if not name:
        return {
            "status": "need_station",
            "tool_name": "get_station_facilities",
            "ai_instruction": "어느 역인지 되물어 주세요. 안내 가능한 역은 %s 소재 지하철역입니다." % SERVICE_AREA,
        }
    data = await route_client.station_facilities(name=name)
    if isinstance(data, dict) and data.get("status") == "error":
        if "찾을 수 없" in (data.get("message") or ""):
            return {
                "status": "station_not_found",
                "tool_name": "get_station_facilities",
                "station": station,
                "ai_instruction": ("'%s' 역의 편의시설 자료를 찾지 못했다고 짧게 알리고, 안내 가능한 역은 "
                                   "%s 소재 지하철역(석수·관악·안양·명학·인덕원·평촌·범계)이라고 "
                                   "덧붙이세요." % (station, SERVICE_AREA)),
            }
        return data
    counts = data.get("counts") or {}
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    elevators = [_loc_text(e) for e in (data.get("elevators") or [])]
    lifts = [_loc_text(l) for l in (data.get("lifts") or [])]
    # 남·여 칸이 같은 위치로 두 줄 오므로 위치 문장 기준으로 합친다
    dis_toilets = list(dict.fromkeys(
        _loc_text({"exit_no": t.get("exit_no"),
                   "detail_loc": "%s%s" % ("게이트 %s " % ("안" if t.get("gate_inout") == "내" else "밖")
                                           if t.get("gate_inout") else "",
                                           t.get("detail_loc") or "")})
        for t in (data.get("toilets") or []) if t.get("disabled")))
    platforms = [{"platform_no": p.get("platform_no"), "updown": p.get("updown"),
                  "safety_plate": p.get("safety_plate"), "screen_door": p.get("screen_door"),
                  "gap_min_cm": p.get("gap_min_cm"), "gap_max_cm": p.get("gap_max_cm")}
                 for p in (data.get("platforms") or [])]
    gaps = [p["gap_max_cm"] for p in platforms if p.get("gap_max_cm") is not None]
    return {
        "status": "success",
        "tool_name": "get_station_facilities",
        "station": data.get("name"),
        "line": data.get("line"),
        "elevator_cnt": counts.get("elevator"),
        "escalator_cnt": counts.get("escalator"),
        "wheelchair_lift_cnt": counts.get("wheelchair_lift"),
        "elevators": elevators,
        "lifts": lifts,
        "dis_toilet_status": status.get("dis_toilet", "unknown"),
        "dis_toilets": dis_toilets,
        "dis_slope_status": status.get("dis_slope", "unknown"),
        "safety_plate_status": status.get("safety_plate", "unknown"),
        "platform_gap_max_cm": max(gaps) if gaps else None,
        "platforms": platforms,
        "base_dt": data.get("base_dt"),
        "ai_instruction": (
            "역 이름과 노선을 말한 뒤, 사용자가 물은 항목만 답하세요. 엘리베이터를 물으면 "
            "elevators 의 출입구·위치를 2~3개까지 읽어 주고, 화장실을 물으면 dis_toilets 의 "
            "게이트 안/밖·출구를 말하세요. 리프트만 있는 역은 도움이 필요할 수 있다고 알리세요. "
            "상태 값이 unknown 이면 '없다'가 아니라 '자료가 없다'고 말하세요. "
            "승강장 이격거리(platform_gap_max_cm)는 휠체어 승차를 물었을 때만 '최대 약 n cm 틈, "
            "안전발판 유무' 로 덧붙이세요. 실시간 고장·운행 여부는 알 수 없다고 하세요."
        ),
    }


# ─────────────────────────────────────────────────────────────
# 도구 #9 — 화면 이동 (#215)
# ─────────────────────────────────────────────────────────────
async def tool_open_navi_screen() -> dict:
    """이동·관광(지도) 화면으로 전환한다.

    사용자가 '지도로 이동해줘', '지도 화면 보여줘' 처럼 화면 이동 자체를
    명시적으로 요청한 경우에만 호출된다 — 이때는 사용자가 원한 전환이므로
    (#213 의 자동전환 금지와 달리) 프런트가 즉시 화면을 바꾼다.
    """
    return {
        "status": "success",
        "tool_name": "open_navi_screen",
        "ui_action": {"action": "open_navi"},
        "ai_instruction": "지도 화면으로 이동했다고 한 문장으로만 짧게 알리세요.",
    }


_REPORT_REASONS = ("curb", "no_sidewalk", "no_crossing", "steep", "blocked", "etc")


async def tool_report_accessibility(reason: str = "etc", detail: str = "",
                                    lat: float = None, lng: float = None,
                                    route_id: str = "") -> dict:
    """접근성 문제 음성 제보 (v1.35.0) — 화면의 '신고' 버튼과 같은 02 수집 API 로 접수.

    사용자가 "여기 턱이 있어", "길이 끊겼어, 신고해줘" 처럼 말하면 호출된다.
    좌표는 모델이 지어내지 못하도록 서버가 현재 위치를 주입한다
    (inject_nav_defaults). 접수 즉시 해당 지점 경로 안내에
    '이용자 제보(미확인)' 경고가 붙는다.
    """
    if lat is None or lng is None:
        return {
            "status": "need_location",
            "ai_instruction": (
                "현재 위치를 알 수 없어 제보를 접수할 수 없다고 안내하고, "
                "위치 권한을 허용하거나 이동·관광 화면을 열어 달라고 요청하세요."
            ),
        }
    if reason not in _REPORT_REASONS:
        reason = "etc"
    payload = {"lat": float(lat), "lng": float(lng), "reason": reason,
               "detail": (str(detail)[:500] if detail else None),
               "route_id": (str(route_id)[:20] if route_id else None)}
    r = await route_client.report_accessibility(payload)
    if isinstance(r, dict) and r.get("status") == "error":
        return r          # route_client 가 만든 안내문(ai_instruction) 그대로 전달
    return {
        "status": "success",
        "report_id": (r or {}).get("report_id"),
        "ai_instruction": (
            "제보가 접수되었고, 확인 후 경로 안내에 반영된다고 짧게 감사 인사와 함께 "
            "알리세요. 접수 번호 등 세부 정보는 말하지 마세요."
        ),
    }


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
        # 이동경로·관광 (02-Route 연동) — 기능 플래그가 꺼져 있으면 선언 자체를 하지 않는다
        "find_bf_tour_spots": tool_find_bf_tour_spots,
        "plan_accessible_route": tool_plan_accessible_route,
        "explain_route_segment": tool_explain_route_segment,
        "find_nearby_transit": tool_find_nearby_transit,
        "get_bus_arrivals": tool_get_bus_arrivals,
        "get_station_facilities": tool_get_station_facilities,
        "open_navi_screen": tool_open_navi_screen,
        "report_accessibility_issue": tool_report_accessibility,
    }
