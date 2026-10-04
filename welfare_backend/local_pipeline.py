# local_pipeline.py
# Gemini Live 폴백 — 온프레미스 음성 상담 파이프라인.
#
# Gemini Live 연결이 결제 소진·권한 등 "재연결 불가" 사유로 실패할 때,
# 동일한 클라이언트 WebSocket 프로토콜을 그대로 유지한 채
# 로컬 STT + Gemma(ollama) + 로컬 TTS 로 턴 기반 음성 상담을 이어간다.
# (프론트엔드 수정 불필요 — 사용자는 폴백 사실을 알 수 없음: silent)
#
# 구성 (전부 지연 로딩 — 모델이 없어도 앱 부팅은 실패하지 않음):
#   VAD : silero-vad          발화 종료 감지
#   STT : faster-whisper      16kHz PCM → 한국어 텍스트
#   LLM : ollama gemma4       function calling → tool_handlers 재사용
#   TTS : MeloTTS / Piper     텍스트 → 24kHz PCM  (LOCAL_TTS_ENGINE 로 교체)
#
# 환경변수:
#   LIVE_LOCAL_FALLBACK   1/0   (기본 1)  폴백 활성화
#   GEMMA_API_URL         기본 http://ollama:11434
#   LOCAL_LLM_MODEL       기본 $GEMMA_MODEL 또는 gemma4:26b
#   LOCAL_STT_MODEL       기본 medium
#   LOCAL_STT_DEVICE      기본 cpu   (GPU는 Gemma 상주로 여유 없음)
#   LOCAL_STT_COMPUTE     기본 int8
#   LOCAL_TTS_ENGINE      melo|piper (기본 melo)
#   LOCAL_TTS_PIPER_MODEL piper onnx 경로 (piper 사용 시)
#   LOCAL_VAD_SILENCE_MS  기본 1200  (Gemini AAD 와 동일 감각)
import asyncio
import base64
import json
import logging
import os
import threading
from typing import Callable, Optional

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

import trial_recorder

from nav_context import (update_nav_state, current_guidance_result,
                         note_new_route, inject_nav_defaults, inject_handoff)
from tool_handlers import run_tool, summarize_tool_result

logger = logging.getLogger(__name__)

TARGET_TTS_RATE = 24000   # 클라이언트가 기대하는 출력 PCM 레이트 (기존 프로토콜과 동일)
INPUT_RATE = 16000        # 클라이언트가 보내는 입력 PCM 레이트

# 발화가 시작되기 전(무음) 구간에서 입력 버퍼에 남겨 둘 길이(초).
# 단말은 무음에도 PCM 을 계속 보내므로, 자르지 않으면 이용자가 말하지 않는 동안
# 버퍼가 끝없이 커진다(16kHz 16bit 기준 1분에 약 1.9MB, 그 전체가 STT 로 넘어간다).
# 근거: VAD 의 발화 시작 판정은 실제 첫 음절보다 늦다(32ms 프레임 판정 + 단말이 묶어
# 보내는 청크 길이 + 약하게 시작하는 첫 자음). 2초를 남기면 첫 음절이 잘리지 않고,
# STT 가 앞 무음을 스스로 걸러내기에도 충분히 짧다.
PRE_SPEECH_KEEP_SEC = 2.0
PRE_SPEECH_KEEP_BYTES = int(PRE_SPEECH_KEEP_SEC * INPUT_RATE) * 2   # 16bit mono

# 대화 이력에 남길 최근 사용자 턴 수(시스템 프롬프트는 항상 보존).
# 근거: 턴마다 도구 결과 JSON(수 KB)이 이력에 쌓이고 매 요청에 전부 다시 실려 간다.
# 자르지 않으면 긴 세션에서 요청이 계속 커져 응답이 느려지고 모델 문맥 한도를 넘는다.
# 8 은 Live 쪽이 세션 복구 때 넘기는 최근 대화 턴 수(RESEED_MAX_TURNS)와 같은 값이다.
LOCAL_HISTORY_MAX_TURNS = 8
GREETING = ("안녕하세요! 장애인 복지 정책에 대해 궁금한 점이 있으신가요? "
            "필요하신 정보를 정확하게 안내해 드릴게요. 편하게 말씀해 주세요.")

# 로컬 폴백 전용 시스템 프롬프트.
# Gemini Live 용 프롬프트에는 [SYSTEM:GREETING] 등 신호 처리 규칙과 google_search 폴백이
# 들어 있어 온프레미스 Gemma 가 도구 루프에서 인사말을 반복하거나 혼란을 일으킴 →
# 로컬 경로는 아래 간결한 전용 프롬프트를 사용(인사·유휴 처리는 코드가 담당).
LOCAL_SYSTEM_PROMPT = """당신은 대한민국 장애인 복지 정책을 안내하는 음성 상담원입니다.

## 도구 사용 (근거 확보)
사용자 질문에 답하기 전에 아래 DB 도구 중 가장 적합한 하나를 호출해 근거를 확보하세요.
- search_by_keyword: 자연어 질문 전반 (기본 도구)
- search_policies_by_metadata: category(교통/통신/의료/세제/소득지원/활동지원/문화·체육/보육·교육/주거/공공시설/기타)나 severity 가 명시된 경우
- get_policy_details: 특정 정책의 상세(지원 금액·신청 방법)
- check_eligibility_criteria: 자격 요건 판정
- find_operating_agencies: 지역·기관·연락처

한 번 검색해 관련 결과가 나오면 같은 질문으로 도구를 반복 호출하지 말고 바로 답하세요.
도구 결과가 비어 있거나 오류이면, 추측하지 말고 정확히 찾지 못했다고 말한 뒤 보건복지부 129를 안내하세요.

## 답변 규칙
- 도구 결과에 실제로 있는 사실만 사용하세요. 금액·자격·시행일·신청처를 지어내지 마세요.
- 한국어 음성 상담체로 간결하게(2~5문장). 금액·날짜는 발화하기 쉬운 한국어로("월 만 육천원" 등).
- 내부 정책 ID(B001 등)와 URL은 음성으로 읽지 마세요.
- 인사말은 이미 상담 시작에 했으니 다시 하지 말고, 사용자의 질문에 바로 답하세요.
- 새 정책을 처음 안내하거나 신청 절차를 안내할 때만 마지막에 문의처(예: 보건복지부 129)를 한 번 덧붙이세요."""


def local_fallback_enabled() -> bool:
    return os.environ.get("LIVE_LOCAL_FALLBACK", "1").strip() not in ("0", "false", "False", "")


# ─────────────────────────────────────────────────────────────
# 지연 로딩 싱글턴 (프로세스당 1회 로드)
# ─────────────────────────────────────────────────────────────
_stt_model = None
_vad_model = None
_tts_engine = None
# STT·VAD 로드는 작업 스레드에서 실행된다(이벤트 루프를 막지 않으려고 asyncio.to_thread 로
# 부른다). 두 세션이 동시에 폴백으로 넘어오면 같은 모델을 두 번 올릴 수 있으므로 잠근다.
_model_load_lock = threading.Lock()


def _get_stt():
    global _stt_model
    if _stt_model is None:
        with _model_load_lock:
            if _stt_model is None:
                from faster_whisper import WhisperModel
                name = os.environ.get("LOCAL_STT_MODEL", "medium")
                device = os.environ.get("LOCAL_STT_DEVICE", "cpu")
                compute = os.environ.get("LOCAL_STT_COMPUTE", "int8")
                logger.info("🧠 STT 로드: faster-whisper %s (device=%s, compute=%s)", name, device, compute)
                _stt_model = WhisperModel(name, device=device, compute_type=compute)
    return _stt_model


def _get_vad():
    global _vad_model
    if _vad_model is None:
        with _model_load_lock:
            if _vad_model is None:
                from silero_vad import load_silero_vad
                logger.info("🧠 VAD 로드: silero-vad")
                _vad_model = load_silero_vad()
    return _vad_model


def _make_vad_iterator(silence_ms: int):
    """VAD 반복기를 만든다 — 모듈 import 와 모델 로드가 무거워 작업 스레드에서 부른다."""
    from silero_vad import VADIterator
    return VADIterator(_get_vad(), sampling_rate=INPUT_RATE,
                       min_silence_duration_ms=silence_ms)


def _get_tts():
    """TTS 엔진 추상화 — (sample_rate, synth_fn) 반환. synth_fn(text)->float32 mono ndarray."""
    global _tts_engine
    if _tts_engine is not None:
        return _tts_engine
    engine = os.environ.get("LOCAL_TTS_ENGINE", "none").lower()
    if engine in ("none", "off", "text", "disabled", ""):
        # 로컬 음성 미사용 — 텍스트+자막(ai_transcript)만 제공.
        # (정책: 대체 합성음성 대신, Gemini 음성 복구 전까지 음성 생략)
        logger.info("🔇 로컬 TTS 비활성(LOCAL_TTS_ENGINE=%s) — 텍스트+자막만 제공", engine)
        _tts_engine = (0, None)
    elif engine == "piper":
        _tts_engine = _load_piper()
    else:
        _tts_engine = _load_melo()
    return _tts_engine


def _load_melo():
    from melo.api import TTS
    device = os.environ.get("LOCAL_TTS_DEVICE", "cpu")
    logger.info("🧠 TTS 로드: MeloTTS(KR, device=%s)", device)
    tts = TTS(language="KR", device=device)
    spk_id = tts.hps.data.spk2id["KR"]
    sr = tts.hps.data.sampling_rate

    def synth(text: str) -> np.ndarray:
        return np.asarray(tts.tts_to_file(text, spk_id, None, quiet=True), dtype=np.float32)

    return (sr, synth)


def _load_piper():
    from piper import PiperVoice  # piper-tts
    model_path = os.environ.get("LOCAL_TTS_PIPER_MODEL", "/models/piper/ko_KR.onnx")
    logger.info("🧠 TTS 로드: Piper(%s)", model_path)
    voice = PiperVoice.load(model_path)
    sr = voice.config.sample_rate

    def synth(text: str) -> np.ndarray:
        buf = b"".join(voice.synthesize_stream_raw(text))
        return np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0

    return (sr, synth)


def warmup():
    """배포 검증용 — 세 모델을 미리 로드해 import/모델 가용성 확인."""
    _get_stt(); _get_vad(); _get_tts()
    logger.info("✅ 로컬 폴백 파이프라인 warmup 완료")


# ─────────────────────────────────────────────────────────────
# 오디오 유틸
# ─────────────────────────────────────────────────────────────
def _pcm16_to_float32(pcm: bytes) -> np.ndarray:
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _float32_to_pcm16(x: np.ndarray) -> bytes:
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767.0).astype(np.int16).tobytes()


def _resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr or x.size == 0:
        return x
    from scipy.signal import resample_poly
    from math import gcd
    g = gcd(int(src_sr), int(dst_sr))
    return resample_poly(x, dst_sr // g, src_sr // g).astype(np.float32)


# ─────────────────────────────────────────────────────────────
# ollama function-calling 도구 스키마 (tool_handlers 디스패처와 1:1)
# ─────────────────────────────────────────────────────────────
def _ollama_tools() -> list:
    def fn(name, desc, props, required=None):
        p = {"type": "object", "properties": props}
        if required:
            p["required"] = required
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": p}}
    S = {"type": "string"}
    I = {"type": "integer"}
    return [
        fn("search_policies_by_metadata", "카테고리·중증도 메타데이터로 정책 후보를 좁힌다. 분류가 명시된 경우 우선 사용.",
           {"category": {"type": "string", "description": "교통/통신/의료/세제/소득지원/활동지원/문화·체육/보육·교육/주거/공공시설/기타"},
            "severity": {"type": "string", "description": "'심한 장애(중증)' 또는 '심하지 않은 장애(경증)'"},
            "limit": I}),
        fn("search_by_keyword", "자연어 질문 전반에 대한 의미적 벡터 검색. 분류 불명확한 질문에 가장 적합(기본 도구).",
           {"query": S, "top_k": I}, ["query"]),
        fn("get_policy_details", "정책 ID로 상세(지원 금액·신청 방법·출처) 전체를 한 번에 가져온다.",
           {"policy_id": {"type": "string", "description": "예: B001"}}, ["policy_id"]),
        fn("check_eligibility_criteria", "특정 정책의 자격 요건(중증·연령·소득 등)을 구조화+본문으로 반환.",
           {"policy_id": S}, ["policy_id"]),
        fn("find_operating_agencies", "지역·기관 관련 질문에서 운영기관·연락처 청크를 벡터 검색.",
           {"query": S, "limit": I}, ["query"]),
    ] + _ollama_route_tools()


def _ollama_route_tools() -> list:
    """이동경로·관광 도구 — Live 선언(live_bridge._route_tool_declarations)과 1:1.

    폴백에서도 경로 안내가 되어야 한다. 경로 서비스가 꺼져 있으면 Live 와 동일하게
    선언 자체를 하지 않는다(없는 기능을 모델이 약속하지 못하게).
    좌표·route_id 는 선언에 넣지 않는다 — 세션이 아는 값을 서버가 주입한다.
    """
    import route_client
    if not route_client.enabled():
        return []

    def fn(name, desc, props, required=None):
        p = {"type": "object", "properties": props}
        if required:
            p["required"] = required
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": p}}

    S = {"type": "string"}
    I = {"type": "integer"}
    return [
        fn("find_bf_tour_spots",
           "장애 유형에 맞는 무장애 관광지를 추천한다. '갈 만한 곳', '휠체어로 갈 수 있는 곳' 질문에 사용.",
           {"disabilities": {"type": "array", "items": S,
                             "description": "지체장애/휠체어/시각장애/청각장애/영유아동반 중 해당하는 것"},
            "sigungu": {"type": "string", "description": "지역명, 기본 '안양'"},
            "topk": I}),
        fn("plan_accessible_route",
           "출발지에서 목적지까지 무장애 보행 경로를 만든다. 경로 안내가 가능한 지역은 안양시뿐이다. "
           "출발지는 현재 위치가 자동 주입된다. 목적지 poi_id 를 모르면 사용자가 말한 이름을 "
           "destination_place 에 담는다 — 관광지·역뿐 아니라 시청·복지관·도서관 같은 일반 시설도 "
           "이름으로 찾는다. 지어낸 poi_id 를 넣지 않는다.",
           {"destination_poi_id": S, "destination_place": S, "destination_type": S,
            "profile": {"type": "string",
                        "description": "wheelchair_electric(기본, 전동 휠체어)/wheelchair_manual(수동)/crutch/visual/walk"},
            "origin_place": {"type": "string", "description": "사용자가 말로 밝힌 출발지 이름"},
            "origin_station": {"type": "string", "description": "사용자가 역 안(승강장)에 있다고 답했을 때 그 역 이름"},
            "origin_travel": {"type": "string", "description": "타고 온 열차의 진행 방향 north/south(station_nearby.choices), 모르면 비움"},
            "mode": {"type": "string",
                     "description": "walk / walk_subway / walk_bus / walk_bus_subway. \'도보로\'·\'걸어서\' 는 walk, \'지하철로\'·\'버스 말고 지하철\' 은 walk_subway, \'버스로\' 는 walk_bus, \'대중교통으로\' 는 walk_bus_subway. 방식을 말하지 않았을 때만 비운다 (v1.48.0)"}}),
        fn("explain_route_segment",
           "직전에 안내한 경로의 특정 구간이 왜 그렇게(우회·경사·계단) 안내되었는지 설명한다. "
           "안내가 진행 중이면 route_id·step_idx 는 서버가 채우므로 생략한다.",
           {"route_id": S, "step_idx": I}),
        fn("get_current_guidance",
           "진행 중인 길안내의 현재 상태(지금 할 안내·다음 안내·남은 거리·목적지)를 조회한다. "
           "이동 중 \"지금 어디로 가야 해\", \"얼마나 남았어\" 질문에는 반드시 이 도구를 먼저 호출한다.",
           {}),
        fn("find_nearby_transit",
           "주변의 버스 정류장·지하철역을 찾는다. 기준 위치는 현재 위치가 자동 주입된다. "
           "결과의 accessible 이 null 이면 '이용 불가'가 아니라 미판정이다 — "
           "accessible_status(yes/no/unknown) 로만 판단해 안내한다.",
           {"place": {"type": "string", "description": "사용자가 말한 기준 장소 이름"},
            "radius_m": I}),
        fn("find_emergency_support",
           "전동 보장구 충전기·보장구 수리센터·장애인콜택시를 현재 위치 주변에서 찾는다. '배터리가 다 됐어', "
           "'충전할 데 있어', '휠체어가 고장났어', '콜택시 불러줘' 같은 긴급 질의에 먼저 호출한다. "
           "open_hours 가 없으면 운영시간을 지어내지 말고 전화 확인을 권한다.",
           {"situation": {"type": "string", "description": "사용자가 말한 상황 원문"},
            "types": {"type": "string", "description": "charge/repair/calltaxi 콤마 구분. 모르면 비운다"},
            "place": {"type": "string", "description": "사용자가 말한 기준 장소 이름"},
            "radius_m": I}),
        fn("find_toilet",
           "휠체어로 갈 수 있는 화장실(장애인 대·소변기 보유 공중화장실)을 현재 위치 주변에서 찾는다. "
           "'화장실 어디야' 질의에 사용. 역 안 화장실은 get_station_facilities.",
           {"place": {"type": "string", "description": "사용자가 말한 기준 장소 이름"},
            "radius_m": I}),
        fn("find_accessible_restaurants",
           "휠체어로 갈 수 있는 음식점을 찾는다. '휠체어로 들어갈 수 있는 식당' 질의에 먼저 사용. 휠체어 출입은 "
           "yes/no/unknown 3상태이며 unknown 은 못 간다는 뜻이 아니다.",
           {"place": {"type": "string", "description": "사용자가 말한 기준 장소 이름"},
            "radius_m": I,
            "accessible_only": {"type": "boolean", "description": "휠체어 출입이 확인된 곳만"}}),
        fn("check_building_accessibility",
           "특정 건물(주민센터·보건소·우체국·도서관·병원 등)의 장애인 편의시설(주출입구 턱·승강기·장애인 화장실·"
           "주차구역)을 알려 준다. 장애인편의시설 실태조사 기준.",
           {"name": {"type": "string", "description": "건물 이름"}}),
        fn("find_service_providers",
           "안양의 장애인 서비스 제공기관(주간활동·방과후·발달재활 바우처·거주시설·활동지원·직업재활 등)을 찾는다.",
           {"service": {"type": "string", "description": "주간활동/방과후/발달재활/언어/거주/활동지원/직업재활/긴급돌봄"},
            "district": {"type": "string", "description": "만안구 또는 동안구"},
            "name": {"type": "string", "description": "기관 이름 일부"}}),
        fn("find_standard_workplaces",
           "장애인 표준사업장(한국장애인고용공단 인증)을 찾는다. 채용공고는 다루지 않는다.",
           {"keyword": {"type": "string", "description": "업종 낱말"},
            "district": {"type": "string", "description": "만안구 또는 동안구"}}),
        fn("get_bus_arrivals",
           "정류장의 실시간 버스 도착정보와 저상버스 여부를 확인한다. '저상버스 언제 와', '다음 버스 저상이야' "
           "질문에 사용. 안내 중이면 승차 정류장·노선이 자동 주입된다 — station_id 를 지어내지 않는다.",
           {"place": {"type": "string", "description": "사용자가 말한 정류장·장소 이름"},
            "route_name": {"type": "string", "description": "사용자가 물은 버스 번호. 결과를 그 번호로 추린다"}}),
        fn("get_station_facilities",
           "지하철역의 교통약자 편의시설(엘리베이터·리프트 출입구, 장애인화장실 위치, 승강장 안전발판·틈)을 "
           "알려 준다. '○○역 엘리베이터 어디 있어' 질문에 사용.",
           {"station": {"type": "string", "description": "역 이름"}},
           ["station"]),
        fn("open_navi_screen",
           "화면을 이동·관광(지도) 탭으로 전환한다. 사용자가 화면 이동 자체를 명시적으로 요청할 때만 사용한다.",
           {}),
        fn("report_station_position",
           "역 안/밖 질문과 출구 확인에 말로 답한다(화면 버튼 대신). \"역 안이야\", \"나왔어\", \"밖이야\", "
           "\"나가는 중이야\", \"아직이야\" 처럼 말하면 사용한다. \"아직 안 나왔어\" 같은 부정 표현은 exiting. "
           "나가는 중이면 재촉하지 말고 한 문장만 답한다.",
           {"where": {"type": "string", "description": "inside / outside / exiting"},
            "travel": {"type": "string", "description": "타고 온 열차 방향 north / south / unknown (말했을 때만)"}},
           ["where"]),
        fn("report_accessibility_issue",
           "현재 위치의 접근성 문제를 제보로 접수한다. \"여기 턱이 있어\", \"보도가 끊겼어\", "
           "\"신고해줘\" 처럼 현장의 통행 문제를 말하면 사용한다. 위치는 자동 주입되므로 좌표를 만들지 않는다.",
           {"reason": {"type": "string",
                       "description": "curb / no_sidewalk / no_crossing / steep / blocked / etc"},
            "detail": {"type": "string", "description": "사용자가 말한 문제 내용을 한 문장으로"}},
           ["reason"]),
    ]


def trim_pre_speech(buf: bytearray, keep_bytes: int = PRE_SPEECH_KEEP_BYTES) -> None:
    """발화 시작 전 입력 버퍼를 최근 keep_bytes 만 남기고 앞에서 잘라낸다(in-place).

    호출부는 발화 중이 아닐 때만 부른다 — 발화 중 구간은 자르지 않는다.
    16bit 샘플 경계가 어긋나지 않게 짝수 바이트만 잘라낸다.
    """
    excess = len(buf) - keep_bytes
    excess -= excess % 2
    if excess > 0:
        del buf[:excess]


def trim_messages(messages: list, max_turns: int = LOCAL_HISTORY_MAX_TURNS) -> None:
    """대화 이력을 시스템 프롬프트 + 최근 max_turns 개 사용자 턴으로 줄인다(in-place).

    자르는 지점은 항상 role="user" 메시지 바로 앞이다. 한 턴은 user → assistant(tool_calls)
    → tool … → assistant 로 이어지므로, user 경계에서만 자르면 tool_calls 를 가진 assistant
    메시지와 그 tool 응답이 서로 떨어지지 않는다(짝이 끊긴 tool 메시지를 받으면 모델
    서버가 요청을 거절하거나 엉뚱한 답을 낸다).
    맨 앞의 system 메시지들은 그대로 둔다. 사용자 턴이 max_turns 이하면 아무것도 하지 않는다.
    """
    head = 0
    while head < len(messages) and messages[head].get("role") == "system":
        head += 1
    user_idx = [i for i in range(head, len(messages)) if messages[i].get("role") == "user"]
    if len(user_idx) <= max_turns:
        return
    cut = user_idx[-max_turns]
    del messages[head:cut]


# ─────────────────────────────────────────────────────────────
# LLM 턴 처리 — ollama gemma4 chat + 도구호출 루프
# ─────────────────────────────────────────────────────────────
async def _run_llm_turn(messages: list, dispatcher: dict, tracker, on_sources,
                        nav_state: dict = None, user_location: dict = None,
                        on_ui_action=None, session_mode: str = None) -> str:
    """messages(대화 누적)에 사용자 발화가 추가된 상태로 호출.
    도구호출을 최대 4회까지 처리하고 최종 한국어 답변 텍스트를 반환.
    messages 는 in-place 로 갱신(assistant/tool 메시지 append)되어 맥락 유지."""
    import httpx
    url = (os.environ.get("GEMMA_API_URL") or "http://ollama:11434").rstrip("/") + "/api/chat"
    model = os.environ.get("LOCAL_LLM_MODEL") or os.environ.get("GEMMA_MODEL") or "gemma4:26b"
    tools = _ollama_tools()
    timeout = httpx.Timeout(180.0, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for _ in range(4):
            # think=False: gemma4 는 thinking 모델 — 미설정 시 content 가 비어 옴(빈 답변 방지).
            payload = {"model": model, "messages": messages, "tools": tools,
                       "stream": False, "think": False,
                       "options": {"temperature": 0}, "keep_alive": -1}
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            msg = resp.json().get("message", {}) or {}
            tool_calls = msg.get("tool_calls") or []
            # assistant 메시지 기록(도구호출 포함) — 맥락 유지용
            messages.append({"role": "assistant",
                             "content": msg.get("content", "") or "",
                             **({"tool_calls": tool_calls} if tool_calls else {})})
            if not tool_calls:
                return (msg.get("content") or "").strip()
            for tc in tool_calls:
                f = tc.get("function", {}) or {}
                fname = f.get("name", "")
                fargs = f.get("arguments", {}) or {}
                if isinstance(fargs, str):
                    try:
                        fargs = json.loads(fargs)
                    except Exception:
                        fargs = {}
                if not isinstance(fargs, dict):
                    fargs = {}
                # 인자 값(말한 장소·질문 원문)은 남기지 않고 도구 이름과 인자 키만 남긴다
                logger.info("🛠 [로컬] 도구 호출: %s (인자 키=%s)", fname, sorted(fargs))
                # 좌표·현재 구간은 모델이 아니라 세션이 아는 값으로 채운다 (live_bridge 와 동일 규칙)
                _nav = nav_state if nav_state is not None else {}
                _loc = user_location if user_location is not None else {}
                if fname == "plan_accessible_route":
                    if _loc.get("lat") is not None:
                        fargs["origin_lat"] = _loc["lat"]
                        fargs["origin_lng"] = _loc["lng"]
                    else:
                        fargs.pop("origin_lat", None)
                        fargs.pop("origin_lng", None)
                fargs = inject_nav_defaults(fname, fargs, _nav, _loc)
                fargs = inject_handoff(fname, fargs, session_mode)   # 정책상담이면 넘기기 (v2.0.0)

                if fname == "get_current_guidance":
                    # 세션 상태만 읽는 도구 — 디스패처를 거치지 않는다
                    result = current_guidance_result(_nav)
                elif fname not in dispatcher:
                    result = {"error": f"unknown tool: {fname}"}
                else:
                    # Live 와 같은 실행부 — 받지 않는 인자 걸러내기·실행 상한·실패를 값으로
                    result = await run_tool(dispatcher, fname, fargs, log_prefix="[로컬] ")
                    logger.info("✓ [로컬] 도구 %s 결과: %s", fname, summarize_tool_result(result))
                if (fname == "plan_accessible_route" and isinstance(result, dict)
                        and result.get("status") == "success"):
                    note_new_route(_nav, result.get("route_id"))
                if tracker is not None:
                    try:
                        tracker.on_tool_call(fname, fargs, result)
                    except Exception:
                        pass
                if fname == "get_policy_details" and on_sources:
                    await on_sources(result)
                if isinstance(result, dict) and result.get("ui_action"):
                    _ui = result.pop("ui_action")
                    if on_ui_action:
                        await on_ui_action(_ui)
                messages.append({"role": "tool", "content": json.dumps(result, ensure_ascii=False)})

        # 도구 루프 상한 초과(모델이 계속 도구만 호출) — 도구 없이 최종 답변을 강제 생성.
        # (그냥 messages[-1] 을 반환하면 도구 결과 JSON 이 답변으로 새어나감)
        messages.append({"role": "user", "content":
            "[지시] 지금까지 조회한 도구 결과만 근거로, 사용자의 마지막 질문에 대한 최종 답변을 "
            "한국어 음성 상담체로 제공하세요. 인사말을 반복하지 말고, 도구를 더 호출하지 말고, "
            "확실한 정보가 없으면 보건복지부 129를 안내하세요."})
        final_payload = {"model": model, "messages": messages,
                         "stream": False, "think": False,
                         "options": {"temperature": 0}, "keep_alive": -1}
        resp = await client.post(url, json=final_payload)
        resp.raise_for_status()
        content = ((resp.json().get("message", {}) or {}).get("content") or "").strip()
        return content or "죄송합니다. 지금은 정확히 안내드리기 어렵습니다. 보건복지부 129로 문의해 주세요."


# ─────────────────────────────────────────────────────────────
# 세션 오케스트레이터
# ─────────────────────────────────────────────────────────────
class LocalVoiceSession:
    def __init__(self, websocket: WebSocket, dispatcher: dict, embed_fn: Callable,
                 system_instruction: str, tracker_factory: Callable, session_id: str,
                 extract_sources: Callable, prior_history: Optional[list] = None,
                 greet: bool = True, session_mode: str = None):
        self.ws = websocket
        self.session_mode = session_mode      # "navi" 면 이동경로 안내 세션 (v2.0.0)
        self.dispatcher = dispatcher
        # embed_fn·system_instruction 은 받기만 하고 쓰지 않는다 — 호출부와 인자 형식을 맞추기 위해
        # 시그니처에 남겨 둔 것이다. 프롬프트는 아래의 로컬 전용 프롬프트(LOCAL_SYSTEM_PROMPT)를 쓴다.
        self.tracker_factory = tracker_factory
        self.session_id = session_id
        self.extract_sources = extract_sources
        self.greet = greet
        # 로컬 경로는 전용 프롬프트 사용(전달받은 Gemini 프롬프트는 신호규칙 때문에 미사용).
        self.messages = [{"role": "system", "content": LOCAL_SYSTEM_PROMPT}]
        for role, text in (prior_history or []):
            if text and text.strip():
                self.messages.append({"role": "assistant" if role == "model" else "user",
                                      "content": text.strip()})
        self.audio_buf = bytearray()
        # 길안내 세션 상태 — 프런트가 보내는 location·nav_state 를 그대로 보관한다.
        # 경로 도구가 좌표를 지어내지 못하게 하는 근거값(live_bridge 와 동일 계약).
        self.user_location = {}
        self.nav_state = {}
        self.silence_ms = int(os.environ.get("LOCAL_VAD_SILENCE_MS", "1200"))
        self._turn_lock = asyncio.Lock()
        # 발화가 끝났지만 아직 처리(_process_turn)가 버퍼를 가져가지 않은 상태.
        # 앞 턴이 처리 중이면 끝난 발화가 버퍼에서 기다린다 — 그동안 무음 구간 자르기를
        # 하면 기다리던 발화가 잘려 나가므로, 이 값이 True 인 동안은 자르지 않는다.
        self._speech_pending = False

    async def _send(self, payload: dict) -> bool:
        _tr = trial_recorder.of(self.ws)            # 실증 참여자 계정 기록(#318)
        if _tr is not None:
            _tr.on_out(payload)
        try:
            await self.ws.send_json(payload)
            return True
        except (RuntimeError, WebSocketDisconnect):
            return False

    async def _send_sources(self, result):
        items = self.extract_sources(result)
        if items:
            await self._send({"type": "sources", "items": items})

    async def _send_ui_action(self, ui):
        await self._send({"type": "ui_action",
                          "action": (ui or {}).get("action"), "payload": ui})

    async def _speak(self, text: str):
        """텍스트를 화면 전사 + TTS 오디오로 전송."""
        if not text:
            return
        await self._send({"type": "ai_transcript", "content": text})
        try:
            sr, synth = _get_tts()
            if synth is None:
                return   # TTS 비활성 — 텍스트+자막만 제공
            wav = await asyncio.to_thread(synth, text)
            wav = _resample(wav, sr, TARGET_TTS_RATE)
            pcm = _float32_to_pcm16(wav)
        except Exception as e:
            logger.exception("[로컬] TTS 실패(텍스트만 전송): %s", e)
            return
        # 1초 단위 청크로 스트리밍 (기존 오디오 포맷과 동일)
        step = TARGET_TTS_RATE * 2
        for i in range(0, len(pcm), step):
            chunk = pcm[i:i + step]
            await self._send({"type": "audio",
                              "mime_type": f"audio/pcm;rate={TARGET_TTS_RATE}",
                              "data": base64.b64encode(chunk).decode()})

    async def _transcribe(self, pcm: bytes) -> str:
        audio = _pcm16_to_float32(pcm)
        if audio.size < INPUT_RATE // 2:   # 0.5초 미만이면 무시
            return ""
        # 첫 호출은 모델 로드(수 초~수십 초)다 — 이벤트 루프에서 하면 그동안 이 프로세스의
        # 모든 연결(다른 이용자의 상담 포함)이 멈추므로 작업 스레드에서 올린다.
        model = await asyncio.to_thread(_get_stt)

        def _run():
            segments, _ = model.transcribe(audio, language="ko", vad_filter=True)
            return "".join(s.text for s in segments).strip()

        return await asyncio.to_thread(_run)

    async def _process_turn(self):
        """버퍼된 사용자 발화를 STT→LLM→TTS 처리."""
        async with self._turn_lock:
            pcm = bytes(self.audio_buf)
            self.audio_buf.clear()
            self._speech_pending = False     # 버퍼를 가져갔다 — 무음 구간 자르기를 다시 허용
            if not pcm:
                return
            tracker = self.tracker_factory()
            user_text = await self._transcribe(pcm)
            if not user_text:
                return
            # 대화 원문은 운영 로그(INFO)에 남기지 않는다 — 길이만, 원문은 DEBUG 에서만
            logger.info("🎤 [로컬] 사용자 음성→텍스트 수신 (%d자)", len(user_text))
            logger.debug("🎤 [로컬] 사용자 음성→텍스트: %s", user_text)
            await self._send({"type": "user_transcript", "content": user_text})
            if tracker is not None:
                try:
                    tracker.on_user_transcript(user_text, raw=None)
                except Exception:
                    pass
            self.messages.append({"role": "user", "content": user_text})
            trim_messages(self.messages)     # 시스템 프롬프트 + 최근 N턴만 유지
            try:
                answer = await _run_llm_turn(self.messages, self.dispatcher, tracker, self._send_sources,
                                             self.nav_state, self.user_location,
                                             self._send_ui_action, self.session_mode)
            except Exception as e:
                logger.exception("[로컬] LLM 처리 실패: %s", e)
                answer = "죄송합니다. 지금은 정확히 안내드리기 어렵습니다. 보건복지부 129로 문의해 주세요."
            await self._speak(answer)
            await self._send({"type": "turn_complete"})
            if tracker is not None:
                try:
                    # 응답 전사를 넘겨야 '정보 없음' 판정과 ai_final_answer 가 산다 (#253)
                    tracker.on_ai_transcript(answer)
                    await tracker.finalize_turn()
                except Exception:
                    pass

    async def _process_text_turn(self, content: str):
        """text 메시지 한 건을 LLM→TTS 로 처리한다 — 음성 턴과 같은 락 아래에서.

        음성 턴(_process_turn)은 태스크로 따로 돌기 때문에, 락 없이 처리하면 두 턴이
        self.messages 를 동시에 고친다(user/assistant/tool 메시지 순서가 섞이고, 모델에
        짝이 맞지 않는 이력이 넘어간다). 같은 락으로 한 번에 한 턴만 진행시킨다.
        """
        async with self._turn_lock:
            tracker = self.tracker_factory()
            await self._send({"type": "user_transcript", "content": content})
            if tracker is not None:
                try:
                    tracker.on_user_transcript(content, raw=None)
                except Exception:
                    pass
            self.messages.append({"role": "user", "content": content})
            trim_messages(self.messages)     # 시스템 프롬프트 + 최근 N턴만 유지
            try:
                answer = await _run_llm_turn(self.messages, self.dispatcher, tracker, self._send_sources,
                                             self.nav_state, self.user_location,
                                             self._send_ui_action, self.session_mode)
            except Exception as e:
                logger.exception("[로컬] LLM(text) 실패: %s", e)
                answer = "죄송합니다. 지금은 정확히 안내드리기 어렵습니다. 보건복지부 129로 문의해 주세요."
            await self._speak(answer)
            await self._send({"type": "turn_complete"})
            if tracker is not None:
                try:
                    tracker.on_ai_transcript(answer)
                    await tracker.finalize_turn()
                except Exception:
                    pass

    async def run(self):
        logger.info("🔁 [로컬 폴백] 음성 상담 세션 시작 (session_id=%s)", self.session_id)
        # 인사말 (silent 폴백 — Gemini 였을 때와 동일한 인사).
        # 세션 도중 전환(이미 대화 진행됨)이면 인사말 생략하고 맥락만 이어받음.
        if self.greet:
            await self._speak(GREETING)
            await self._send({"type": "turn_complete"})

        vad_iter = None
        try:
            # VAD 모듈 import·모델 로드는 무겁다 — 이벤트 루프를 막지 않게 작업 스레드에서 한다
            vad_iter = await asyncio.to_thread(_make_vad_iterator, self.silence_ms)
        except Exception as e:
            logger.warning("[로컬] VAD 미가용 — end_of_turn 신호에만 의존: %s", e)

        vad_carry = np.zeros(0, dtype=np.float32)
        speech_active = False

        try:
            while True:
                raw = await self.ws.receive_text()
                msg = json.loads(raw)
                mtype = msg.get("type")
                _tr = trial_recorder.of(self.ws)        # 실증 기록(#318)
                if _tr is not None:
                    _tr.on_in(msg)

                if mtype == "audio_chunk":
                    pcm = base64.b64decode(msg["data"])
                    self.audio_buf.extend(pcm)
                    if vad_iter is not None:
                        # 512 샘플(32ms) 프레임 단위로 VAD 판정
                        vad_carry = np.concatenate([vad_carry, _pcm16_to_float32(pcm)])
                        while vad_carry.size >= 512:
                            frame = vad_carry[:512]
                            vad_carry = vad_carry[512:]
                            evt = vad_iter(frame, return_seconds=False)
                            if evt and "start" in evt:
                                speech_active = True
                            elif evt and "end" in evt and speech_active:
                                speech_active = False
                                self._speech_pending = True
                                asyncio.create_task(self._process_turn())
                        # 발화 시작 전(무음) 구간은 최근 N초만 남긴다. 발화 중이거나, 끝난
                        # 발화가 처리를 기다리는 중이면 자르지 않는다. VAD 가 없으면 발화
                        # 경계를 알 수 없어 자르지 않는다(end_of_turn 신호가 버퍼를 비운다).
                        if not speech_active and not self._speech_pending:
                            trim_pre_speech(self.audio_buf)

                elif mtype == "location":
                    try:
                        self.user_location["lat"] = float(msg["lat"])
                        self.user_location["lng"] = float(msg["lng"])
                    except (KeyError, TypeError, ValueError):
                        logger.warning("[로컬] 잘못된 location 메시지 무시")

                elif mtype == "nav_state":
                    try:
                        update_nav_state(self.nav_state, msg)
                    except Exception:
                        logger.warning("[로컬] 잘못된 nav_state 메시지 무시")

                elif mtype == "text":
                    content = msg.get("content", "")
                    if content.strip():
                        await self._process_text_turn(content)

                elif mtype == "end_of_turn":
                    if vad_iter is not None:
                        try:
                            vad_iter.reset_states()
                        except Exception:
                            pass
                    speech_active = False
                    await self._process_turn()

        except WebSocketDisconnect:
            logger.info("[로컬] 클라이언트 WebSocket 종료")
        except Exception as e:
            logger.exception("[로컬] 세션 오류: %s", e)
