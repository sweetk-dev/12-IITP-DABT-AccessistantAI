# -*- coding: utf-8 -*-
"""Live 세션 중계(live_bridge.handle_live_chat)의 연결 유지 계약 — 가짜 단말·가짜 Gemini 세션으로 검증.

    python3 -m pytest tests/test_live_session.py        (또는 python3 tests/test_live_session.py)

지키는 계약:
  1) 재연결 직후 [SYSTEM:RESUME_ANSWER] 는 **실제 사용자 입력**(음성 전사·text·end_of_turn) 뒤에
     답을 못 받았을 때만 나간다. 길안내 화면의 type:"activity"(하트비트·화면 조작)는 유휴 타이머만
     갱신하고 재개 판정에는 쓰이지 않는다.
  2) 잘못된 클라이언트 메시지 한 건(JSON 아님·객체 아님·키 누락·base64 깨짐·바이너리 프레임)은
     그 메시지만 버린다 — 세션이 재연결로 넘어가지 않고 다음 메시지를 계속 받는다.
  3) 세션이 유지되는 오류(도구 응답 전송 실패)는 {"type":"error","fatal":false} 로 보낸다.
  4) 대화 원문(사용자 전사·상담원 전사·도구 인자 값)은 INFO 이상 로그에 남지 않는다.
"""
import asyncio
import json
import logging
import sys
import types as _t
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# live_bridge 는 Gemini SDK·DB 계층·수치 라이브러리를 import 한다 — 없는 환경에서는 건너뛴다
pytest.importorskip("google.genai")
pytest.importorskip("sqlalchemy.orm")
pytest.importorskip("pgvector")
pytest.importorskip("asyncpg")
pytest.importorskip("numpy")

from fastapi import WebSocketDisconnect                  # noqa: E402
from starlette.websockets import WebSocketState          # noqa: E402

import live_bridge as lb                                 # noqa: E402

_DISCONNECT = object()      # 단말이 연결을 끊음
_END_TURN = object()        # Gemini 응답 스트림 한 턴 끝


# ── 판정 함수(순수) ─────────────────────────────────────────────
def test_resume_only_after_unanswered_user_utterance():
    f = lb.should_resume_answer
    assert f(None, 100.0, 200.0) is False, "사용자 입력이 한 번도 없었으면 재개하지 않는다"
    assert f(150.0, 100.0, 160.0) is True, "말한 뒤 AI 턴이 끝나지 않았으면 재개"
    assert f(90.0, 100.0, 160.0) is False, "이미 답한 발화는 재개 대상이 아니다"
    assert f(100.0, 100.0, 160.0) is False
    assert f(150.0, 100.0, 150.0 + lb.RESUME_WINDOW_SEC) is True
    assert f(150.0, 100.0, 150.0 + lb.RESUME_WINDOW_SEC + 0.1) is False, "오래된 입력은 재개하지 않는다"


def test_parse_client_message_rejects_bad_shapes_without_content():
    ok, why = lb.parse_client_message('{"type":"text","content":"안녕"}')
    assert ok == {"type": "text", "content": "안녕"} and why is None
    assert lb.parse_client_message('{"type":"activity","source":"ui"}')[0] is not None
    for raw in ("not json", "[1,2]", '"str"', "null", '{"type":"audio_chunk"}',
                '{"type":"audio_chunk","data":5}', '{"type":"text"}', '{"type":"text","content":null}'):
        msg, why = lb.parse_client_message(raw)
        assert msg is None and why, raw
    # 사유에는 type 까지만 — 본문은 담지 않는다
    msg, why = lb.parse_client_message('{"type":"text","content":123456789}')
    assert msg is None and "123456789" not in why


# ── 가짜 단말·가짜 Gemini ───────────────────────────────────────
class _FakeWS:
    def __init__(self):
        self.headers = {}
        self.query_params = {}
        self.state = _t.SimpleNamespace()
        self.client_state = WebSocketState.CONNECTED
        self.inq = asyncio.Queue()
        self.sent = []

    async def accept(self):
        pass

    async def receive_text(self):
        item = await self.inq.get()
        if item is _DISCONNECT:
            self.client_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect()
        if isinstance(item, Exception):
            raise item
        return item

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.client_state = WebSocketState.DISCONNECTED


class _FakeSession:
    def __init__(self):
        self.q = asyncio.Queue()
        self.client_content = []      # send_client_content 로 받은 텍스트
        self.audio = []
        self.tool_responses = []
        self.fail_tool_response = False

    async def receive(self):
        while True:
            item = await self.q.get()
            if isinstance(item, Exception):
                raise item
            if item is _END_TURN:
                return
            yield item

    async def send_client_content(self, turns=None, turn_complete=True):
        self.client_content.append(turns[0].parts[0].text)

    async def send_realtime_input(self, **kw):
        self.audio.append(kw)

    async def send_tool_response(self, function_responses=None):
        if self.fail_tool_response:
            raise RuntimeError("전송 실패(시험)")
        self.tool_responses.append(function_responses)


class _FakeClient:
    def __init__(self):
        self.sessions = []
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                s = _FakeSession()
                outer.sessions.append(s)
                return s

            async def __aexit__(self_inner, *exc):
                return False

        self.aio = _t.SimpleNamespace(live=_t.SimpleNamespace(connect=lambda model=None, config=None: _CM()))


def _resp(**server_content):
    """Gemini 응답 한 건 — 지정한 server_content 필드만 채운다."""
    sc = dict(model_turn=None, turn_complete=False, interrupted=False, grounding_metadata=None,
              input_transcription=None, output_transcription=None)
    sc.update(server_content)
    return _t.SimpleNamespace(server_content=_t.SimpleNamespace(**sc), tool_call=None,
                              session_resumption_update=None, go_away=None)


def _tool_call(name, args):
    r = _resp()
    r.tool_call = _t.SimpleNamespace(function_calls=[_t.SimpleNamespace(name=name, args=args, id="c1")])
    return r


async def _until(cond, timeout=5.0):
    t0 = asyncio.get_event_loop().time()
    while not cond():
        if asyncio.get_event_loop().time() - t0 > timeout:
            raise AssertionError("조건이 %.0f초 안에 충족되지 않음" % timeout)
        await asyncio.sleep(0.01)


def _drive(scenario):
    """handle_live_chat 을 가짜 단말·가짜 Gemini 로 돌리고 scenario(ws, client) 를 실행한다."""
    async def main():
        ws, client = _FakeWS(), _FakeClient()
        task = asyncio.create_task(lb.handle_live_chat(ws, client, None, mode="navi", greet=False))
        try:
            await _until(lambda: client.sessions)
            await scenario(ws, client)
        finally:
            ws.inq.put_nowait(_DISCONNECT)
            await asyncio.wait_for(task, 10)
        return ws, client
    return asyncio.run(main())


async def _break_gemini_session(client):
    """현재 Gemini 세션을 끊어 silent 재연결을 일으키고, 새 세션이 열릴 때까지 기다린다."""
    n = len(client.sessions)
    s = client.sessions[-1]
    s.q.put_nowait(_resp())                       # 응답 1건 — '진전 있음'으로 세어 백오프 없이 재연결
    s.q.put_nowait(RuntimeError("세션 종료(시험)"))
    await _until(lambda: len(client.sessions) == n + 1)
    await asyncio.sleep(0.05)                     # 재연결 직후 신호 전송이 끝날 시간
    return client.sessions[-1]


# ── 1) 재개 판정 ────────────────────────────────────────────────
def test_activity_heartbeat_does_not_trigger_resume_answer():
    async def scenario(ws, client):
        for src in ("heartbeat", "ui"):
            ws.inq.put_nowait(json.dumps({"type": "activity", "source": src}))
        await asyncio.sleep(0.05)
        s2 = await _break_gemini_session(client)
        assert "[SYSTEM:RESUME_ANSWER]" not in s2.client_content, \
            "화면 하트비트만 있었는데 재개 신호가 나감: %s" % s2.client_content
    _drive(scenario)


def test_unanswered_text_triggers_resume_answer():
    async def scenario(ws, client):
        ws.inq.put_nowait(json.dumps({"type": "text", "content": "장애인 콜택시 어떻게 불러요"}))
        await _until(lambda: client.sessions[0].client_content)
        s2 = await _break_gemini_session(client)
        assert s2.client_content == ["[SYSTEM:RESUME_ANSWER]"], s2.client_content
    _drive(scenario)


def test_unanswered_voice_transcript_triggers_resume_answer():
    async def scenario(ws, client):
        client.sessions[0].q.put_nowait(_resp(input_transcription=_t.SimpleNamespace(text="화장실 어디야")))
        await _until(lambda: any(p.get("type") == "user_transcript" for p in ws.sent))
        s2 = await _break_gemini_session(client)
        assert s2.client_content == ["[SYSTEM:RESUME_ANSWER]"], s2.client_content
    _drive(scenario)


# ── 2) 잘못된 메시지 ────────────────────────────────────────────
def test_bad_client_messages_are_dropped_and_session_continues():
    async def scenario(ws, client):
        for raw in ("not json", "[1,2]", '{"type":"audio_chunk"}', '{"type":"audio_chunk","data":"a"}',
                    '{"type":"text"}', KeyError("text"), '{"type":"location","lat":"x"}'):
            ws.inq.put_nowait(raw)
        ws.inq.put_nowait(json.dumps({"type": "text", "content": "안녕하세요"}))
        await _until(lambda: client.sessions[0].client_content)
        assert client.sessions[0].client_content == ["안녕하세요"]
        assert len(client.sessions) == 1, "잘못된 메시지 때문에 세션이 재연결됨"
        assert client.sessions[0].audio == [], "해석 못 한 오디오를 Gemini 로 보냄"
    ws, client = _drive(scenario)
    assert len(client.sessions) == 1
    assert not [p for p in ws.sent if p.get("type") == "error"]


# ── 3) 비치명 오류 표시 ─────────────────────────────────────────
def test_tool_response_send_failure_is_marked_non_fatal():
    async def scenario(ws, client):
        s = client.sessions[0]
        s.fail_tool_response = True
        s.q.put_nowait(_tool_call("open_navi_screen", {}))
        await _until(lambda: any(p.get("type") == "error" for p in ws.sent))
    ws, _ = _drive(scenario)
    errs = [p for p in ws.sent if p.get("type") == "error"]
    assert len(errs) == 1 and errs[0].get("fatal") is False, errs
    assert ws.client_state == WebSocketState.DISCONNECTED     # 시험이 끊은 것 — 오류로 닫힌 게 아니다


# ── 4) 로그에 대화 원문을 남기지 않는다 ─────────────────────────
def test_info_logs_do_not_contain_conversation_text(caplog):
    secret_user, secret_ai, secret_arg = "비밀질문문장", "비밀답변문장", "비밀장소이름"

    async def scenario(ws, client):
        s = client.sessions[0]
        s.q.put_nowait(_resp(input_transcription=_t.SimpleNamespace(text=secret_user)))
        s.q.put_nowait(_resp(output_transcription=_t.SimpleNamespace(text=secret_ai)))
        # 선언에 없는 인자(memo)는 걸러지고, 이름만 경고 로그에 남는다
        s.q.put_nowait(_tool_call("open_navi_screen", {"memo": secret_arg}))
        await _until(lambda: s.tool_responses)

    with caplog.at_level(logging.DEBUG):
        _drive(scenario)
    info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    for secret in (secret_user, secret_ai, secret_arg):
        leaked = [m for m in info if secret in m]
        assert not leaked, "INFO 이상 로그에 원문이 남음: %s" % leaked
    assert any("도구 호출: open_navi_screen" in m and "memo" in m for m in info), "도구 이름·인자 키는 남아야 한다"
    assert any("받지 않는 인자" in m and "memo" in m for m in info), "걸러낸 인자 이름이 경고에 없다"
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(secret_user in m for m in debug), "원문은 DEBUG 에서는 볼 수 있어야 한다"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
