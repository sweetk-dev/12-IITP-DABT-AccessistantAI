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
import struct
from typing import Callable, Optional

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

TARGET_TTS_RATE = 24000   # 클라이언트가 기대하는 출력 PCM 레이트 (기존 프로토콜과 동일)
INPUT_RATE = 16000        # 클라이언트가 보내는 입력 PCM 레이트
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


def _get_stt():
    global _stt_model
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
        from silero_vad import load_silero_vad
        logger.info("🧠 VAD 로드: silero-vad")
        _vad_model = load_silero_vad()
    return _vad_model


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
    ]


# ─────────────────────────────────────────────────────────────
# LLM 턴 처리 — ollama gemma4 chat + 도구호출 루프
# ─────────────────────────────────────────────────────────────
async def _run_llm_turn(messages: list, dispatcher: dict, tracker, on_sources) -> str:
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
                logger.info("🛠 [로컬] 도구 호출: %s(%s)", fname, fargs)
                if fname not in dispatcher:
                    result = {"error": f"unknown tool: {fname}"}
                else:
                    try:
                        result = await dispatcher[fname](**fargs)
                    except Exception as e:
                        logger.exception("[로컬] 도구 실행 실패 %s: %s", fname, e)
                        result = {"error": str(e)}
                if tracker is not None:
                    try:
                        tracker.on_tool_call(fname, fargs, result)
                    except Exception:
                        pass
                if fname == "get_policy_details" and on_sources:
                    await on_sources(result)
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
                 greet: bool = True):
        self.ws = websocket
        self.dispatcher = dispatcher
        self.embed_fn = embed_fn
        self.system_instruction = system_instruction
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
        self.silence_ms = int(os.environ.get("LOCAL_VAD_SILENCE_MS", "1200"))
        self._turn_lock = asyncio.Lock()
        self._closed = False

    async def _send(self, payload: dict) -> bool:
        try:
            await self.ws.send_json(payload)
            return True
        except (RuntimeError, WebSocketDisconnect):
            return False

    async def _send_sources(self, result):
        items = self.extract_sources(result)
        if items:
            await self._send({"type": "sources", "items": items})

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
        model = _get_stt()

        def _run():
            segments, _ = model.transcribe(audio, language="ko", vad_filter=True)
            return "".join(s.text for s in segments).strip()

        return await asyncio.to_thread(_run)

    async def _process_turn(self):
        """버퍼된 사용자 발화를 STT→LLM→TTS 처리."""
        async with self._turn_lock:
            pcm = bytes(self.audio_buf)
            self.audio_buf.clear()
            if not pcm:
                return
            tracker = self.tracker_factory()
            user_text = await self._transcribe(pcm)
            if not user_text:
                return
            logger.info("🎤 [로컬] 사용자 음성→텍스트: %s", user_text)
            await self._send({"type": "user_transcript", "content": user_text})
            if tracker is not None:
                try:
                    tracker.on_user_transcript(user_text, raw=None)
                except Exception:
                    pass
            self.messages.append({"role": "user", "content": user_text})
            try:
                answer = await _run_llm_turn(self.messages, self.dispatcher, tracker, self._send_sources)
            except Exception as e:
                logger.exception("[로컬] LLM 처리 실패: %s", e)
                answer = "죄송합니다. 지금은 정확히 안내드리기 어렵습니다. 보건복지부 129로 문의해 주세요."
            await self._speak(answer)
            await self._send({"type": "turn_complete"})
            if tracker is not None:
                try:
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
            from silero_vad import VADIterator
            vad_iter = VADIterator(_get_vad(), sampling_rate=INPUT_RATE,
                                   min_silence_duration_ms=self.silence_ms)
        except Exception as e:
            logger.warning("[로컬] VAD 미가용 — end_of_turn 신호에만 의존: %s", e)

        vad_carry = np.zeros(0, dtype=np.float32)
        speech_active = False

        try:
            while True:
                raw = await self.ws.receive_text()
                msg = json.loads(raw)
                mtype = msg.get("type")

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
                                asyncio.create_task(self._process_turn())

                elif mtype == "text":
                    content = msg.get("content", "")
                    if content.strip():
                        tracker = self.tracker_factory()
                        await self._send({"type": "user_transcript", "content": content})
                        self.messages.append({"role": "user", "content": content})
                        try:
                            answer = await _run_llm_turn(self.messages, self.dispatcher, tracker, self._send_sources)
                        except Exception as e:
                            logger.exception("[로컬] LLM(text) 실패: %s", e)
                            answer = "죄송합니다. 지금은 정확히 안내드리기 어렵습니다. 보건복지부 129로 문의해 주세요."
                        await self._speak(answer)
                        await self._send({"type": "turn_complete"})
                        if tracker is not None:
                            try:
                                await tracker.finalize_turn()
                            except Exception:
                                pass

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
        finally:
            self._closed = True
