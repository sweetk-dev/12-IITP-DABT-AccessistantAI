# -*- coding: utf-8 -*-
"""실증 참여자 계정 전용 대화·음성 기록 (v1.58.0, #318).

    python3 tests/test_trial_recorder.py

계약:
  1) 설정(TRIAL_RECORD_CLIENTS)에 있는 계정만 기록 대상 — 기본은 아무도 아니다
  2) 사건 기록 — rid·type 검증, 크기 상한, 서버 시각·출처·계정 부착
  3) 웹소켓 기록기 — 상담원 음성·받은 마이크 음성을 파일로, 바이트 위치를 사건에 남긴다
  4) 단말 마이크 조각 저장 — 검증·크기 상한
  5) 음성 누적 상한을 넘으면 음성 저장을 멈추고 사건만 남긴다
  6) 기록 실패는 예외로 새지 않는다
  7) 타임라인 스크립트 — 전사 조각 잇기, 길안내·끊김 표시, 스테레오 믹스에 상담원 음성 배치
"""
import base64
import json
import os
import sys
import tempfile
import time
import types
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

TMP = tempfile.mkdtemp(prefix="trialrec_")
os.environ["TRIAL_RECORD_DIR"] = TMP
os.environ["TRIAL_RECORD_CLIENTS"] = "Trial_A, trial_b"
os.environ["TRIAL_RECORD_MIN_FREE_MB"] = "0"

import trial_recorder as tr          # noqa: E402
import trial_timeline as tl          # noqa: E402

FAILS = []


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


def read_events(rid):
    p = os.path.join(tr.rid_dir(rid), "events.jsonl")
    with open(p, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


class FakeWS:
    def __init__(self, user, qp):
        self.headers = {"x-remote-user": user} if user is not None else {}
        self.query_params = qp
        self.state = types.SimpleNamespace()


# 1) 대상 계정
def t_enabled():
    assert tr.enabled_for("trial_a") and tr.enabled_for(" TRIAL_B ")
    assert not tr.enabled_for("ops_user")
    assert not tr.enabled_for(None) and not tr.enabled_for("")
    old = os.environ.pop("TRIAL_RECORD_CLIENTS")
    try:
        assert not tr.enabled_for("trial_a"), "설정이 없으면 아무도 기록하지 않아야 한다"
    finally:
        os.environ["TRIAL_RECORD_CLIENTS"] = old


check("설정된 계정만 기록 대상(대소문자·공백 무시), 설정 없으면 없음", t_enabled)


def t_base_dir_default():
    old = os.environ.pop("TRIAL_RECORD_DIR")
    os.environ["ROUTE_CLIENT_LOG_PATH"] = "/data/logs/route_client.jsonl"
    try:
        assert tr.base_dir() == "/data/logs/trial"
    finally:
        os.environ["TRIAL_RECORD_DIR"] = old
        os.environ.pop("ROUTE_CLIENT_LOG_PATH", None)


check("기록 폴더 기본값 = 경로 호출 로그와 같은 볼륨의 trial/", t_base_dir_default)


# 2) 사건
def t_events():
    rid = "tTEST_events01"
    n = tr.append_events(rid, [{"type": "navi_play", "text": "앞으로", "tc": 1, "ts": 999, "src": "x"},
                               {"type": "Bad Type"}, "문자열", {"type": "ok_2", "big": "가" * 5000}],
                         "client", "trial_a")
    assert n == 2, n
    ev = read_events(rid)
    assert ev[0]["type"] == "navi_play" and ev[0]["src"] == "client" and ev[0]["user"] == "trial_a"
    assert ev[0]["ts"] != 999, "단말이 보낸 ts 로 서버 시각을 덮으면 안 된다"
    assert ev[1]["big"].endswith("…") and len(ev[1]["big"]) < 700
    assert tr.append_events("../escape", [{"type": "x"}], "client") == 0
    assert tr.append_events("short", [{"type": "x"}], "client") == 0
    assert tr.append_events(rid, "notalist", "client") == 0


check("사건 기록: 서버 시각·출처·계정 부착, 잘못된 type/rid 거부, 큰 값 자르기", t_events)


# 3) 웹소켓 기록기
def t_ws_session():
    ws = FakeWS("TRIAL_A", {"rid": "tTEST_ws00001", "mode": "navi", "voice": "female"})
    s = tr.session_for_ws(ws)
    assert s is not None and tr.of(ws) is s
    a1 = b"\x01\x00" * 100
    a2 = b"\x02\x00" * 50
    s.on_out({"type": "audio", "mime_type": "audio/pcm;rate=24000", "data": base64.b64encode(a1).decode()})
    s.on_out({"type": "ai_transcript", "content": "안녕하세요"})
    s.on_out({"type": "audio", "mime_type": "audio/pcm;rate=24000", "data": base64.b64encode(a2).decode()})
    s.on_out({"type": "location_echo_not_logged"})
    s.on_in({"type": "audio_chunk", "data": base64.b64encode(b"\x05\x00" * 10).decode()})
    s.on_in({"type": "audio_chunk", "data": base64.b64encode(b"\x06\x00" * 10).decode()})
    s.on_in({"type": "text", "content": "화장실 어디예요"})
    s.on_in({"type": "location", "lat": 37.1, "lng": 126.9})
    s.event("echo_dropped", content="요")
    s.close()
    s.on_out({"type": "ai_transcript", "content": "닫힌 뒤"})
    ev = read_events("tTEST_ws00001")
    types_ = [e["type"] for e in ev]
    assert types_[0] == "ws_open" and ev[0]["mode"] == "navi" and ev[0]["user"] == "trial_a"
    aa = [e for e in ev if e["type"] == "ai_audio"]
    assert [e["off"] for e in aa] == [0, 200] and aa[1]["bytes"] == 100
    assert "out_ai_transcript" in types_ and "in_text" in types_ and "echo_dropped" in types_
    assert "in_location" not in types_ and "out_location_echo_not_logged" not in types_
    assert types_.count("in_audio_resume") == 1, "연속 청크마다 사건을 남기면 안 된다"
    assert types_[-1] == "ws_close" and ev[-1]["ai_bytes"] == 300 and ev[-1]["in_bytes"] == 40
    with open(os.path.join(tr.rid_dir("tTEST_ws00001"), "ai_%s.pcm" % s.conn), "rb") as f:
        assert f.read() == a1 + a2
    assert not any(e.get("content") == "닫힌 뒤" for e in ev)


check("웹소켓 기록기: 상담원 음성 파일·바이트 위치, 받은 마이크 파일, 사건 선별, 닫힌 뒤 무시", t_ws_session)


def t_ws_not_target():
    ws = FakeWS("ops_user", {"rid": "tTEST_ops00001"})
    assert tr.session_for_ws(ws) is None
    assert tr.of(ws) is None
    assert not os.path.exists(tr.rid_dir("tTEST_ops00001"))
    ws2 = FakeWS(None, {})
    assert tr.session_for_ws(ws2) is None


check("대상 아닌 계정: 기록기 없음·폴더도 만들지 않음", t_ws_not_target)


def t_ws_bad_rid():
    ws = FakeWS("trial_b", {"rid": "../../etc"})
    s = tr.session_for_ws(ws)
    assert s is not None and s.rid.startswith("ws") and ".." not in s.dir
    s.close()


check("잘못된 rid 는 서버가 새로 만든다(경로 탈출 차단)", t_ws_bad_rid)


# 4) 마이크 조각
def t_mic_part():
    rid = "tTEST_mic0001"
    assert tr.save_mic_part(rid, "rabc12345", 0, b"webmhead", {"t0": 1, "tc": 2, "mime": "audio/webm"})
    assert tr.save_mic_part(rid, "rabc12345", 1, b"more", {})
    assert not tr.save_mic_part(rid, "bad/rec", 2, b"x", {})
    assert not tr.save_mic_part(rid, "rabc12345", -1, b"x", {})
    assert not tr.save_mic_part(rid, "rabc12345", 3, b"", {})
    assert not tr.save_mic_part(rid, "rabc12345", 4, b"x" * (tr.MAX_AUDIO_PART_BYTES + 1), {})
    d = tr.rid_dir(rid)
    assert sorted(os.listdir(d)) == ["events.jsonl", "mic_rabc12345_000000.part", "mic_rabc12345_000001.part"]
    ev = read_events(rid)
    assert ev[0]["type"] == "mic_part" and ev[0]["t0"] == 1 and ev[0]["mime"] == "audio/webm"


check("단말 마이크 조각: 저장·검증·크기 상한", t_mic_part)


# 5) 상한
def t_budget():
    old = tr._bytes_written
    os.environ["TRIAL_RECORD_MAX_MB"] = "1"
    tr._bytes_written = 1024 * 1024 - 10
    try:
        ws = FakeWS("trial_a", {"rid": "tTEST_budget1"})
        s = tr.session_for_ws(ws)
        s.on_out({"type": "audio", "data": base64.b64encode(b"\x00" * 100).decode()})
        s.on_out({"type": "audio", "data": base64.b64encode(b"\x00" * 4).decode()})   # 이미 멈춤
        s.on_out({"type": "turn_complete"})
        s.close()
        types_ = [e["type"] for e in read_events("tTEST_budget1")]
        assert "audio_budget_exceeded" in types_ and "ai_audio" not in types_
        assert "out_turn_complete" in types_, "상한 뒤에도 사건은 남아야 한다"
        assert not tr.save_mic_part("tTEST_budget1", "rabc12345", 0, b"x" * 100, {})
    finally:
        tr._bytes_written = old
        os.environ.pop("TRIAL_RECORD_MAX_MB", None)


check("음성 누적 상한: 음성 저장 중단·사건은 계속", t_budget)


# 6) 실패 삼키기
def t_fail_quiet():
    old = os.environ["TRIAL_RECORD_DIR"]
    blocker = os.path.join(TMP, "blocker_file")
    open(blocker, "w").close()
    os.environ["TRIAL_RECORD_DIR"] = blocker          # 파일 아래에 폴더를 만들 수 없음
    try:
        assert tr.append_events("tTEST_fail0001", [{"type": "x"}], "client") == 0
        assert tr.save_mic_part("tTEST_fail0001", "rabc12345", 0, b"x", {}) is False
        ws = FakeWS("trial_a", {"rid": "tTEST_fail0001"})
        assert tr.session_for_ws(ws) is None
    finally:
        os.environ["TRIAL_RECORD_DIR"] = old


check("저장 실패는 조용히 — 예외 없이 0/False/None", t_fail_quiet)


# 7) 타임라인·믹스
def t_timeline():
    rid = "tTEST_tl00001"
    ws = FakeWS("trial_a", {"rid": rid})
    s = tr.session_for_ws(ws)
    pcm = (b"\x00\x40" * 2400)                           # 0.1초(24kHz) 크기 0.5
    s.on_out({"type": "user_transcript", "content": "화장실 "})
    s.on_out({"type": "user_transcript", "content": "어디예요"})
    s.on_out({"type": "audio", "mime_type": "audio/pcm;rate=24000", "data": base64.b64encode(pcm).decode()})
    s.on_out({"type": "ai_transcript", "content": "가장 가까운 곳은 "})
    s.on_out({"type": "ai_transcript", "content": "시청입니다"})
    s.on_out({"type": "interrupted"})
    s.close()
    now_ms = int(time.time() * 1000)
    tr.append_events(rid, [{"type": "ai_play", "off": 0, "tc": now_ms},
                           {"type": "navi_play", "text": "앞으로 50m", "via": "device", "tc": now_ms + 500},
                           {"type": "navi_cut", "text": "앞으로 50m", "tc": now_ms + 900},
                           {"type": "navi_drop", "text": "다음 안내", "tc": now_ms + 950}], "client", "trial_a")
    out = os.path.join(TMP, "out_tl")
    assert tl.main([tr.rid_dir(rid), "--out", out, "--mix", "--tts-url", ""]) == 0
    md = open(os.path.join(out, "timeline.md"), encoding="utf-8").read()
    assert "| 참여자(서비스 인식) | 화장실 어디예요 |" in md, md
    assert "| 상담원 | 가장 가까운 곳은 시청입니다 |" in md
    assert "앞으로 50m  [기기 음성]" in md and "길안내 끊김: 앞으로 50m" in md
    assert "말하지 못하고 버린 안내: 다음 안내" in md and "상담원 답변 끊김" in md
    assert os.path.isfile(os.path.join(out, "ai_%s.wav" % s.conn))
    with wave.open(os.path.join(out, "mix.wav"), "rb") as w:
        assert w.getnchannels() == 2 and w.getframerate() == 16000
        import numpy as np
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)
    assert np.abs(x[:, 1]).max() > 8000, "상담원 음성이 오른쪽 채널에 놓이지 않았다"
    assert np.abs(x[:, 0]).max() == 0, "단말 녹음이 없으면 왼쪽은 비어야 한다"


check("타임라인: 전사 잇기·길안내·끊김·버린 문장 + 스테레오 믹스에 상담원 음성 배치", t_timeline)


def t_clock_offset():
    evs = [{"src": "client", "tc": 1000, "ts": 5.2}, {"src": "client", "tc": 2000, "ts": 6.05},
           {"src": "server", "ts": 1.0}]
    assert tl.clock_offset_ms(evs) == 4050.0
    assert abs(tl.event_time(evs[0], 4050.0) - 5.05) < 1e-9
    assert tl.event_time(evs[2], 4050.0) == 1.0


check("단말 시계 보정: 도착-단말 시각 차의 최솟값", t_clock_offset)

print("\n%d failed" % len(FAILS))
sys.exit(1 if FAILS else 0)
