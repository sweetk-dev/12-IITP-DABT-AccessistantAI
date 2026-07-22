# tool_handlers.py
# Gemini Live API 의 Function Calling 핸들러.
# main.py 의 5종 FastAPI 엔드포인트와 동일 로직을 "일반 async 함수" 형태로 재구현해
# Gemini SDK 가 직접 호출 가능하도록 합니다.
#
# FastAPI 엔드포인트는 Depends(get_db) 의존성 주입 때문에 Gemini Live tools 에
# 그대로 넣을 수 없어, 같은 DB 세션 헬퍼를 받는 일반 함수로 분리했습니다.
import asyncio
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
        qvec = await asyncio.to_thread(embed_fn, query)
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
_ANYANG_STATIONS = {
    "안양":   ("1호선", 37.4016302, 126.9228826),
    "명학":   ("1호선", 37.3843939, 126.9356089),
    "석수":   ("1호선", 37.4351332, 126.9023059),
    "관악":   ("1호선", 37.4187236, 126.9091539),
    "범계":   ("4호선", 37.3899129, 126.95091),
    "평촌":   ("4호선", 37.394288,  126.9638795),
    "인덕원": ("4호선", 37.4016323, 126.9769656),
}


async def _resolve_origin_place(place: str) -> Optional[dict]:
    """말로 지정한 출발지 이름을 좌표로 해석 — ① 안양 지하철역 ② 무장애 관광 POI.

    "안양역에 있는데", "범계역에서 출발" 처럼 사용자가 출발지를 말로 밝히는 경우
    실제 GPS 위치(서비스 지역 밖일 수 있음) 대신 그 지점을 출발지로 쓴다.
    """
    q = (place or "").strip()
    if not q:
        return None
    # ① 지하철역 — 역명은 '역' 접미사를 떼고 비교.
    #    역 접근성 테이블(poi_station_access_status)은 이동편의 DB(iitp_db) 소속이라
    #    이 백엔드의 정책 DB 세션으로는 조회할 수 없어, 변동 없는 안양 소재 7역을
    #    정적 매핑으로 둔다 (좌표 출처: iitp_db poi_station_access_status, 2026-07-14).
    stn = re.sub(r"\s+", "", q)
    stn = re.sub(r"(지하철)?역$", "", stn) or stn
    hit = _ANYANG_STATIONS.get(stn)
    if hit:
        return {"lat": hit[1], "lng": hit[2], "label": "%s역(%s)" % (stn, hit[0])}

    # ② 무장애 관광 POI 이름 매칭
    try:
        data = await route_client.tour_spots(sigungu="안양", limit=60)
        for it in (data.get("items") or []):
            nm = (it.get("name") or "").strip()
            if nm and (q in nm or nm in q) and it.get("lat") is not None:
                return {"lat": float(it["lat"]), "lng": float(it["lng"]), "label": nm}
    except Exception:
        logger.exception("출발지 POI 해석 실패: %s", q)
    return None


async def tool_plan_accessible_route(destination_poi_id: str = "",
                                     destination_type: str = "tour",
                                     profile: str = "wheelchair_manual",
                                     origin_lat: float = None,
                                     origin_lng: float = None,
                                     origin_place: str = "") -> dict:
    """현재 위치(또는 말로 지정한 출발지)에서 목적지까지 무장애 경로.

    origin_lat/lng 은 프런트가 보낸 현위치가 주입되고,
    사용자가 출발지를 말로 밝히면 origin_place 가 우선한다.
    """
    origin_label = None
    if origin_place:
        hit = await _resolve_origin_place(origin_place)
        if hit is None:
            return {
                "status": "need_origin",
                "tool_name": "plan_accessible_route",
                "message": "출발지 '%s' 를 찾을 수 없습니다" % origin_place,
                "ai_instruction": (
                    "말씀하신 출발지를 찾을 수 없다고 안내하고, 안양시 내 지하철역 이름(예: 안양역, 범계역)이나 "
                    "무장애 관광지 이름으로 다시 말씀해 주시거나, 이동·관광 화면의 지도를 눌러 출발지를 "
                    "직접 지정해 달라고 요청하세요. 경로를 추측하지 마세요."
                ),
            }
        origin_lat, origin_lng = hit["lat"], hit["lng"]
        origin_label = hit["label"]

    if origin_lat is None or origin_lng is None:
        return {
            "status": "need_location",
            "tool_name": "plan_accessible_route",
            "ai_instruction": (
                "현재 위치를 알 수 없다고 안내하고, 화면의 위치 권한을 허용하거나 "
                "출발지 이름(예: 안양역)을 말씀해 주시거나, 이동·관광 화면 지도에서 출발지를 "
                "지정해 달라고 짧게 요청하세요. 경로를 추측하지 마세요."
            ),
        }

    data = await route_client.plan_route(
        {"lat": origin_lat, "lng": origin_lng},
        {"type": destination_type, "poi_id": destination_poi_id},
        profile=profile,
    )
    if data.get("status") == "error":
        return data

    routes = data.get("routes") or []
    if not routes:
        return {"status": "error", "message": "경로를 찾지 못했습니다"}

    primary = routes[0]
    summary = primary.get("summary", {})
    return {
        "status": "success",
        "tool_name": "plan_accessible_route",
        "origin_label": origin_label,
        "route_id": data.get("route_id"),
        "summary": {
            "distance_m": summary.get("total_distance_m"),
            "duration_min": round((summary.get("duration_sec") or 0) / 60),
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
            + "총 거리·예상 시간·최대 경사·계단 수를 한 문장으로 요약하고, 첫 안내 한 문장을 덧붙이세요. "
            "경고(warnings)나 제약 완화(fallback.used=true)가 있으면 반드시 함께 알리세요. "
            "전체 경로를 단계별로 읽지 마세요 — 화면과 안내 음성이 따로 진행합니다."
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
        "open_navi_screen": tool_open_navi_screen,
    }
