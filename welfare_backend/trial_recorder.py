# -*- coding: utf-8 -*-
"""실증 참여자 계정 전용 대화·음성 기록 (v1.58.0, #318).

왜 필요한가
  상담 중 단말은 마이크를 에코 제거를 켠 채 열어 두어 소리를 통화 모드로 처리하고,
  안드로이드는 통화 모드 소리를 화면 녹화에 담지 않는다. 참여자는 이어마이크를 쓰므로
  현장 촬영으로도 안내 음성을 확인할 수 없다. 그래서 실증 참여자 계정으로 접속했을 때만
  서버가 대화와 음성을 남긴다.

무엇을 남기나 (TRIAL_RECORD_DIR/<YYYYMMDD>/<rid>/)
  events.jsonl       — 한 줄에 사건 하나. 서버 시각 ts(초) + src(server/client) + type
  ai_<conn>.pcm      — 서버가 단말로 보낸 상담원 음성(PCM s16le, 기본 24kHz). events 의
                       ai_audio 행이 이 파일의 바이트 위치(off)와 도착 시각을 적는다
  in_<conn>.pcm      — 단말이 서비스로 보낸 마이크 음성(PCM 16kHz). 안내 중에는 단말이
                       걸러 보내지 않으므로 끊긴 구간이 있다 — 서비스가 '들은' 소리
  mic_<rec>_<seq>.part — 단말이 따로 녹음한 마이크 전체(webm/opus 조각). 같은 rec 의
                       조각을 seq 순서로 이으면 한 파일이 된다

원칙
  - 설정(TRIAL_RECORD_CLIENTS)에 있는 계정만 기록한다. 기본값은 비어 있어 아무것도 안 한다.
  - 기록 실패는 조용히 삼킨다 — 상담·길안내 동작에 영향을 주지 않는다.
  - 음성 누적이 TRIAL_RECORD_MAX_MB(기본 4096)를 넘거나 남은 디스크가 TRIAL_RECORD_MIN_FREE_MB(기본 2048)
    아래면 음성 저장을 멈추고 사건 기록만 남긴다.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

_RID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z0-9_]{1,40}$")
MAX_EVENTS_PER_BATCH = 500
MAX_EVENT_BYTES = 4000           # 사건 한 건 직렬화 상한 — 넘으면 잘라서 남긴다
MAX_AUDIO_PART_BYTES = 2 * 1024 * 1024

# 서버→단말 메시지 중 사건으로 남길 것 (audio 는 파일로 따로 저장)
_OUT_TYPES = {"user_transcript", "ai_transcript", "text", "answer_card", "tool_call",
              "turn_complete", "interrupted", "ui_action", "sources", "grounding",
              "idle_warning", "auto_close", "error"}
# 단말→서버 메시지 중 사건으로 남길 것 (audio_chunk 는 파일로 따로 저장, location 은 02 궤적이 맡는다)
_IN_TYPES = {"text", "end_of_turn", "nav_state", "activity"}

_lock = threading.Lock()
_bytes_written = 0               # 프로세스 기준 음성 누적 바이트(디스크 상한 판정)


def _clients() -> set:
    raw = os.environ.get("TRIAL_RECORD_CLIENTS", "")
    return {c.strip().lower() for c in raw.split(",") if c.strip()}


def enabled_for(remote_user) -> bool:
    """이 계정(nginx 가 넣는 X-Remote-User)이 기록 대상인가."""
    if not remote_user:
        return False
    return str(remote_user).strip().lower() in _clients()


def base_dir() -> str:
    d = os.environ.get("TRIAL_RECORD_DIR")
    if d:
        return d
    # 경로 호출 로그와 같은 볼륨에 둔다 (기본 logs/trial)
    log_path = os.environ.get("ROUTE_CLIENT_LOG_PATH", "logs/route_client.jsonl")
    return os.path.join(os.path.dirname(log_path) or "logs", "trial")


def _max_bytes() -> int:
    try:
        return int(os.environ.get("TRIAL_RECORD_MAX_MB", "4096")) * 1024 * 1024
    except ValueError:
        return 4096 * 1024 * 1024


def valid_rid(rid) -> bool:
    return bool(rid) and bool(_RID_RE.match(str(rid)))


def rid_dir(rid: str, day: Optional[str] = None) -> str:
    day = day or time.strftime("%Y%m%d")
    return os.path.join(base_dir(), day, rid)


_free_check = {"ts": 0.0, "ok": True}


def _disk_free_ok() -> bool:
    """남은 디스크가 TRIAL_RECORD_MIN_FREE_MB(기본 2048) 아래면 음성 저장을 멈춘다. 30초마다 확인."""
    now = time.time()
    if now - _free_check["ts"] < 30:
        return _free_check["ok"]
    _free_check["ts"] = now
    try:
        need = int(os.environ.get("TRIAL_RECORD_MIN_FREE_MB", "2048")) * 1024 * 1024
        d = base_dir()
        probe = d if os.path.isdir(d) else (os.path.dirname(d) or ".")
        _free_check["ok"] = shutil.disk_usage(probe).free >= need
    except Exception:
        _free_check["ok"] = True
    return _free_check["ok"]


def _audio_budget_ok(n: int) -> bool:
    global _bytes_written
    if not _disk_free_ok():
        return False
    with _lock:
        if _bytes_written + n > _max_bytes():
            return False
        _bytes_written += n
        return True


def _clip(obj):
    """사건 한 건이 너무 크면 문자열 값을 잘라 상한 안에 맞춘다."""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s.encode("utf-8")) <= MAX_EVENT_BYTES:
        return obj
    out = {}
    for k, v in obj.items():
        if isinstance(v, str) and len(v) > 600:
            out[k] = v[:600] + "…"
        elif isinstance(v, (dict, list)):
            vs = json.dumps(v, ensure_ascii=False)
            out[k] = vs[:600] + "…" if len(vs) > 600 else v
        else:
            out[k] = v
    return out


def append_events(rid: str, events: list, src: str, user: Optional[str] = None) -> int:
    """사건 여러 건을 events.jsonl 에 붙인다. 붙인 건수를 돌려준다(실패 시 0)."""
    if not valid_rid(rid) or not isinstance(events, list):
        return 0
    try:
        d = rid_dir(rid)
        os.makedirs(d, exist_ok=True)
        now = round(time.time(), 3)
        lines = []
        for ev in events[:MAX_EVENTS_PER_BATCH]:
            if not isinstance(ev, dict):
                continue
            etype = str(ev.get("type", ""))
            if not _EVENT_TYPE_RE.match(etype):
                continue
            rec = {"ts": now, "src": src, **{k: v for k, v in ev.items() if k not in ("ts", "src")}}
            if user:
                rec["user"] = user
            lines.append(json.dumps(_clip(rec), ensure_ascii=False))
        if not lines:
            return 0
        with _lock:
            with open(os.path.join(d, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        return len(lines)
    except Exception as e:                       # 기록 실패는 서비스에 영향 주지 않는다
        logger.warning("실증 기록 사건 저장 실패: %s", e)
        return 0


def save_mic_part(rid: str, rec: str, seq: int, data: bytes, meta: dict) -> bool:
    """단말이 따로 녹음한 마이크 조각을 저장한다."""
    if not valid_rid(rid) or not valid_rid(rec):
        return False
    if not isinstance(seq, int) or seq < 0 or seq > 100000:
        return False
    if not data or len(data) > MAX_AUDIO_PART_BYTES:
        return False
    if not _audio_budget_ok(len(data)):
        logger.warning("실증 기록 디스크 상한 도달 — 마이크 조각 저장 중단")
        return False
    try:
        d = rid_dir(rid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "mic_%s_%06d.part" % (rec, seq)), "wb") as f:
            f.write(data)
        append_events(rid, [{"type": "mic_part", "rec": rec, "seq": seq, "bytes": len(data),
                             **{k: meta[k] for k in ("t0", "tc", "mime") if k in meta}}], "client")
        return True
    except Exception as e:
        logger.warning("실증 기록 마이크 조각 저장 실패: %s", e)
        return False


class TrialSession:
    """웹소켓 한 연결의 기록기. websocket.state.trial_rec 에 붙여 쓴다."""

    def __init__(self, rid: str, user: str, mode: Optional[str] = None, voice: Optional[str] = None):
        self.rid = rid
        self.user = user
        self.conn = uuid.uuid4().hex[:8]
        self.dir = rid_dir(rid)
        self._ai = None
        self._in = None
        self.ai_bytes = 0
        self.in_bytes = 0
        self._audio_off = False
        self.closed = False
        os.makedirs(self.dir, exist_ok=True)
        self.event("ws_open", conn=self.conn, mode=mode, voice=voice)

    # ── 사건 ──
    def event(self, etype: str, **kw) -> None:
        if self.closed:
            return
        append_events(self.rid, [{"type": etype, "conn": self.conn, **kw}], "server", self.user)

    # ── 음성 파일 ──
    def _write(self, which: str, data: bytes) -> Optional[int]:
        if self._audio_off or not data:
            return None
        if not _audio_budget_ok(len(data)):
            self._audio_off = True
            self.event("audio_budget_exceeded")
            return None
        try:
            if which == "ai":
                if self._ai is None:
                    self._ai = open(os.path.join(self.dir, "ai_%s.pcm" % self.conn), "ab")
                off = self.ai_bytes
                self._ai.write(data)
                self.ai_bytes += len(data)
            else:
                if self._in is None:
                    self._in = open(os.path.join(self.dir, "in_%s.pcm" % self.conn), "ab")
                off = self.in_bytes
                self._in.write(data)
                self.in_bytes += len(data)
            return off
        except Exception as e:
            logger.warning("실증 기록 음성 저장 실패(%s): %s", which, e)
            self._audio_off = True
            return None

    def on_out(self, payload: dict) -> None:
        """서버 → 단말 메시지."""
        if self.closed or not isinstance(payload, dict):
            return
        try:
            t = payload.get("type")
            if t == "audio":
                raw = base64.b64decode(payload.get("data") or "")
                off = self._write("ai", raw)
                if off is not None:
                    self.event("ai_audio", off=off, bytes=len(raw), mime=payload.get("mime_type"))
            elif t in _OUT_TYPES:
                self.event("out_" + t, **{k: v for k, v in payload.items() if k != "type"})
        except Exception as e:
            logger.debug("실증 기록 on_out 무시: %s", e)

    def on_in(self, msg: dict) -> None:
        """단말 → 서버 메시지."""
        if self.closed or not isinstance(msg, dict):
            return
        try:
            t = msg.get("type")
            if t == "audio_chunk":
                raw = base64.b64decode(msg.get("data") or "")
                off = self._write("in", raw)
                # 청크마다 사건을 남기면 너무 많다 — 끊겼다가 다시 들어오기 시작한 청크만 표시
                if off is not None and (time.time() - getattr(self, "_last_in_ts", 0.0)) > 0.6:
                    self.event("in_audio_resume", off=off)
                self._last_in_ts = time.time()
            elif t in _IN_TYPES:
                self.event("in_" + t, **{k: v for k, v in msg.items() if k != "type"})
        except Exception as e:
            logger.debug("실증 기록 on_in 무시: %s", e)

    def close(self) -> None:
        if self.closed:
            return
        self.event("ws_close", ai_bytes=self.ai_bytes, in_bytes=self.in_bytes)
        self.closed = True
        for f in (self._ai, self._in):
            try:
                if f:
                    f.close()
            except Exception:
                pass


def session_for_ws(websocket) -> Optional[TrialSession]:
    """웹소켓 연결이 기록 대상이면 기록기를 만들어 websocket.state 에 붙인다."""
    try:
        user = websocket.headers.get("x-remote-user")
        if not enabled_for(user):
            return None
        rid = websocket.query_params.get("rid")
        if not valid_rid(rid):
            rid = "ws" + uuid.uuid4().hex[:16]
        sess = TrialSession(rid, str(user).strip().lower(),
                            mode=websocket.query_params.get("mode"),
                            voice=websocket.query_params.get("voice"))
        websocket.state.trial_rec = sess
        return sess
    except Exception as e:
        logger.warning("실증 기록기 생성 실패: %s", e)
        return None


def of(websocket) -> Optional[TrialSession]:
    """websocket 에 붙은 기록기(없으면 None). 훅 지점에서 한 줄로 쓴다."""
    try:
        return getattr(websocket.state, "trial_rec", None)
    except Exception:
        return None
