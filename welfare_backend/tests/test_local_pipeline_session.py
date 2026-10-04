# -*- coding: utf-8 -*-
"""온프레미스 폴백 세션(local_pipeline.LocalVoiceSession)의 자원·순서 계약.

    python3 tests/test_local_pipeline_session.py

음성 모델(STT·VAD·LLM) 없이 가짜로 바꿔 세션 로직만 검증한다.

지키는 계약:
  1) STT·VAD 모델 로드는 이벤트 루프가 아닌 작업 스레드에서 한다(로드 동안 다른 연결이 멈추지 않게)
  2) 발화가 시작되기 전(무음)의 입력 버퍼는 최근 N초만 남긴다. 발화 중·처리 대기 중에는 자르지 않는다
  3) text 턴은 음성 턴과 같은 락 아래에서 처리한다(대화 이력을 동시에 고치지 않는다)
  4) 대화 이력은 시스템 프롬프트 + 최근 N턴만 유지하고, tool_calls 와 tool 응답 쌍을 끊지 않는다
"""
import asyncio
import base64
import json
import os
import sys
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ROUTE_API_BASE_URL", "http://route-api:18100")
os.environ.setdefault("FEATURE_ROUTE", "1")

for name, attrs in (
    ("sqlalchemy", {"select": lambda *a, **k: None, "or_": lambda *a, **k: None}),
    ("sqlalchemy.ext", {}),
    ("sqlalchemy.ext.asyncio", {"AsyncSession": object}),
    ("database", {"AsyncSessionLocal": None}),
    ("models", {}),
):
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

try:
    import numpy                                           # noqa: F401
except ImportError:                                        # 수치 라이브러리가 없으면 세션을 만들 수 없다
    if "pytest" in sys.modules:
        import pytest
        pytest.skip("numpy 없음", allow_module_level=True)
    print("SKIP — numpy 없음")
    sys.exit(0)

from fastapi import WebSocketDisconnect                    # noqa: E402
from starlette.websockets import WebSocketState            # noqa: E402

import local_pipeline as lp                                # noqa: E402

FAILS = []
_DISCONNECT = object()
_CHUNK_SEC = 0.25
_SILENCE = base64.b64encode(b"\x00\x00" * int(lp.INPUT_RATE * _CHUNK_SEC)).decode()
_CHUNK_BYTES = int(lp.INPUT_RATE * _CHUNK_SEC) * 2


def check(name, fn):
    try:
        fn()
        print("  PASS  %s" % name)
    except AssertionError as e:
        FAILS.append(name)
        print("  FAIL  %s — %s" % (name, e))
    except Exception as e:                     # noqa: BLE001
        FAILS.append(name)
        print("  ERROR %s — %r" % (name, e))


def _run(coro):
    return asyncio.run(coro)


class _WS:
    def __init__(self):
        self.headers, self.query_params = {}, {}
        self.state = types.SimpleNamespace()
        self.client_state = WebSocketState.CONNECTED
        self.inq = asyncio.Queue()
        self.sent = []

    async def receive_text(self):
        item = await self.inq.get()
        if item is _DISCONNECT:
            self.client_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect()
        return item

    async def send_json(self, payload):
        self.sent.append(payload)


def _session(ws, **kw):
    return lp.LocalVoiceSession(websocket=ws, dispatcher={}, embed_fn=None, system_instruction="",
                                tracker_factory=lambda: None, session_id="t",
                                extract_sources=lambda r: [], greet=False, **kw)


class _Vad:
    """VADIterator 대역 — events[i] 를 i 번째 프레임의 판정으로 돌려준다(없으면 None)."""

    def __init__(self, events=None):
        self.events, self.n = dict(events or {}), 0

    def __call__(self, frame, return_seconds=False):
        evt = self.events.get(self.n)
        self.n += 1
        return evt

    def reset_states(self):
        pass


class _Patched:
    """모델 로드·LLM 호출을 가짜로 바꾼다."""

    def __init__(self, vad=None, llm=None):
        self.vad, self.llm = vad, llm
        self.vad_threads, self.stt_threads = [], []

    def __enter__(self):
        p = self

        def make_vad(silence_ms):
            p.vad_threads.append(threading.current_thread())
            if p.vad is None:
                raise RuntimeError("VAD 없음(시험)")
            return p.vad

        class _Stt:
            def transcribe(self, audio, language="ko", vad_filter=True):
                return [types.SimpleNamespace(text="안녕하세요")], None

        def get_stt():
            p.stt_threads.append(threading.current_thread())
            return _Stt()

        async def default_llm(messages, *a, **k):
            messages.append({"role": "assistant", "content": "네"})
            return "네"

        self._saved = (getattr(lp, "_make_vad_iterator", None), lp._get_stt, lp._run_llm_turn, lp._get_tts)
        lp._make_vad_iterator = make_vad
        lp._get_stt = get_stt
        lp._run_llm_turn = self.llm or default_llm
        lp._get_tts = lambda: (0, None)
        return self

    def __exit__(self, *exc):
        lp._make_vad_iterator, lp._get_stt, lp._run_llm_turn, lp._get_tts = self._saved
        return False


async def _until(cond, timeout=5.0):
    t0 = asyncio.get_event_loop().time()
    while not cond():
        if asyncio.get_event_loop().time() - t0 > timeout:
            raise AssertionError("조건이 %.0f초 안에 충족되지 않음" % timeout)
        await asyncio.sleep(0.01)


def _audio_msg():
    return json.dumps({"type": "audio_chunk", "data": _SILENCE})


# ── 1) 모델 로드는 작업 스레드에서 ──────────────────────────────
def t_model_loads_run_off_the_event_loop_thread():
    async def main(p):
        loop_thread = threading.current_thread()
        ws = _WS()
        sess = _session(ws)
        task = asyncio.create_task(sess.run())
        ws.inq.put_nowait(_audio_msg())
        ws.inq.put_nowait(_audio_msg())
        ws.inq.put_nowait(_audio_msg())
        ws.inq.put_nowait(json.dumps({"type": "end_of_turn"}))
        await _until(lambda: any(m.get("type") == "turn_complete" for m in ws.sent))
        ws.inq.put_nowait(_DISCONNECT)
        await asyncio.wait_for(task, 5)
        assert p.vad_threads and all(t is not loop_thread for t in p.vad_threads), \
            "VAD 로드가 이벤트 루프 스레드에서 실행됨"
        assert p.stt_threads and all(t is not loop_thread for t in p.stt_threads), \
            "STT 로드가 이벤트 루프 스레드에서 실행됨"
        assert any(m.get("type") == "user_transcript" for m in ws.sent)
    with _Patched(vad=_Vad()) as p:
        _run(main(p))


# ── 2) 무음 구간 버퍼 상한 ──────────────────────────────────────
def t_trim_pre_speech_keeps_recent_tail_on_sample_boundary():
    buf = bytearray(bytes(range(256)) * 400)             # 102,400 바이트
    tail = bytes(buf[-1000:])
    lp.trim_pre_speech(buf, 1000)
    assert bytes(buf) == tail
    buf = bytearray(b"\x01" * 1003)
    lp.trim_pre_speech(buf, 1000)
    assert len(buf) == 1001, "16bit 샘플 경계(짝수 바이트)로만 잘라야 한다: %d" % len(buf)
    buf = bytearray(b"\x01" * 10)
    lp.trim_pre_speech(buf, 1000)
    assert len(buf) == 10
    assert lp.PRE_SPEECH_KEEP_BYTES == int(lp.PRE_SPEECH_KEEP_SEC * lp.INPUT_RATE) * 2


def _feed(n_chunks, vad):
    """무음 청크 n 개를 보낸 뒤의 세션을 돌려준다(연결은 끊는다)."""
    async def main():
        ws = _WS()
        sess = _session(ws)
        task = asyncio.create_task(sess.run())
        for _ in range(n_chunks):
            ws.inq.put_nowait(_audio_msg())
        await _until(lambda: ws.inq.empty())
        await asyncio.sleep(0.05)
        size = len(sess.audio_buf)
        ws.inq.put_nowait(_DISCONNECT)
        await asyncio.wait_for(task, 5)
        return size
    with _Patched(vad=vad):
        return _run(main())


def t_silence_before_speech_is_capped():
    n = 80                                               # 20초 분량의 무음
    size = _feed(n, _Vad())
    assert n * _CHUNK_BYTES > lp.PRE_SPEECH_KEEP_BYTES * 5
    assert size <= lp.PRE_SPEECH_KEEP_BYTES, "무음인데 버퍼가 계속 커짐: %d 바이트" % size
    assert size == lp.PRE_SPEECH_KEEP_BYTES


def t_buffer_is_not_trimmed_during_speech():
    n = 80
    size = _feed(n, _Vad({0: {"start": 0}}))             # 첫 프레임에서 발화 시작, 끝나지 않음
    assert size == n * _CHUNK_BYTES, "발화 중 구간이 잘림: %d != %d" % (size, n * _CHUNK_BYTES)


def t_finished_utterance_waiting_for_previous_turn_is_not_trimmed():
    """앞 턴이 처리 중이라 끝난 발화가 버퍼에서 기다릴 때, 뒤따르는 무음이 그 발화를 잘라내지 않는다."""
    frames_per_chunk = int(lp.INPUT_RATE * _CHUNK_SEC) // 512     # 청크당 VAD 프레임 수(나머지는 이월)
    speech_chunks = 20                                            # 5초 발화
    end_frame = speech_chunks * frames_per_chunk
    seen = {}

    async def main():
        ws = _WS()
        sess = _session(ws)
        orig_transcribe = sess._transcribe

        async def spy(pcm):
            seen["pcm_len"] = len(pcm)
            return await orig_transcribe(pcm)
        sess._transcribe = spy
        await sess._turn_lock.acquire()                           # 앞 턴이 진행 중인 상태
        task = asyncio.create_task(sess.run())
        for _ in range(speech_chunks + 40):                       # 발화 뒤 10초 무음이 이어진다
            ws.inq.put_nowait(_audio_msg())
        await _until(lambda: ws.inq.empty())
        await asyncio.sleep(0.05)
        assert "pcm_len" not in seen
        sess._turn_lock.release()                                 # 앞 턴이 끝남 → 기다리던 발화 처리
        await _until(lambda: "pcm_len" in seen)
        ws.inq.put_nowait(_DISCONNECT)
        await asyncio.wait_for(task, 5)
    with _Patched(vad=_Vad({0: {"start": 0}, end_frame: {"end": 0}})):
        _run(main())
    assert seen["pcm_len"] >= speech_chunks * _CHUNK_BYTES, \
        "기다리던 발화가 무음 자르기에 잘려 나감: %d 바이트" % seen["pcm_len"]


# ── 3) text 턴도 같은 락 아래에서 ───────────────────────────────
def t_text_turn_waits_for_turn_lock():
    calls = []

    async def llm(messages, *a, **k):
        calls.append([m["content"] for m in messages if m["role"] == "user"])
        return "네"

    async def main():
        ws = _WS()
        sess = _session(ws)
        await sess._turn_lock.acquire()                           # 음성 턴이 진행 중인 상태
        task = asyncio.create_task(sess.run())
        ws.inq.put_nowait(json.dumps({"type": "text", "content": "콜택시 번호 알려줘"}))
        await _until(lambda: ws.inq.empty())
        await asyncio.sleep(0.1)
        assert calls == [], "음성 턴이 진행 중인데 text 턴이 대화 이력을 고치고 LLM 을 호출함"
        assert sess.messages[-1]["role"] == "system", "락을 얻기 전에 이력에 추가함"
        sess._turn_lock.release()
        await _until(lambda: calls)
        await _until(lambda: any(m.get("type") == "turn_complete" for m in ws.sent))
        ws.inq.put_nowait(_DISCONNECT)
        await asyncio.wait_for(task, 5)
        assert calls == [["콜택시 번호 알려줘"]]
        assert not sess._turn_lock.locked()
    with _Patched(vad=None, llm=llm):
        _run(main())


# ── 4) 대화 이력 상한 ───────────────────────────────────────────
def _turn(i, with_tool=True):
    msgs = [{"role": "user", "content": "q%d" % i}]
    if with_tool:
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "t"}}]})
        msgs.append({"role": "tool", "content": "{\"i\": %d}" % i})
    msgs.append({"role": "assistant", "content": "a%d" % i})
    return msgs


def _pairs_intact(messages):
    """tool 메시지는 항상 tool_calls 를 가진 assistant(또는 다른 tool) 바로 뒤에 있어야 한다."""
    for i, m in enumerate(messages):
        if m["role"] == "tool":
            prev = messages[i - 1]
            if not (prev["role"] == "tool" or (prev["role"] == "assistant" and prev.get("tool_calls"))):
                return False
        if m["role"] == "assistant" and m.get("tool_calls"):
            if i + 1 >= len(messages) or messages[i + 1]["role"] != "tool":
                return False
    return True


def t_trim_messages_keeps_system_and_recent_turns_with_tool_pairs():
    sys_msg = {"role": "system", "content": "S"}
    msgs = [sys_msg, {"role": "assistant", "content": "이전 세션 답변"}]
    for i in range(12):
        msgs += _turn(i)
    lp.trim_messages(msgs, 3)
    assert msgs[0] is sys_msg, "시스템 프롬프트가 사라짐"
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["q9", "q10", "q11"]
    assert msgs[1]["role"] == "user", "잘린 이력이 user 가 아닌 메시지로 시작함: %s" % msgs[1]
    assert _pairs_intact(msgs), "tool_calls 와 tool 응답 쌍이 끊김"
    assert len(msgs) == 1 + 3 * 4


def t_trim_messages_noop_when_within_limit():
    msgs = [{"role": "system", "content": "S"}] + _turn(0) + _turn(1, with_tool=False)
    before = list(msgs)
    lp.trim_messages(msgs, 2)
    assert msgs == before
    lp.trim_messages(msgs)                                        # 기본 상한
    assert msgs == before
    empty = []
    lp.trim_messages(empty, 1)
    assert empty == []


def t_session_history_is_bounded_over_many_turns():
    async def llm(messages, *a, **k):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "t"}}]})
        messages.append({"role": "tool", "content": "{}"})
        messages.append({"role": "assistant", "content": "네"})
        return "네"

    async def main():
        ws = _WS()
        sess = _session(ws, prior_history=[("user", "예전 질문"), ("model", "예전 답")])
        task = asyncio.create_task(sess.run())
        total = lp.LOCAL_HISTORY_MAX_TURNS + 12
        for i in range(total):
            ws.inq.put_nowait(json.dumps({"type": "text", "content": "질문 %d" % i}))
        await _until(lambda: sum(1 for m in ws.sent if m.get("type") == "turn_complete") == total)
        ws.inq.put_nowait(_DISCONNECT)
        await asyncio.wait_for(task, 5)
        users = [m["content"] for m in sess.messages if m["role"] == "user"]
        assert len(users) == lp.LOCAL_HISTORY_MAX_TURNS, "이력이 상한 없이 커짐: 사용자 턴 %d" % len(users)
        assert users[-1] == "질문 %d" % (total - 1)
        assert sess.messages[0] == {"role": "system", "content": lp.LOCAL_SYSTEM_PROMPT}
        assert _pairs_intact(sess.messages)
    with _Patched(vad=None, llm=llm):
        _run(main())


if __name__ == "__main__":
    for nm, fn in sorted((k, v) for k, v in list(globals().items())
                         if k.startswith("t_") and callable(v)):
        check(nm, fn)
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("all passed")
