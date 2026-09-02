# main.py
# Welfare Policy AI Bridge API v1.1
# - 5종 Function Calling 도구 모두 구현
# - Fat Tool Response 패턴 (보고서 v1.2 §7.2)
import asyncio
import logging
import os
from typing import Optional
from time import sleep

from fastapi import FastAPI, Depends, Query, HTTPException, WebSocket, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from dotenv import load_dotenv
from google import genai

from database import get_db, engine
import models

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()  # GEMINI_API_KEY 로딩

# Gemini 임베딩 클라이언트 (도구 #5 의 자연어 → 768차원 벡터 변환용)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# 임베딩 모델 — DB 청크 임베딩과 반드시 동일 모델 사용
EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.environ.get("GEMINI_EMBED_DIM", "768"))


def _embed(text_query: str) -> list[float]:
    """자연어 → 768차원 임베딩. 가벼운 재시도(2회) 포함."""
    if ai_client is None:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY 미설정 — 벡터 검색 도구 사용 불가",
        )
    from google.genai import types as _gtypes
    last_err = None
    for attempt in range(2):
        try:
            cfg = _gtypes.EmbedContentConfig(output_dimensionality=EMBED_DIM)
            resp = ai_client.models.embed_content(
                model=EMBED_MODEL,
                contents=text_query,
                config=cfg,
            )
            return resp.embeddings[0].values
        except Exception as e:
            last_err = e
            sleep(2**attempt)
    raise HTTPException(status_code=502, detail=f"임베딩 API 실패: {last_err}")


import route_client
import tool_handlers
from tool_handlers import expand_query

app = FastAPI(
    title="Welfare Policy AI Bridge API",
    version="1.2",
    description=(
        "장애인 복지 정책 DB(B001~B039) 검색 API. "
        "Gemini Multimodal Live API의 Function Calling 백엔드. "
        "Phase 3: 실시간 음성 WebSocket 브릿지 포함."
    ),
)

# CORS 허용 오리진 — 환경변수 ALLOWED_ORIGINS(콤마 구분), 미설정 시 로컬 개발 오리진만 허용
_DEFAULT_ORIGINS = "http://127.0.0.1:18000,http://localhost:18000"
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 정적 페이지 서빙 (welfare_backend/static/ — accessistant.html 진입 페이지 등)
from fastapi.staticfiles import StaticFiles
import pathlib as _pl
_static_dir = _pl.Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# 관리자 콘솔 라우터 (검토 큐 v1-1)
try:
    from admin_router import router as _admin_router
    app.include_router(_admin_router)
except Exception as _e:  # 라우터 로드 실패가 본 서비스 기동을 막지 않도록
    import logging as _lg
    _lg.getLogger("main").warning("admin_router 로드 실패: %s", _e)


# 운영 스케줄러(크롤/백업) 기동 — 단일 워커 전제
@app.on_event("startup")
async def _ensure_schema_migrations():
    """운영 DB 스키마를 최신 상태로 맞춘다(멱등). 실패해도 서비스는 계속 뜬다."""
    import logging as _lg
    try:
        from create_unresolved_table import ensure_migrations
        async with engine.begin() as conn:
            applied = await ensure_migrations(conn)
        if applied:
            _lg.getLogger("main").info("스키마 확장 적용: %s", ", ".join(applied))
    except Exception as _e:
        _lg.getLogger("main").warning("스키마 확장 실패 (무시하고 계속): %s", _e)


@app.on_event("startup")
def _start_ops_scheduler():
    try:
        import scheduler as _scheduler
        _scheduler.start()
    except Exception as _e:
        import logging as _lg
        _lg.getLogger("main").warning("scheduler 기동 실패: %s", _e)


# ─────────────────────────────────────────────────────────────
# Meta
# ─────────────────────────────────────────────────────────────
def _declared_tool_count() -> Optional[int]:
    """Live 세션에 선언되는 함수 도구 개수(웹검색 도구는 제외).

    live_bridge 는 google-genai 를 끌어오므로 지연 임포트한다.
    세지 못하면 거짓 숫자를 주는 대신 null 을 반환한다.
    """
    try:
        from live_bridge import build_tool_declarations
        return sum(len(getattr(t, "function_declarations", None) or [])
                   for t in build_tool_declarations() if t is not None)
    except Exception:
        return None


@app.get("/health", tags=["meta"])
async def health_check():
    """헬스체크 + DB 연결 + Gemini 클라이언트 상태."""
    db_ok = True
    db_msg = "connected"
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        db_ok = False
        db_msg = str(e)
    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_msg,
        "gemini_client": "ready" if ai_client else "missing GEMINI_API_KEY",
        # 모델에 실제로 선언되는 도구 수 — 하드코딩하면 도구가 늘어도 숫자가 안 따라온다.
        # 선언 빌더를 그대로 세어 항상 현행값을 보고한다.
        "tools_declared": _declared_tool_count(),
        "route_api": "ready" if route_client.enabled() else "disabled",
    }


# ─────────────────────────────────────────────────────────────
# 프런트 런타임 설정 — 지도 JS 키·기능 플래그 주입
# (키를 정적 파일에 하드코딩하지 않기 위한 엔드포인트)
# ─────────────────────────────────────────────────────────────
@app.get("/api/v1/config", tags=["meta"], summary="프런트 런타임 설정")
async def front_config():
    # 서비스 가능한 공간 범위 — 프런트가 "지역 밖입니다" 를 판단해 출발지 선택을 유도한다.
    # 경로 서비스가 없거나 응답하지 않으면 생략(기능 자체가 꺼진 상태이므로 무해).
    service_area = None
    if route_client.enabled():
        meta = await route_client.meta_network()
        if meta.get("status") != "error" and meta.get("bbox"):
            service_area = {
                "region": meta.get("region"),
                "bbox": meta["bbox"],
                "network_version": meta.get("network_version"),
            }

    return {
        "kakao_js_key": os.environ.get("KAKAO_JS_KEY", ""),
        "features": {
            "route": route_client.FEATURE_ROUTE and bool(route_client.BASE_URL),
            "tour": route_client.FEATURE_TOUR and bool(route_client.BASE_URL),
        },
        "service_area": service_area,
        "map": {
            # 안양시청 — 위치 권한 거부 시 지도 초기 중심
            "default_center": {"lat": 37.3943, "lng": 126.9568},
            "default_level": 5,
        },
        "route_profiles": [
            {"id": "wheelchair_manual", "label": "수동 휠체어"},
            {"id": "wheelchair_electric", "label": "전동 휠체어"},
            {"id": "crutch", "label": "목발·보행보조"},
            {"id": "visual", "label": "시각장애"},
            {"id": "walk", "label": "일반 보행"},
        ],
    }


# ─────────────────────────────────────────────────────────────
# 도구 #6~8 — 이동경로·무장애 관광 (REST 미러. 프런트가 직접 호출)
# ─────────────────────────────────────────────────────────────
@app.get("/api/v1/tools/find_bf_tour_spots", tags=["tools"],
         summary="[6] 장애 유형별 무장애 관광지 추천")
async def find_bf_tour_spots(
    disabilities: str = Query("지체장애", description="쉼표 구분. 예: '지체장애,시각장애'"),
    sigungu: str = Query("안양"),
    topk: int = Query(5, ge=1, le=50),
    origin_lat: float = Query(None, description="출발지 위도 — 주면 거리 오름차순"),
    origin_lng: float = Query(None, description="출발지 경도"),
    offset: int = Query(0, ge=0, description="거리순 목록에서 건너뛸 개수(무한스크롤)"),
):
    types_ = [d.strip() for d in disabilities.split(",") if d.strip()]
    return await tool_handlers.tool_find_bf_tour_spots(
        disabilities=types_, sigungu=sigungu, topk=topk,
        origin_lat=origin_lat, origin_lng=origin_lng, offset=offset,
    )


# ─────────────────────────────────────────────────────────────
# 길안내 TTS — 상담 음성과 동일한 Gemini 보이스로 안내 문장을 합성
# ─────────────────────────────────────────────────────────────
# 브라우저 내장 TTS 와 상담 음성의 톤 차이를 없애기 위해 같은 prebuilt 보이스를 사용.
# 안내 문장은 반복성이 높아 서버 캐시(LRU)로 비용·지연을 줄인다. 실패 시 프런트가
# 브라우저 TTS 로 폴백하므로 이 엔드포인트는 best-effort 로 동작하면 된다.
from collections import OrderedDict as _OrderedDict
import re as _re
import struct as _struct

GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
_TTS_CACHE: "_OrderedDict[tuple, bytes]" = _OrderedDict()
_TTS_CACHE_MAX = 300


def _pcm_to_wav(pcm: bytes, rate: int = 24000, channels: int = 1, width: int = 2) -> bytes:
    """원시 PCM(16bit) 을 WAV 컨테이너로 감싼다 — <audio>/WebAudio 에서 바로 재생 가능."""
    byte_rate = rate * channels * width
    block_align = channels * width
    header = b"RIFF" + _struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + _struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, block_align, width * 8)
    header += b"data" + _struct.pack("<I", len(pcm))
    return header + pcm


@app.get("/api/v1/tts", tags=["tools"], summary="[9] 길안내 음성 합성 (상담 보이스 통일)")
async def synthesize_tts(
    text: str = Query(..., min_length=1, max_length=300),
    voice: str = Query("female", description="male/female 또는 prebuilt voice 이름"),
):
    if ai_client is None:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY 미설정")
    from live_bridge import resolve_voice
    from google.genai import types as _t

    vname = resolve_voice(voice)
    key = (vname, text)
    cached = _TTS_CACHE.get(key)
    if cached is not None:
        _TTS_CACHE.move_to_end(key)
        return Response(content=cached, media_type="audio/wav",
                        headers={"X-TTS-Cache": "hit", "Cache-Control": "max-age=86400"})

    def _gen():
        return ai_client.models.generate_content(
            model=GEMINI_TTS_MODEL,
            contents=text,
            config=_t.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=_t.SpeechConfig(
                    voice_config=_t.VoiceConfig(
                        prebuilt_voice_config=_t.PrebuiltVoiceConfig(voice_name=vname))),
            ),
        )

    try:
        resp = await asyncio.to_thread(_gen)
        part = resp.candidates[0].content.parts[0]
        pcm = part.inline_data.data
        mime = part.inline_data.mime_type or ""
        m = _re.search(r"rate=(\d+)", mime)
        rate = int(m.group(1)) if m else 24000
    except Exception as e:
        logging.getLogger(__name__).warning("TTS 합성 실패(%s): %s", vname, e)
        raise HTTPException(status_code=502, detail="음성 합성에 실패했습니다")

    wav = _pcm_to_wav(pcm, rate)
    _TTS_CACHE[key] = wav
    while len(_TTS_CACHE) > _TTS_CACHE_MAX:
        _TTS_CACHE.popitem(last=False)
    return Response(content=wav, media_type="audio/wav",
                    headers={"X-TTS-Cache": "miss", "Cache-Control": "max-age=86400"})


@app.get("/api/v1/tools/plan_accessible_route", tags=["tools"],
         summary="[7] 현위치 → 목적지 무장애 경로")
async def plan_accessible_route(
    origin_lat: float = Query(...),
    origin_lng: float = Query(...),
    destination_poi_id: str = Query(""),
    destination_place: str = Query(""),
    destination_lat: float = Query(None, description="지도에서 직접 지정한 목적지 위도"),
    destination_lng: float = Query(None, description="지도에서 직접 지정한 목적지 경도"),
    destination_type: str = Query("tour"),
    profile: str = Query("wheelchair_manual"),
    mode: str = Query("", description="walk | walk_bus | walk_bus_subway | ''(자동 추천)"),
):
    return await tool_handlers.tool_plan_accessible_route(
        destination_poi_id=destination_poi_id,
        destination_place=destination_place,
        destination_lat=destination_lat,
        destination_lng=destination_lng,
        destination_type=destination_type,
        profile=profile,
        origin_lat=origin_lat,
        origin_lng=origin_lng,
        mode=mode,
    )


@app.get("/api/v1/tools/search_places", tags=["tools"],
         summary="[7-2] 이름으로 장소 찾기 (목적지·출발지 지정용)")
async def search_places(
    q: str = Query(..., min_length=1, description="장소 이름. 예: '안양시청', '노인종합복지관'"),
    limit: int = Query(8, ge=1, le=20),
):
    """관광지·지하철역·건물(시청·복지관·도서관 등)을 이름으로 찾아 좌표를 돌려준다.

    화면의 장소 검색과 음성 도구가 같은 출처를 쓰도록 02 `/poi/search` 를 그대로 미러한다.
    """
    data = await route_client.poi_search(q, sigungu="안양", limit=limit)
    if data.get("status") == "error":
        return {"status": "error", "query": q, "count": 0, "items": [],
                "message": data.get("message")}
    return {"status": "success", "query": q,
            "count": data.get("count", 0), "items": data.get("items", []),
            "region": data.get("region")}


@app.get("/api/v1/tools/transit_access_points", tags=["tools"],
         summary="[7-1] 휠체어 접근 가능한 정류장·역")
async def transit_access_points(
    lat: float = Query(...),
    lng: float = Query(...),
    radius_m: float = Query(800, ge=50, le=3000),
    profile: str = Query("wheelchair_manual"),
):
    return await route_client.transit_access(lat, lng, radius_m, profile)


# ─────────────────────────────────────────────────────────────
# 수집 장치화 (v1.34.0) — 안내 세션의 부산물이 데이터가 된다.
# 참여자 식별자는 받지도 넘기지도 않는다 (route_id 익명).
# ─────────────────────────────────────────────────────────────
from fastapi import Body as _Body  # noqa: E402


@app.get("/api/v1/tools/bus_arrivals", tags=["tools"],
         summary="[7-3] 정류장 실시간 도착정보·저상버스 (안내 화면 폴링용)")
async def bus_arrivals(
    station_id: str = Query(..., description="GBIS 정류소 ID (경로 스텝 leg_ref.board_station_id)"),
    route_id: str = Query("", description="지정 시 그 노선만"),
):
    """화면의 버스 구간 카드가 20초 간격으로 부른다. 음성 도구 get_bus_arrivals 와 같은 출처(02)를 쓴다."""
    return await tool_handlers.tool_get_bus_arrivals(station_id=station_id, route_id=route_id)


@app.get("/api/v1/tools/station_facilities", tags=["tools"],
         summary="[7-4] 역 편의시설 — 승강기·리프트 출입구, 화장실, 승강장")
async def station_facilities(
    station: str = Query(..., min_length=1, description="역 이름(예: '범계역')"),
):
    return await tool_handlers.tool_get_station_facilities(station=station)


@app.post("/api/v1/nav/track", tags=["collect"],
          summary="[10] 주행 GPS 트랙 업로드 (안내 종료 시 1회)")
async def nav_track(payload: dict = _Body(...)):
    pts = payload.get("points") or []
    if not isinstance(pts, list) or len(pts) > 5000:
        raise HTTPException(status_code=422, detail="points 는 5,000개 이하 배열이어야 합니다")
    # 경로 API 계약 밖 필드는 넘기지 않는다 (개인정보 유입 차단)
    clean = {"route_id": str(payload.get("route_id") or "")[:20],
             "points": pts, "meta": payload.get("meta")}
    if not clean["route_id"]:
        raise HTTPException(status_code=422, detail="route_id 가 필요합니다")
    return await route_client.log_track(clean)


@app.post("/api/v1/nav/report", tags=["collect"],
          summary="[11] 접근성 오류 제보 (원터치)")
async def nav_report(payload: dict = _Body(...)):
    try:
        lat, lng = float(payload.get("lat")), float(payload.get("lng"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="lat/lng 가 필요합니다")
    clean = {"lat": lat, "lng": lng,
             "reason": str(payload.get("reason") or "etc")[:20],
             "detail": (str(payload.get("detail"))[:500]
                        if payload.get("detail") else None),
             "route_id": (str(payload.get("route_id"))[:20]
                          if payload.get("route_id") else None),
             "photo_base64": payload.get("photo_base64"),
             "photo_mime": (str(payload.get("photo_mime"))[:40]
                            if payload.get("photo_mime") else None)}
    return await route_client.report_accessibility(clean)


@app.get("/api/v1/tools/explain_route_segment", tags=["tools"],
         summary="[8] 경로 구간 사유 설명")
async def explain_route_segment(
    route_id: str = Query(...),
    step_idx: int = Query(None),
):
    return await tool_handlers.tool_explain_route_segment(route_id=route_id, step_idx=step_idx)


# ─────────────────────────────────────────────────────────────
# 도구 #1 — search_policies_by_metadata
# ─────────────────────────────────────────────────────────────
@app.get(
    "/api/v1/tools/search_policies_by_metadata",
    tags=["tools"],
    summary="[1] 카테고리·중증도 메타데이터 필터링",
)
async def search_policies_by_metadata(
    category: Optional[str] = Query(None, description="교통/통신/의료/세제/소득지원/활동지원/문화·체육/보육·교육/주거/공공시설/기타"),
    severity: Optional[str] = Query(None, description="'심한 장애(중증)' 또는 '심하지 않은 장애(경증)'"),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(models.WelfarePolicy)
    if category:
        stmt = stmt.where(models.WelfarePolicy.category == category)
    if severity:
        stmt = stmt.where(models.WelfarePolicy.severity_levels.contains([severity]))
    stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    policies = result.scalars().all()

    return {
        "status": "success",
        "tool_name": "search_policies_by_metadata",
        "matched_count": len(policies),
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
            for p in policies
        ],
        "ai_instruction": (
            "위 결과를 3문장 이내로 음성 요약. 정책 ID는 노출 금지. "
            "상세 필요 시 get_policy_details(policy_id) 추가 호출."
        ),
    }


# ─────────────────────────────────────────────────────────────
# 도구 #2 — search_by_keyword (벡터 검색)
# ─────────────────────────────────────────────────────────────
@app.get(
    "/api/v1/tools/search_by_keyword",
    tags=["tools"],
    summary="[2] 자연어 키워드 벡터 검색 (모든 청크 대상)",
)
async def search_by_keyword(
    query: str = Query(..., description="자연어 질문"),
    top_k: int = Query(5, ge=1, le=15),
    expand: bool = Query(
        False,
        description="생활 언어 발화를 정책 어휘로 확장한 뒤 검색. "
                    "곤란·결핍을 호소하는 상태 발화일 때만 true.",
    ),
    db: AsyncSession = Depends(get_db),
):
    search_text = query
    expanded_query = None
    if expand:
        expanded_query = await expand_query(query)
        if expanded_query:
            search_text = expanded_query
    qvec = await asyncio.to_thread(_embed, search_text)
    stmt = (
        select(
            models.PolicyChunk.policy_id,
            models.PolicyChunk.chunk_type,
            models.PolicyChunk.content,
            models.WelfarePolicy.title,
            models.WelfarePolicy.short_summary,
            models.WelfarePolicy.category,
        )
        .join(models.WelfarePolicy, models.PolicyChunk.policy_id == models.WelfarePolicy.id)
        .order_by(models.PolicyChunk.embedding.cosine_distance(qvec))
        .limit(top_k)
    )
    rows = (await db.execute(stmt)).all()
    return {
        "status": "success",
        "tool_name": "search_by_keyword",
        "query": query,
        # 확장이 실제로 적용됐는지 관측 가능하게 노출 (미적용 시 None)
        "expanded_query": expanded_query,
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
        "ai_instruction": (
            "matched_content 우선 활용. 정책 단위로 묶어 3~4문장 음성 요약. "
            "expanded_query 가 있으면 사용자의 상태 발화를 정책 영역으로 해석한 결과이므로, "
            "먼저 어려움에 공감하는 한 문장을 말한 뒤 정책을 안내하고, "
            "마지막에 실제로 어떤 도움이 필요한지 되묻는 질문 하나로 마무리하세요."
        ),
    }


# ─────────────────────────────────────────────────────────────
# 도구 #3 — get_policy_details (Fat 응답)
# ─────────────────────────────────────────────────────────────
@app.get(
    "/api/v1/tools/get_policy_details",
    tags=["tools"],
    summary="[3] 특정 정책 전체 상세 + 출처 + 핵심 요약 한 번에",
)
async def get_policy_details(
    policy_id: str = Query(..., description="예: B001"),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(models.WelfarePolicy).where(models.WelfarePolicy.id == policy_id)
    policy = (await db.execute(stmt)).scalar_one_or_none()
    if not policy:
        raise HTTPException(status_code=404, detail=f"정책 {policy_id} 없음")

    fd = policy.full_data or {}
    # Fat Tool Response — Gemini가 한 번 호출로 음성 답변 작성 가능하도록
    sources_top3 = (fd.get("sources") or [])[:3]
    return {
        "status": "success",
        "tool_name": "get_policy_details",
        "policy_id": policy.id,
        "title": policy.title,
        "summary": policy.short_summary,
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
        "full_details": fd,  # 추가 깊은 정보 필요 시 AI가 직접 참조
        "ai_instruction": (
            "supported_amount, how_to_use, application 을 중심으로 3문장 이내 음성 요약. "
            "sources_top3 의 publisher 만 짧게 언급하고 URL은 음성으로 읽지 말 것. "
            "금액·기준을 말할 때는 last_verified 의 연도를 함께 안내할 것."
        ),
    }


# ─────────────────────────────────────────────────────────────
# 도구 #4 — check_eligibility_criteria
# ─────────────────────────────────────────────────────────────
@app.get(
    "/api/v1/tools/check_eligibility_criteria",
    tags=["tools"],
    summary="[4] 특정 정책의 자격 요건 청크 + 구조화된 메타 한 번에",
)
async def check_eligibility_criteria(
    policy_id: str = Query(..., description="예: B001"),
    db: AsyncSession = Depends(get_db),
):
    # 마스터 메타 (구조화된 빠른 판정용)
    p = (await db.execute(
        select(models.WelfarePolicy).where(models.WelfarePolicy.id == policy_id)
    )).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail=f"정책 {policy_id} 없음")

    # eligibility 청크 본문
    chunks = (await db.execute(
        select(models.PolicyChunk.content)
        .where(models.PolicyChunk.policy_id == policy_id)
        .where(models.PolicyChunk.chunk_type == "eligibility")
    )).scalars().all()

    fd = p.full_data or {}
    return {
        "status": "success",
        "tool_name": "check_eligibility_criteria",
        "policy_id": policy_id,
        "title": p.title,
        # Fat 응답 — 구조화 + 본문 둘 다 제공
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
        "ai_instruction": (
            "structured 필드로 빠른 매칭(중증 여부·연령·소득기준) 후, "
            "eligibility_details 본문에서 미세 조건을 확인해 답변하세요. "
            "사용자가 본인 해당 여부를 물으면 '예/아니요/추가 확인 필요' 셋 중 명확히."
        ),
    }


# ─────────────────────────────────────────────────────────────
# 도구 #5 — find_operating_agencies (벡터 검색)
# ─────────────────────────────────────────────────────────────
@app.get(
    "/api/v1/tools/find_operating_agencies",
    tags=["tools"],
    summary="[5] 지역명·기관명 벡터 검색 (agency_specific + contact 청크)",
)
async def find_operating_agencies(
    query: str = Query(..., description="예: '서울에서 어디서 신청?'"),
    limit: int = Query(3, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
):
    qvec = await asyncio.to_thread(_embed, query)
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
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return {
        "status": "success",
        "tool_name": "find_operating_agencies",
        "query": query,
        "results": [
            {
                "policy_id": r.policy_id,
                "policy_title": r.title,
                "chunk_type": r.chunk_type,
                "agency_info": r.content,
                "metadata": r.metadata_,  # region·agency 등 부가 정보
            }
            for r in rows
        ],
        "ai_instruction": (
            "각 결과의 region/agency 메타와 본문에서 전화번호·신청 채널을 추출해 "
            "사용자에게 가장 가까운 신청처 1~2곳을 음성으로 안내."
        ),
    }


# ─────────────────────────────────────────────────────────────
# Phase 3 — Gemini Live API WebSocket 브릿지
# ─────────────────────────────────────────────────────────────
from live_bridge import handle_live_chat


@app.websocket("/ws/live-chat")
async def websocket_live_chat(websocket: WebSocket, voice: str = None, mode: str = None):
    """클라이언트 ↔ Gemini Live API ↔ DB 도구 실시간 중계.

    Query 파라미터:
      voice — Gemini Live prebuilt voice 이름(예: Charon, Kore) 또는 카테고리(male/female).
              미지정 시 기본값(여성 Kore).
      mode  — 세션 시작 화면. "navi"(이동·관광 길안내)면 경로 안내용 인사말을 사용.

    클라이언트 메시지 포맷:
      {"type":"audio_chunk", "data":"<base64 PCM 16kHz>"}
      {"type":"text", "content":"..."}
      {"type":"end_of_turn"}

    서버 → 클라이언트 메시지 포맷:
      {"type":"audio", "mime_type":"audio/pcm;rate=24000", "data":"<base64>"}
      {"type":"text", "content":"..."}
      {"type":"tool_call", "name":"...", "args":{...}}  (디버그/UX)
      {"type":"turn_complete"}
      {"type":"idle_warning", "message":"..."}
      {"type":"auto_close", "message":"..."}
      {"type":"error", "message":"..."}
    """
    if ai_client is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "GEMINI_API_KEY 미설정"})
        await websocket.close()
        return
    await handle_live_chat(websocket, ai_client, _embed, voice=voice, mode=mode)
