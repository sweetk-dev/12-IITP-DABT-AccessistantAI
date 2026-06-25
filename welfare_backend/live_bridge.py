# live_bridge.py
# Gemini Multimodal Live API ↔ FastAPI WebSocket 브릿지.
# Phase 3: 실시간 음성 AI 상담 핵심 로직.
# Phase 5 보강: voice 선택, 초기 인사말, 무응답 자동 종료, "상담" 용어 통일.
#
# ⚠️ SDK 호환성 주의:
#   google-genai SDK 2.x 의 Live API 시그니처는 빠르게 변하고 있습니다.
#   본 코드는 google-genai>=2.5 기준 일반 패턴이지만, SDK 마이너 버전에 따라
#   types.Modality / send_realtime_input / send_tool_response 등의 명칭이
#   미세하게 다를 수 있습니다. 첫 실행 시 콘솔 에러가 나면 그 라인의 메서드명을
#   현재 설치된 SDK 의 dir(session) 결과로 보정해 주세요.
import asyncio
import base64
import json
import logging
import uuid
from typing import Callable

from fastapi import WebSocket, WebSocketDisconnect
from google.genai import types
from starlette.websockets import WebSocketState

from tool_handlers import get_tool_dispatcher
from unresolved_logger import TurnTracker
from database import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def _safe_send_json(websocket: WebSocket, payload: dict) -> bool:
    """클라이언트 disconnect 후의 send 시도를 무해하게 처리.

    Phase 5 보완:
      asyncio.gather 로 묶인 두 펌프 중 client 가 먼저 끊겨도 gemini 쪽 펌프가
      Gemini 응답 스트림을 마저 받아 send_json 시도 → starlette 가
      RuntimeError("Unexpected ASGI message 'websocket.send' ...") 발생.
      websocket 상태를 먼저 확인하고, 그래도 race 가 발생하면 조용히 흡수.
    """
    # client_state 사전 체크는 false positive 가능 (application_state 와 unsync 시점) —
    # try/except 로 흡수하는 게 가장 안정적.
    try:
        await websocket.send_json(payload)
        return True
    except (RuntimeError, WebSocketDisconnect) as e:
        logger.debug("send_json 무시 (연결 종료 상태): %s", e)
        return False


def _extract_sources(result) -> list:
    """도구 결과에서 화면 표시용 출처(기관명+URL) 추출."""
    if not isinstance(result, dict):
        return []
    raw = result.get("sources_top3") or result.get("sources") or []
    out, seen = [], set()
    for sc in raw:
        if not isinstance(sc, dict):
            continue
        url = (sc.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"publisher": (sc.get("publisher") or "출처"), "url": url})
    return out[:5]


def _extract_grounding_sources(gm) -> list:
    """google_search grounding_metadata 에서 웹 출처(title+uri) 추출."""
    out, seen = [], set()
    for ch in (getattr(gm, "grounding_chunks", None) or []):
        web = getattr(ch, "web", None)
        if not web:
            continue
        uri = getattr(web, "uri", None)
        title = getattr(web, "title", None)
        if uri and uri not in seen:
            seen.add(uri)
            out.append({"publisher": (title or uri), "url": uri})
    return out[:5]


# ─────────────────────────────────────────────────────────────
# System Instruction — 보고서 v1.2 §7.2 (Fat Tool Response 원칙)
# ─────────────────────────────────────────────────────────────
SYSTEM_INSTRUCTION = """당신은 대한민국 장애인 복지 정책을 안내하는 전문 음성 상담원입니다.

## 답변 원칙 (절대 규칙)
- **모든 사실 정보는 반드시 도구 호출 결과에서만 사용**. 사전 학습 지식으로 금액·날짜·자격·신청처를 추측·생성하지 마세요.
- 'B001' 같은 내부 정책 ID는 절대 음성으로 읽지 말 것. 정책 제목으로 안내.
- 금액·날짜는 "월 만 육천원", "이천이십육년 일월"처럼 한국어 발화체로.
- URL은 음성으로 읽지 말고 "자세한 내용은 화면의 출처를 참고하세요"로 갈음.
- 도구 응답의 `ai_instruction` 필드가 있으면 그 지시를 우선 따를 것.

### 답변 길이 가이드 (질문 유형별)
- **단순 확인·짧은 사실 질문**("얼마예요?", "어디서 신청해요?", "몇 시까지예요?") → **2~3문장**으로 간결하게.
- **일반 정책 안내·자격 설명** → **3~4문장**으로 핵심 위주.
- **절차·서류·다단계 안내가 필요한 질문**("어떻게 신청해요?", "필요한 서류가 뭐예요?", "절차가 어떻게 돼요?") → **5~7문장 허용**. 단계가 여러 개면 *"첫째, 둘째, 셋째"* 또는 *"1단계, 2단계"* 처럼 음성으로도 구분이 명확하게 번호 매겨 안내.
- **여러 기관·여러 옵션을 비교해야 하는 질문**("어디로 문의해요?", "각 지역 콜번호가 뭐예요?") → 핵심 2~3개만 골라 간추리고, 나머지는 "그 외 자세한 내용은 화면을 참고해 주세요"로 갈음.
- **사용자가 '자세히', '더 알려줘', '구체적으로'라고 요청한 경우** → 위 기본보다 1~2문장 더 추가. 최대 8문장 이내.

너무 짧아 사용자가 *"그게 다예요?"* 라고 묻게 만들지 말고, 너무 길어 듣다 지치게 만들지도 마세요. 질문의 결을 보고 자연스럽게 조절합니다.

## 🔴 답변 우선순위 (반드시 이 순서로)

### 1단계 — **내부 DB 도구 우선 호출 (절대 1순위)**
사용자 질문을 받으면 **무조건 먼저** 다음 5종 중 가장 적합한 DB 도구를 1개 호출하세요. 인사·잡담 외에는 도구 호출 없는 답변 금지.

1. **search_policies_by_metadata** — 카테고리·중증도 명시된 경우.
   - `category` 는 다음 중 **정확히 한 값**: 교통 / 통신 / 의료 / 세제 / 소득지원 / 활동지원 / 문화·체육 / 보육·교육 / 주거 / 공공시설 / 기타. 슬래시(/)로 여러 값 묶지 말 것.
2. **search_by_keyword** — 분류 모호한 자연어 질문 ("환승하면 무료?", "보청기 지원?"). **기본 도구**.
3. **get_policy_details** — 정책 ID 식별 후 상세 필요 시.
4. **check_eligibility_criteria** — "제가 받을 수 있나요?", "자격 요건?" 같은 자격 판정.
5. **find_operating_agencies** — "부산 신청처?", "전화번호?" 지역·기관·연락처.

### 2단계 — **DB 결과 평가 + 안전한 답변 마무리**
DB 도구 응답을 받으면 다음을 판단:
- **결과 충분** → 그 결과만 사용해 한국어 음성 답변 생성. **답변 끝 마무리는 아래 규칙 엄수**.
- **결과 비어있거나 / `"error"` 있거나 / 사용자 질문과 동떨어진 경우** → 3단계로.

#### ⭐ 답변 끝 마무리 (시행주체 추측 절대 금지)

⚠️ **하지 말아야 할 것**:
- `sources[].publisher` 를 시행 주체처럼 말하지 마세요. publisher 는 "정보를 가져온 출처"일 뿐, 정책의 시행 주체가 아닙니다.
- 예: 보건복지부 사업안내 PDF에서 가져온 지하철 무임 정보를 "보건복지부 정책 기준" 이라고 하면 **틀린 안내**. 실제 시행은 서울교통공사·코레일·각 지자체 도시철도공사 등.
- 시행 주체를 임의로 추측·요약하지 마세요. 정확한 주체는 `operating_agencies` 배열이며 지역별로 다 다릅니다.

✅ **반드시 사용할 것 — 다음 둘 중 하나, 또는 두 개 모두**:

**A. 법적 근거 멘트** (있을 때) — 도구 응답의 `legal_basis[0].name` 을 자연스러운 문장으로:
- "이 정책은 장애인복지법에 근거합니다."
- "도시철도법 시행령에 따른 제도입니다."
- "지방세특례제한법 제17조에 따른 감면 제도입니다."

**B. 문의처 안내 멘트** — 도구 응답의 `key_contact` 또는 `contact[0]` 의 `name` + `phone` 을 그대로:
- "자세한 내용은 보건복지부 129로 문의하시면 됩니다."
- "국민건강보험공단 1577-1000으로 문의하실 수 있습니다."
- "KBS 수신료 콜센터 1588-1801로 문의해 보세요."

#### 🚫 문의처·법적근거 멘트 반복 방지 규칙 (중요)

같은 세션 안에서 같은 문장을 답변마다 붙이면 사용자가 피로감을 느낍니다. 다음 규칙으로 **언제 붙일지 직접 판단**하세요.

✅ **문의처 멘트를 붙여야 할 때**:
1. **새로운 정책을 처음 안내하는 답변** (해당 세션에서 한 번도 언급 안 된 정책)
2. **사용자가 명시적으로** "어디로 문의해요?", "전화번호 알려주세요" 같이 연락처를 물었을 때
3. **신청·접수 절차를 안내한 답변** (사용자가 행동을 취해야 하는 단계)
4. **외부 검색(google_search) 폴백을 사용한 답변** (정확도가 낮을 수 있으므로 반드시 문의처 안내)

🚫 **문의처 멘트를 생략해야 할 때**:
- 같은 정책에 대한 **후속 질문**("그럼 금액은요?", "신청 자격은요?", "언제부터요?") — 본 답변만 깔끔하게.
- **단순 확인성 답변**("네, 맞습니다", "아니요, 해당 안 됩니다") — 군더더기 없이.
- **직전 답변에서 이미 같은 문의처를 안내한 경우** — 다시 반복하지 말 것.

✅ **법적 근거 멘트(legal_basis)** — 정책을 처음 소개할 때 1회만 자연스럽게 언급. 같은 세션에서 같은 정책의 후속 질문에는 반복하지 마세요.

#### 권장 패턴 (첫 안내일 때만): "이 정책은 [legal_basis]에 근거합니다. 자세한 내용은 [contact]로 문의하세요."
#### 권장 패턴 (후속 질문): 본 답변만 깔끔하게 제공, 마무리 멘트 생략.

이 규칙으로 AI가 의역·추측할 여지를 차단하면서도, 같은 멘트가 반복되어 사용자가 답답함을 느끼지 않게 합니다.

### 3단계 — **외부 검색 폴백 (google_search)**
DB 결과가 부족하면 아래를 **한 번의 답변 안에서** 자연스럽게 이어서 수행:

(a) 외부 검색으로 확인하겠다고 **짧게 한 번만** 안내. 상황에 맞게 자연스럽게 표현하되, 같은 안내를 **두 번 말하거나 예시 문구를 따옴표째 그대로 다시 읽지 말 것**. (안내 취지: 정책 DB에 정확한 정보가 없어 외부 검색으로 확인한다 — 이 문장을 그대로 외워 읽지 말고 자연스럽게.)

(b) 곧바로 `google_search` 도구를 호출 (Gemini 내장).

(c) **검색 결과의 핵심 내용을 반드시 같은 답변에서 구체적으로 전달**. 안내만 하고 결과 없이 끝내지 말 것.
- 결과가 있으면: 핵심을 요약해 답한 뒤 **마지막에 한 번만** 출처 주의를 덧붙임 (외부 웹 검색 결과라 정확도는 공식 기관 재확인 권장, 보건복지부 129 안내).
- 결과가 없거나 불확실하면: 출처 주의 보일러플레이트를 붙이지 말고, 정직하게 정확한 정보를 찾지 못했다고 말한 뒤 관련 콜센터(보건복지부 129)를 안내.

### 금지사항
- DB 도구를 건너뛰고 바로 `google_search` 부르지 말 것.
- DB 결과 있는데 추가로 자체 지식·외부 검색 섞지 말 것.
- 안내만 하고 **외부 검색 결과 본문을 빠뜨리지 말 것**.
- 같은 안내 문구를 반복하거나, 예시로 제시된 문장을 **따옴표째 그대로 다시 발화하지 말 것**.

## 단계적 라우팅
- 첫 호출로 정보 부족하면 한 번 더 다른 도구를 연쇄 호출해도 됩니다. 단, 음성 침묵을 줄이기 위해 가능한 한 1~2회 안에 답변을 완성하세요.
- 내부 DB로 답변 불가능하다고 판단되면 정직하게 "현재 제 정보로는 정확히 안내드리기 어렵습니다" 라고 말한 뒤, 관련 콜센터(보건복지부 129 등)를 안내하세요.

## 답변 톤
- 친절하지만 군더더기 없이. 사용자가 시각·청각 장애를 가졌을 수 있으므로 명확한 발음·짧은 문장.
- 절대 추측해서 금액·시행일·자격을 지어내지 말 것. 도구가 반환하지 않은 정보는 모른다고 말할 것.

## 시스템 신호(`[SYSTEM]`) 처리 규칙
사용자 발화가 아니라 백엔드가 직접 보내는 메시지가 `[SYSTEM]` 으로 시작하는 경우, 도구 호출 없이 **지시된 문장을 그대로 음성으로 전달**하세요. 종류는 다음 3가지뿐입니다.

1. `[SYSTEM:GREETING]` — 세션이 막 연결된 직후. 다음 인사말을 그대로 한국어 음성으로 한 번만 출력:
   > "안녕하세요! 장애인 복지 정책에 대해 궁금한 점이 있으신가요? 필요하신 정보를 정확하게 안내해 드릴게요. 편하게 말씀해 주세요."

2. `[SYSTEM:IDLE_CHECK]` — 사용자 입력이 3분간 없을 때. 다음 문장을 그대로 음성으로 출력:
   > "혹시 더 도와드릴 일이 있으신가요? 2분 동안 응답이 없으시면 상담을 자동으로 종료할게요."

3. `[SYSTEM:AUTO_CLOSE]` — 추가 2분 무응답으로 자동 종료 직전. 다음 문장을 그대로 음성으로 출력:
   > "응답이 없어 상담을 종료합니다. 다음에 또 이용해 주세요. 감사합니다."

이 세 가지 시스템 신호에는 **DB 도구 호출 금지**, **외부 검색 금지**, **법적 근거·문의처 멘트 금지**. 지정된 문장만 정확히 발화한 뒤 turn 을 종료하세요.
"""


def build_tool_declarations() -> list:
    """Gemini Live API 에 등록할 5종 함수 선언.

    google-genai SDK 의 types.FunctionDeclaration 패턴으로 작성.
    인자 스키마는 도구 디스패처와 정확히 일치해야 함.
    """
    return [
        types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name="search_policies_by_metadata",
                description="카테고리·중증도 메타데이터로 정책 후보를 좁힌다. 사용자가 분류를 명시한 경우 우선 사용.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "category": types.Schema(type=types.Type.STRING, description="교통/통신/의료/세제/소득지원/활동지원/문화·체육/보육·교육/주거/공공시설/기타"),
                        "severity": types.Schema(type=types.Type.STRING, description="'심한 장애(중증)' 또는 '심하지 않은 장애(경증)'"),
                        "limit": types.Schema(type=types.Type.INTEGER, description="최대 반환 개수, 기본 5"),
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="search_by_keyword",
                description="자연어 질문 전반에 대한 의미적 벡터 검색. 분류 불명확한 질문에 가장 적합.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    required=["query"],
                    properties={
                        "query": types.Schema(type=types.Type.STRING, description="사용자 자연어 질문"),
                        "top_k": types.Schema(type=types.Type.INTEGER, description="반환 개수, 기본 5"),
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="get_policy_details",
                description="정책 ID로 상세(지원 금액·신청 방법·출처) 전체를 한 번에 가져온다.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    required=["policy_id"],
                    properties={
                        "policy_id": types.Schema(type=types.Type.STRING, description="예: B001"),
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="check_eligibility_criteria",
                description="특정 정책의 자격 요건(중증·연령·소득 등)을 구조화+본문으로 반환.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    required=["policy_id"],
                    properties={
                        "policy_id": types.Schema(type=types.Type.STRING),
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="find_operating_agencies",
                description="지역·기관 관련 질문에서 운영기관·연락처 청크를 벡터 검색.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    required=["query"],
                    properties={
                        "query": types.Schema(type=types.Type.STRING),
                        "limit": types.Schema(type=types.Type.INTEGER),
                    },
                ),
            ),
        ]),
        # ── 외부 검색 폴백 (Gemini 내장) ──
        # DB 도구로 답을 못 찾을 때만 사용되도록 SYSTEM_INSTRUCTION 으로 우선순위 강제.
        # SDK 버전에 따라 google_search 필드명이 다를 수 있으므로 호환성 fallback 적용.
        _build_google_search_tool(),
    ]


def _build_google_search_tool():
    """SDK 버전별 google_search 도구 객체 빌더 (호환성)."""
    # 시도 순서: google_search → google_search_retrieval → 미지원 시 None
    try:
        return types.Tool(google_search=types.GoogleSearch())
    except Exception:
        pass
    try:
        return types.Tool(google_search_retrieval=types.GoogleSearchRetrieval())
    except Exception:
        pass
    logger.warning("⚠️ SDK 가 google_search 도구를 지원하지 않음 — 외부 검색 비활성")
    return None


# ─────────────────────────────────────────────────────────────
# 사용 가능한 prebuilt voice (Gemini Live API)
# ─────────────────────────────────────────────────────────────
# 보안 차원에서 화이트리스트로 잠가둠 — 클라이언트가 임의 문자열 보내도
# 알 수 없는 값은 기본값으로 폴백.
ALLOWED_VOICES = {
    # 정책 상담 컨셉 (전문성·신뢰감) 권장 쌍
    "Charon": "male",   # 남성 — 깊고 차분, 정보 전달 톤
    "Kore":   "female", # 여성 — 명료하고 정확
    # 보조 옵션 (운영자가 .env 로 바꿀 때 후보)
    "Orus":   "male",
    "Puck":   "male",
    "Fenrir": "male",
    "Aoede":  "female",
    "Leda":   "female",
    "Zephyr": "female",
}
DEFAULT_VOICE_MALE = "Charon"
DEFAULT_VOICE_FEMALE = "Kore"


def resolve_voice(requested: str | None) -> str:
    """클라이언트 요청 voice 값을 안전하게 화이트리스트에 매핑.

    'male'/'female' 같은 카테고리 입력도 받아주고, 알 수 없으면 여성 기본값.
    """
    import os
    if not requested:
        return os.environ.get("GEMINI_LIVE_VOICE", DEFAULT_VOICE_FEMALE)
    req = requested.strip()
    if req in ALLOWED_VOICES:
        return req
    # 카테고리(male/female) 매핑
    if req.lower() == "male":
        return os.environ.get("GEMINI_LIVE_VOICE_MALE", DEFAULT_VOICE_MALE)
    if req.lower() == "female":
        return os.environ.get("GEMINI_LIVE_VOICE_FEMALE", DEFAULT_VOICE_FEMALE)
    logger.warning("⚠️ 알 수 없는 voice 요청 '%s' → 기본값(%s) 사용",
                   requested, DEFAULT_VOICE_FEMALE)
    return DEFAULT_VOICE_FEMALE


async def handle_live_chat(
    websocket: WebSocket,
    ai_client,
    embed_fn: Callable,
    model_name: str = None,
    voice: str = None,
):
    # 환경변수 우선, 기본은 안정 GA 모델
    if model_name is None:
        import os
        model_name = os.environ.get("GEMINI_LIVE_MODEL", "gemini-2.0-flash-live-001")
    selected_voice = resolve_voice(voice)
    logger.info("🎙 선택된 음성: %s (요청='%s')", selected_voice, voice)
    """클라이언트 ↔ Gemini Live ↔ DB 도구 3자 중계.

    1) 클라이언트 → Gemini : 사용자 오디오/텍스트를 Gemini 에 스트리밍 전송
    2) Gemini → 클라이언트 : AI 음성/텍스트 답변을 클라이언트에 전달
    3) Gemini → 도구 호출 : function_call 가로채서 DB 도구 실행 후 결과 회신
    """
    await websocket.accept()
    dispatcher = get_tool_dispatcher(embed_fn)

    # Phase 5 Track A — 세션 발급 + 폴백 적재 추적기
    session_id = uuid.uuid4()
    tracker = TurnTracker(session_id=session_id, session_factory=AsyncSessionLocal)
    logger.info("📌 새 Live 세션 시작: session_id=%s", session_id)

    all_tools = [t for t in build_tool_declarations() if t is not None]
    # transcription 설정: 사용자 음성 + AI 음성 모두 텍스트로 변환받음 (디버깅·UI 표시용)
    config_kwargs = dict(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(
            parts=[types.Part.from_text(text=SYSTEM_INSTRUCTION)]
        ),
        tools=all_tools,
    )
    # SDK 버전에 따라 transcription 설정 명칭이 다름 — 호환성 시도
    _has_output_transcription = False
    try:
        config_kwargs["input_audio_transcription"] = types.AudioTranscriptionConfig()
        config_kwargs["output_audio_transcription"] = types.AudioTranscriptionConfig()
        _has_output_transcription = True
    except AttributeError:
        logger.warning("⚠️ SDK 가 transcription 미지원 — 음성→텍스트 변환 비활성")

    # Phase 5 — 자동 발화 감지(AAD) 명시 설정.
    # 프론트엔드 mic-processor 가 무음 게이트를 제거하고 PCM 을 항상 스트림하므로,
    # 인터럽션·turn 경계 판정 권한을 Gemini AAD 에 100% 일임. 한국어 호흡 길이 +
    # 일반 사무실 노이즈 환경을 고려한 보수적 감도 적용.
    # Voice 적용 — Gemini Live API 의 prebuilt voice 중 하나로 음성 합성.
    # SDK 버전에 따라 SpeechConfig/VoiceConfig/PrebuiltVoiceConfig 구조가 달라질 수 있어
    # 단계별 try 로 호환성 폴백.
    try:
        try:
            speech_cfg = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=selected_voice),
                ),
                language_code="ko-KR",
            )
        except TypeError:
            # language_code 미지원 구버전
            speech_cfg = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=selected_voice),
                ),
            )
        config_kwargs["speech_config"] = speech_cfg
        logger.info("🔊 SpeechConfig 적용 (voice=%s, lang=ko-KR)", selected_voice)
    except (AttributeError, TypeError) as e:
        logger.warning("⚠️ SDK 가 SpeechConfig 미지원 — 기본 음성 사용: %s", e)

    try:
        aad_kwargs = {"disabled": False}
        # 감도 enum — SDK 버전마다 명칭 약간 다를 수 있어 안전하게 시도
        try:
            aad_kwargs["start_of_speech_sensitivity"] = (
                types.StartSensitivity.START_SENSITIVITY_MEDIUM
            )
            aad_kwargs["end_of_speech_sensitivity"] = (
                types.EndSensitivity.END_SENSITIVITY_LOW
            )
        except AttributeError:
            pass
        # 음절 클리핑 방지 패딩 (200ms) + 한국어 호흡 고려 침묵 길이 (1200ms)
        aad_kwargs["prefix_padding_ms"] = 200
        aad_kwargs["silence_duration_ms"] = 1200

        aad = types.AutomaticActivityDetection(**aad_kwargs)
        config_kwargs["realtime_input_config"] = types.RealtimeInputConfig(
            automatic_activity_detection=aad,
        )
        logger.info("✅ Gemini AAD 명시 설정 적용 (prefix=200ms, silence=1200ms)")
    except (AttributeError, TypeError) as e:
        logger.warning("⚠️ SDK 가 AAD 명시 설정 미지원 — 기본값 사용: %s", e)

    # Phase 5 보강 — Context Window Compression 으로 세션 시간 한계 제거.
    # Gemini Live 의 기본 세션 한계(오디오 약 15분 + WebSocket 약 10분)는 본 설정으로
    # 사실상 무제한 대화가 가능해진다. trigger_tokens 도달 시 sliding window 로 압축.
    try:
        config_kwargs["context_window_compression"] = types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(target_tokens=12800),
            trigger_tokens=25600,
        )
        logger.info("✅ ContextWindowCompression 설정 적용 (trigger=25.6k, target=12.8k)")
    except (AttributeError, TypeError) as e:
        logger.warning("⚠️ SDK 가 ContextWindowCompression 미지원 — 기본 한계 적용: %s", e)
    # session_resumption 은 connect 시점에 handle 을 매번 갱신해야 하므로 outer 루프에서 설정.

    config = types.LiveConnectConfig(**config_kwargs)
    # all_tools 는 Tool 객체 단위 카운트 (function_declarations 5개 = 1 Tool, google_search = 1 Tool)
    has_search = any(getattr(t, "google_search", None) or getattr(t, "google_search_retrieval", None) for t in all_tools)
    logger.info("🔧 도구 등록: function_declarations(5) + google_search=%s (Tool 객체 %d개)",
                "✅" if has_search else "❌", len(all_tools))

    # ─────────────────────────────────────────────────────────────
    # Phase 5 보완 — 무입력 자동 종료 + 초기 인사말 트리거
    # ─────────────────────────────────────────────────────────────
    # 한계:
    #   Gemini Live preview 세션 자체는 ~10~15분 내장 한계가 있고, 그 시점에
    #   1008 GoAway 가 떨어집니다. 우리는 그 한계가 오기 전에, 실제로 사용자가
    #   더 이상 말하지 않는 상황을 능동적으로 감지해 깔끔하게 종료시킵니다.
    # 정책:
    #   - IDLE_PROMPT_SEC(=180): 사용자 입력 없이 3분이면 종료 여부를 음성으로 묻음.
    #   - AUTO_CLOSE_SEC(=120):  그 후 추가 2분 더 무응답이면 자동 종료.
    # 활동 판정:
    #   audio_chunk 는 mic-processor 가 무음에서도 항상 전송하므로 신호로 부적합.
    #   진짜 사용자 발화는 Gemini 의 input_transcription 으로 도착하므로 그걸 신호로 사용.
    #   텍스트 입력(type==text)·end_of_turn 도 활동으로 간주.
    IDLE_PROMPT_SEC = 180
    AUTO_CLOSE_SEC = 120
    last_activity_ts = asyncio.get_event_loop().time()
    idle_state = {"prompted": False}  # 종료 확인 음성을 이미 보냈는지

    def mark_user_active(source: str):
        nonlocal last_activity_ts
        last_activity_ts = asyncio.get_event_loop().time()
        if idle_state["prompted"]:
            logger.info("👤 사용자 활동 재개(%s) — idle 카운터 초기화", source)
        idle_state["prompted"] = False

    async def _send_system_signal(session, tag: str):
        """Gemini Live 에 시스템 신호를 user role 메시지로 주입.

        SYSTEM_INSTRUCTION 에 [SYSTEM:GREETING]/[SYSTEM:IDLE_CHECK]/[SYSTEM:AUTO_CLOSE]/[SYSTEM:RECONNECTED]
        네 가지 처리 규칙을 등록해 두었으므로, AI 가 지정된 문장을 그대로 음성 출력.
        """
        try:
            await session.send_client_content(
                turns=[types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=tag)],
                )],
                turn_complete=True,
            )
            logger.info("📨 시스템 신호 전송: %s", tag)
        except Exception as e:
            logger.warning("시스템 신호 전송 실패(%s): %s", tag, e)

    # ─────────────────────────────────────────────────────────────
    # 재연결 루프용 상태 변수 (silent reconnection)
    # ─────────────────────────────────────────────────────────────
    # Gemini Live 의 WebSocket 한계(약 10분 GoAway / 15분 세션) 도달 시
    # 자동으로 같은 handle 로 재연결해 사용자에게 끊김 없는 경험 제공.
    # session_resumption handle 은 server 가 SessionResumptionUpdate 로 보내줌.
    #
    # ⚠️ silent 정책 — 사용자 요청: "이용자 모르게 백단에서 빠르게"
    #   - 재연결 시 음성 안내([SYSTEM:RECONNECTED]) 보내지 않음
    #   - 클라이언트 UI 알림(reconnecting/reconnected) 보내지 않음
    #   - 백오프 sleep 없이 즉시 재연결 시도
    #   - 단, 백엔드 로그에는 재연결 소요시간(ms) 기록 → 운영자 확인용
    session_handle = None
    reconnect_count = 0
    consecutive_failures = 0            # 연속 '무진전' 재연결 횟수 (handle 손상 추정)
    session_progressed = False          # 이번 세션에서 응답/handle 진전이 있었는지
    MAX_CONSECUTIVE_FAILURES = 5        # 초과 시 silent 헛돌이 중단(무한 1007 루프 방지)
    # 컨텍스트 보존 복구(2단계) — handle 폐기 후 새 세션에 직전 대화 맥락을 silent 주입
    convo_history = []                  # [(role, text)] 확정된 대화 턴
    _ai_buf = ""                        # 현재 AI 턴 전사 누적
    _user_buf = ""                      # 현재 사용자 턴 입력 누적
    reseed_context = False              # 새 세션에 맥락 re-seed 필요 여부
    _BASE_SYS = SYSTEM_INSTRUCTION
    RESEED_MAX_TURNS = 8                # 주입할 최근 대화 턴 수(컨텍스트 비대화 방지)

    try:
      while True:
        session_progressed = False  # 이번 연결 시도의 진전 여부 초기화
        # 복구 재시작 시: handle 없이 새 세션을 열되 직전 대화 맥락을 system instruction 에
        # silent 주입(음성 재생/안내 없이 배경 컨텍스트로만) → 대화 내용 보존.
        if reseed_context and convo_history:
            _recent = [(r, t) for r, t in convo_history[-RESEED_MAX_TURNS:] if t.strip()]
            if _recent:
                _ctx = "\n".join(("사용자: " if r == "user" else "상담원: ") + t[:250] for r, t in _recent)
                _sys_text = _BASE_SYS + ("\n\n[복구된 이전 대화 맥락 — 사용자에게 다시 언급하거나 "
                    "반복 발화하지 말고, 이미 나눈 대화로 간주해 자연스럽게 이어가세요]\n" + _ctx)
            else:
                _sys_text = _BASE_SYS
            config_kwargs["system_instruction"] = types.Content(parts=[types.Part.from_text(text=_sys_text)])
            logger.info("🧩 복구 새 세션에 이전 대화 맥락 %d턴 re-seed", len(_recent))
        else:
            config_kwargs["system_instruction"] = types.Content(parts=[types.Part.from_text(text=_BASE_SYS)])
        reseed_context = False
        # session_resumption config 매 연결 시 갱신 (handle 이 None → 새 세션, 있으면 이어받기)
        try:
            config_kwargs["session_resumption"] = types.SessionResumptionConfig(handle=session_handle)
        except (AttributeError, TypeError) as e:
            logger.warning("⚠️ SDK 가 SessionResumption 미지원 — 단순 재연결만 가능: %s", e)
        config = types.LiveConnectConfig(**config_kwargs)

        # 재연결 소요시간 측정 시작
        _connect_t0 = asyncio.get_event_loop().time()
        async with ai_client.aio.live.connect(model=model_name, config=config) as session:
            _connect_elapsed_ms = int((asyncio.get_event_loop().time() - _connect_t0) * 1000)
            if reconnect_count == 0:
                logger.info("✅ Gemini Live 세션 연결됨 (model=%s, %dms)",
                            model_name, _connect_elapsed_ms)
                # ── 세션 시작 인사말 트리거 (DB 도구 호출 없이 지정 문장만 발화) ──
                await _send_system_signal(session, "[SYSTEM:GREETING]")
            else:
                # silent 재연결 — 사용자에게 어떤 알림도 보내지 않음. 로그만 남김.
                logger.info("🔇 Gemini Live silent 재연결 #%d (handle=%s, %dms)",
                            reconnect_count,
                            "이어받음" if session_handle else "신규",
                            _connect_elapsed_ms)

            # ─── 클라이언트 → Gemini ───
            async def pump_client_to_gemini():
                nonlocal _user_buf
                try:
                    while True:
                        raw = await websocket.receive_text()
                        msg = json.loads(raw)
                        # 클라이언트 메시지 포맷 (간단 합의):
                        #  {"type":"audio_chunk", "data": "<base64 PCM 16kHz>"}
                        #  {"type":"text", "content": "..."}
                        if msg.get("type") == "audio_chunk":
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    mime_type="audio/pcm;rate=16000",
                                    data=base64.b64decode(msg["data"]),
                                )
                            )
                            # audio_chunk 자체는 무음 포함이라 활동 신호로 부적합 — 무시.
                        elif msg.get("type") == "text":
                            _user_buf += msg.get("content", "")
                            await session.send_client_content(
                                turns=[types.Content(
                                    role="user",
                                    parts=[types.Part.from_text(text=msg["content"])],
                                )],
                                turn_complete=True,
                            )
                            mark_user_active("text")
                        elif msg.get("type") == "end_of_turn":
                            # 사용자가 말을 끝냈음을 알리는 신호 (VAD 가 없을 때)
                            await session.send_realtime_input(audio_stream_end=True)
                            mark_user_active("end_of_turn")
                except WebSocketDisconnect:
                    logger.info("클라이언트 WebSocket 연결 종료")
                except Exception as e:
                    logger.exception("클라이언트 수신 오류: %s", e)

            # ─── Gemini → 클라이언트 + 도구 실행 ───
            # ⚠️ session.receive() 는 turn 단위 generator — 1 turn 끝나면 종료됨.
            #    multi-turn 대화를 위해 외부 while True 로 감싸 새 turn 마다 재시작.
            async def pump_gemini_to_client():
                # turn_complete 시점에 tracker 를 새 인스턴스로 교체하기 때문에
                # outer scope 의 tracker 를 재바인딩 — nonlocal 선언 필수.
                # session_handle 도 server 가 SessionResumptionUpdate 보낼 때마다 갱신.
                nonlocal tracker, session_handle, session_progressed, _ai_buf, _user_buf
                total_responses = 0
                turn_count = 0
                while True:
                    turn_count += 1
                    logger.info("🔄 receive() turn #%d 대기 시작", turn_count)
                    try:
                        async for response in session.receive():
                            total_responses += 1
                            session_progressed = True  # 1건이라도 응답 수신 → 정상 진전

                            # session_resumption_update — server 가 주기적으로 새 handle 발행.
                            # 이 handle 을 outer 변수에 저장해두면 GoAway 발생 후 같은 handle 로
                            # 재연결해 대화 컨텍스트를 그대로 이어받을 수 있다.
                            sru = getattr(response, "session_resumption_update", None)
                            if sru:
                                new_h = getattr(sru, "new_handle", None) or getattr(sru, "handle", None)
                                if new_h:
                                    session_handle = new_h
                                    logger.debug("📌 session_resumption handle 갱신 (앞 16자: %s...)",
                                                 str(new_h)[:16])

                            # go_away — 세션이 곧 종료될 예정. (참고용 로그)
                            ga = getattr(response, "go_away", None)
                            if ga:
                                time_left = getattr(ga, "time_left", None)
                                logger.warning("⚠️ Gemini GoAway 신호 수신 — 곧 세션 종료 예정 (time_left=%s)",
                                               time_left)

                            sc = getattr(response, "server_content", None)

                            # A) 일반 콘텐츠 (텍스트/오디오)
                            if sc and getattr(sc, "model_turn", None):
                                for part in sc.model_turn.parts:
                                    # AUDIO 모드 + output transcription 활성 시 model_turn 텍스트는
                                    # ai_transcript 와 동일 내용이라 이중 렌더 → 전송 생략(전사 비활성 시에만 사용)
                                    if getattr(part, "text", None) and not _has_output_transcription:
                                        await _safe_send_json(websocket,{"type": "text", "content": part.text})
                                    if getattr(part, "inline_data", None):
                                        audio_b64 = base64.b64encode(part.inline_data.data).decode()
                                        await _safe_send_json(websocket,{
                                            "type": "audio",
                                            "mime_type": part.inline_data.mime_type,
                                            "data": audio_b64,
                                        })
                            if sc and getattr(sc, "turn_complete", False):
                                # 대화 맥락 버퍼에 이번 턴 확정 기록(복구 re-seed 용)
                                if _user_buf.strip():
                                    convo_history.append(("user", _user_buf.strip()))
                                if _ai_buf.strip():
                                    convo_history.append(("model", _ai_buf.strip()))
                                _user_buf = ""; _ai_buf = ""
                                if len(convo_history) > 100:
                                    del convo_history[:-100]
                                logger.info("✅ AI turn #%d 완료", turn_count)
                                await _safe_send_json(websocket,{"type": "turn_complete"})
                                # ── Phase 5 Track A: turn 단위 폴백 적재 ─────────
                                # 현재 tracker 를 finalize 에 넘기고, 다음 turn 을 위해
                                # 새 tracker 인스턴스로 교체 (적재가 비동기로 미뤄져도 race 없음).
                                _t_done = tracker
                                tracker = TurnTracker(session_id=session_id,
                                                      session_factory=AsyncSessionLocal)
                                asyncio.create_task(_t_done.finalize_turn())

                            # ⛔ Barge-in: 사용자가 AI 응답 중 발화하면 Gemini가 자동으로
                            #    interrupted=True 를 보냄 → 클라이언트에 즉시 알려서 재생 큐 비우게.
                            if sc and getattr(sc, "interrupted", False):
                                logger.info("⛔ AI 응답 인터럽트됨 (사용자 발화 감지)")
                                await _safe_send_json(websocket,{"type": "interrupted"})

                            # Grounding metadata (google_search 사용 시)
                            if sc and getattr(sc, "grounding_metadata", None):
                                gm = sc.grounding_metadata
                                logger.info("🔍 google_search 사용 감지: %s", str(gm)[:200])
                                tracker.on_grounding(gm)
                                await _safe_send_json(websocket,{"type": "grounding", "info": str(gm)[:500]})
                                _gsrc = _extract_grounding_sources(gm)
                                if _gsrc:
                                    await _safe_send_json(websocket, {"type": "sources", "items": _gsrc})

                            # 입력 transcription (사용자 음성→텍스트)
                            if sc and getattr(sc, "input_transcription", None):
                                it = sc.input_transcription
                                text = getattr(it, "text", None)
                                if text:
                                    _user_buf += text
                                    logger.info("🎤 사용자 음성→텍스트: %s", text)
                                    tracker.on_user_transcript(text, raw=it)
                                    # ✅ 실제 사용자 발화 — 무입력 타이머 reset
                                    mark_user_active("voice_transcript")
                                    await _safe_send_json(websocket,{"type": "user_transcript", "content": text})

                            # 출력 transcription (AI 음성→텍스트)
                            if sc and getattr(sc, "output_transcription", None):
                                ot = sc.output_transcription
                                text = getattr(ot, "text", None)
                                if text:
                                    _ai_buf += text
                                    logger.info("음성→텍스트: %s", text)
                                    tracker.on_ai_transcript(text)
                                    await _safe_send_json(websocket,{"type": "ai_transcript", "content": text})

                            # B) 도구 호출 — 가로채서 DB 실행 후 결과 회신
                            tc = getattr(response, "tool_call", None)
                            if tc and tc.function_calls:
                                responses = []
                                for fc in tc.function_calls:
                                    fname = fc.name
                                    fargs = dict(fc.args or {})
                                    logger.info("🛠 도구 호출: %s(%s)", fname, fargs)
                                    if fname not in dispatcher:
                                        result = {"error": f"unknown tool: {fname}"}
                                    else:
                                        try:
                                            result = await dispatcher[fname](**fargs)
                                            logger.info("✓ 도구 %s 결과: %s", fname, str(result)[:200])
                                        except Exception as e:
                                            logger.exception("도구 실행 실패 %s: %s", fname, e)
                                            result = {"error": str(e)}
                                    # Phase 5 Track A — 도구 호출 시퀀스 추적
                                    tracker.on_tool_call(fname, fargs, result)
                                    _src = _extract_sources(result)
                                    if _src:
                                        await _safe_send_json(websocket, {"type": "sources", "items": _src})
                                    responses.append(types.FunctionResponse(
                                        id=fc.id,
                                        name=fname,
                                        response=result,
                                    ))
                                    await _safe_send_json(websocket,{"type": "tool_call", "name": fname, "args": fargs})
                                try:
                                    logger.info("📤 send_tool_response 시작 (%d개)", len(responses))
                                    await session.send_tool_response(function_responses=responses)
                                    logger.info("📤 send_tool_response 완료")
                                except AttributeError:
                                    logger.warning("send_tool_response 미지원 — session.send(input=...) fallback")
                                    try:
                                        await session.send(input=responses)
                                    except Exception as e2:
                                        logger.exception("fallback send 도 실패: %s", e2)
                                except Exception as e:
                                    logger.exception("❌ send_tool_response 실패: %s", e)
                                    await _safe_send_json(websocket,{"type": "error", "message": f"도구 응답 전송 실패: {e}"})

                        # async for 정상 종료 — 1 turn 완료, 다음 receive() 대기
                        logger.info("⏸ turn #%d 응답 스트림 종료 — 다음 turn 대기", turn_count)
                    except asyncio.CancelledError:
                        logger.info("Gemini 수신 루프 취소됨")
                        break
                    except Exception as e:
                        # 진단: 1007(invalid argument) 등 근본 원인 특정용 상세 로그
                        logger.error(
                            "Gemini 수신 오류 (turn #%d) — type=%s code=%s msg=%s | "
                            "total_responses=%d handle=%s resumed=%s model=%s",
                            turn_count, type(e).__name__, getattr(e, "code", None), str(e),
                            total_responses, "있음" if session_handle else "없음",
                            (reconnect_count > 0), model_name)
                        logger.exception("Gemini 수신 오류 상세 트레이스 (turn #%d)", turn_count)
                        break
                logger.info("Gemini 수신 루프 최종 종료 (총 %d턴, %d응답)", turn_count, total_responses)

            # ─── 무입력 모니터 ───
            # 10초 간격으로 idle 시간 체크. 3분 무입력 시 종료 확인 음성 1회.
            # 추가 2분 더 무응답 시 WebSocket 정상 종료(코드 1000).
            async def monitor_inactivity():
                try:
                    while True:
                        await asyncio.sleep(10)
                        if websocket.client_state != WebSocketState.CONNECTED:
                            logger.debug("WS 이미 종료됨 — inactivity 모니터 종료")
                            return
                        now = asyncio.get_event_loop().time()
                        idle = now - last_activity_ts

                        if not idle_state["prompted"] and idle >= IDLE_PROMPT_SEC:
                            logger.info("⏱ 무입력 %d초 — 종료 확인 음성 전송", int(idle))
                            idle_state["prompted"] = True
                            await _safe_send_json(websocket, {
                                "type": "idle_warning",
                                "message": "3분간 입력이 없습니다. 2분 더 응답이 없으시면 상담이 자동 종료됩니다.",
                                "auto_close_in_sec": AUTO_CLOSE_SEC,
                            })
                            await _send_system_signal(session, "[SYSTEM:IDLE_CHECK]")

                        elif idle_state["prompted"] and idle >= IDLE_PROMPT_SEC + AUTO_CLOSE_SEC:
                            logger.info("⏱ 무입력 %d초 — 자동 종료 진행", int(idle))
                            await _safe_send_json(websocket, {
                                "type": "auto_close",
                                "reason": "inactivity",
                                "message": "응답이 없어 상담을 자동 종료합니다.",
                            })
                            # 마지막 작별 인사 음성 발화 — Gemini 가 한 turn 만 더 출력하도록.
                            await _send_system_signal(session, "[SYSTEM:AUTO_CLOSE]")
                            # 인사 음성이 클라이언트에 도달할 시간 확보 (대략 turn 1회분)
                            await asyncio.sleep(6)
                            try:
                                await websocket.close(code=1000, reason="inactivity timeout")
                            except Exception as e:
                                logger.debug("WS close 무시: %s", e)
                            return
                except asyncio.CancelledError:
                    logger.debug("inactivity 모니터 취소됨")
                    raise
                except Exception as e:
                    logger.exception("inactivity 모니터 오류: %s", e)

            # return_exceptions=True 로 하나가 끝나도 다른 코루틴이 영향받지 않도록 분리 관리.
            client_task = asyncio.create_task(pump_client_to_gemini())
            gemini_task = asyncio.create_task(pump_gemini_to_client())
            idle_task = asyncio.create_task(monitor_inactivity())
            try:
                # 어느 한 쪽이라도 끝나면 (정상 종료/오류/inactivity) 나머지 정리.
                done, pending = await asyncio.wait(
                    {client_task, gemini_task, idle_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # cancel 정리 대기
                for t in pending:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            finally:
                for t in (client_task, gemini_task, idle_task):
                    if not t.done():
                        t.cancel()

        # ─── async with 종료 후 — 재연결 여부 판단 ───
        # 정상 종료(클라이언트 disconnect / inactivity 자동 종료)면 ws 가 CONNECTED 가 아님 → break
        # Gemini 측만 끊긴 경우(GoAway/1008/세션 만료) ws 는 살아있음 → silent 재연결.
        if websocket.client_state != WebSocketState.CONNECTED:
            logger.info("클라이언트 측 종료 감지 — 재연결 루프 종료 (총 재연결 %d회)",
                        reconnect_count)
            break
        # 직전 세션이 무진전(즉시 무응답 종료)이면 handle 손상 의심 → 연속 실패 누적
        if session_progressed:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            # 재개(handle) 재연결이 무진전이면 handle/컨텍스트 손상 추정 →
            # 다음 시도는 handle 폐기 후 새 세션으로 복구(컨텍스트 초기화되나 영구 멈춤 방지).
            if session_handle is not None:
                logger.warning("⚠️ handle 재개 재연결이 무진전 — handle 폐기 후 새 세션으로 복구(맥락 re-seed) 시도")
                session_handle = None
                reseed_context = True
        # 무한 silent 헛돌이 방지 — 연속 무진전이 상한 초과면 사용자에게 안내 후 종료
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.error("⛔ silent 재연결 %d회 연속 무진전 — handle 손상 추정. 루프 중단 (총 재연결 %d회)",
                         consecutive_failures, reconnect_count)
            try:
                await _safe_send_json(websocket, {"type": "error",
                    "message": "연결이 불안정하여 상담을 이어가지 못했습니다. 잠시 후 다시 시도해 주세요."})
                await websocket.close()
            except Exception:
                pass
            break
        reconnect_count += 1
        # 점증 백오프(최대 2초) — 무한 tight-loop 방지하되 사용자 체감 지연 최소화(silent 유지)
        _backoff = min(0.3 * consecutive_failures, 2.0)
        if _backoff > 0:
            await asyncio.sleep(_backoff)
        logger.info("🔇 Gemini 세션 종료 감지 → silent 재연결 시도 #%d (handle=%s, 연속무진전 %d)",
                    reconnect_count, "이어받음" if session_handle else "신규", consecutive_failures)
        # while True 가 다시 돌면서 새 connect (handle 사용 시 컨텍스트 이어받음)

    except Exception as e:
        logger.exception("Gemini Live 연결 실패 (재연결 불가능한 오류): %s", e)
        try:
            await _safe_send_json(websocket,{"type": "error", "message": str(e)})
            await websocket.close()
        except Exception:
            pass
