# -*- coding: utf-8 -*-
"""실증 기록(#318)을 시각 순서로 이어 타임라인과 음성 파일로 만든다.

    python3 scripts/trial_timeline.py <기록 폴더> [--out <출력 폴더>] [--mix]
                                      [--tts-url http://127.0.0.1:18000] [--rate 16000]

<기록 폴더> = TRIAL_RECORD_DIR/<YYYYMMDD>/<rid> (trial_recorder.py 참조)

만드는 것
  timeline.md      — 시각(KST)·누가·무엇을. 참여자 말(서비스 인식), 상담원 답변, 길안내 문장,
                     끊김·버려진 문장·기기 음성 전환·도구 호출을 한 줄씩
  ai_<conn>.wav    — 상담원 음성(서버가 보낸 그대로)
  in_<conn>.wav    — 서비스가 받은 마이크 음성(안내 중 걸러진 구간은 빠져 있다)
  mic_<rec>.webm   — 단말이 따로 녹음한 마이크 전체(조각을 순서대로 이음)
  mix.wav (--mix)  — 스테레오. 왼쪽 = 참여자 마이크(단말 연속 녹음), 오른쪽 = 서비스 음성
                     (상담원 음성 + 길안내 음성). 길안내 음성은 --tts-url 의 합성 API 캐시에서 받는다.
                     단말 녹음 해독에 ffmpeg 가 필요하다(없으면 왼쪽은 비운다).

시각 맞추기
  단말 사건에는 단말 시각(tc, ms)과 서버 도착 시각(ts)이 함께 있다. 단말→서버 지연의 하한을 쓰기 위해
  (ts*1000 - tc) 의 최솟값을 단말 시계 보정값으로 쓴다(수백 ms 오차 가능 — 영상과는 박수·화면 시계로 맞춘다).
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave
from collections import defaultdict

KST = 9 * 3600


def load_events(d: str) -> list:
    evs = []
    p = os.path.join(d, "events.jsonl")
    if not os.path.isfile(p):
        return evs
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evs.append(json.loads(line))
            except ValueError:
                continue
    return evs


def clock_offset_ms(evs: list) -> float:
    """단말 시각 + 보정값 = 서버 시각(ms)."""
    cands = [e["ts"] * 1000.0 - e["tc"] for e in evs
             if e.get("src") == "client" and isinstance(e.get("tc"), (int, float)) and isinstance(e.get("ts"), (int, float))]
    return min(cands) if cands else 0.0


def event_time(e: dict, off_ms: float) -> float:
    """사건이 실제로 일어난 서버 기준 시각(초). 단말 사건은 단말 시각을 보정해 쓴다."""
    if e.get("src") == "client" and isinstance(e.get("tc"), (int, float)):
        return (e["tc"] + off_ms) / 1000.0
    return float(e.get("ts") or 0.0)


def fmt(t: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(t + KST)) + ".%01d" % int((t % 1) * 10)


def build_timeline(evs: list, off_ms: float) -> list:
    """(시각, 누가, 내용) 목록. 조각으로 오는 전사는 이어 붙인다."""
    rows = []
    buf = {"who": None, "t": None, "text": ""}

    def flush():
        if buf["who"] and buf["text"].strip():
            rows.append((buf["t"], buf["who"], buf["text"].strip()))
        buf.update(who=None, t=None, text="")

    for e in sorted(evs, key=lambda x: event_time(x, off_ms)):
        t = event_time(e, off_ms)
        typ = e.get("type", "")
        if typ in ("out_user_transcript", "out_ai_transcript", "out_text"):
            who = "참여자(서비스 인식)" if typ == "out_user_transcript" else "상담원"
            if buf["who"] != who:
                flush()
                buf.update(who=who, t=t)
            buf["text"] += str(e.get("content") or "")
            continue
        flush()
        if typ == "in_text":
            rows.append((t, "참여자(글자 입력)", str(e.get("content") or "")))
        elif typ == "navi_play":
            src = "기기 음성" if e.get("via") == "device" else "서버 음성"
            rows.append((t, "길안내", "%s  [%s]" % (e.get("text"), src)))
        elif typ == "navi_cut":
            rows.append((t, "·", "길안내 끊김: %s" % e.get("text")))
        elif typ == "navi_barge":
            rows.append((t, "·", "참여자가 말해 길안내 멈춤"))
        elif typ == "navi_drop":
            rows.append((t, "·", "말하지 못하고 버린 안내: %s" % e.get("text")))
        elif typ == "navi_muted":
            rows.append((t, "·", "소리 꺼짐 상태라 말하지 않은 안내: %s" % e.get("text")))
        elif typ == "out_interrupted":
            rows.append((t, "·", "상담원 답변 끊김(참여자 발화 감지)"))
        elif typ == "consult_device_tts":
            rows.append((t, "상담원(기기 음성)", str(e.get("text") or "")))
        elif typ == "out_tool_call":
            rows.append((t, "·", "도구 호출: %s" % e.get("name")))
        elif typ == "out_answer_card":
            rows.append((t, "·", "정책 카드 표시"))
        elif typ == "echo_dropped":
            rows.append((t, "·", "에코로 보고 버린 인식: %s" % e.get("content")))
        elif typ in ("gemini_reconnect", "local_fallback", "ws_open", "ws_close", "mic_rec_start",
                     "mic_rec_stop", "mic_rec_error", "mic_part_dropped", "audio_budget_exceeded",
                     "out_error", "out_idle_warning", "out_auto_close"):
            rows.append((t, "·", typ + (" " + json.dumps({k: v for k, v in e.items()
                                                          if k not in ("ts", "tc", "src", "type", "user")},
                                                         ensure_ascii=False) if typ.startswith(("out_", "mic_")) else "")))
    flush()
    return rows


def write_timeline(rows: list, path: str, title: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 실증 기록 타임라인 — %s\n\n" % title)
        f.write("| 시각(KST) | 누가 | 내용 |\n|---|---|---|\n")
        for t, who, text in rows:
            f.write("| %s | %s | %s |\n" % (fmt(t), who, str(text).replace("|", "／").replace("\n", " ")))


def pcm_to_wav(src: str, dst: str, rate: int) -> None:
    with open(src, "rb") as f:
        data = f.read()
    with wave.open(dst, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)


def concat_mic(d: str, out: str) -> dict:
    """mic_<rec>_<seq>.part → mic_<rec>.webm. {rec: 경로}"""
    parts = defaultdict(list)
    for p in glob.glob(os.path.join(d, "mic_*_*.part")):
        m = re.match(r"mic_(.+)_(\d{6})\.part$", os.path.basename(p))
        if m:
            parts[m.group(1)].append((int(m.group(2)), p))
    res = {}
    for rec, lst in parts.items():
        lst.sort()
        dst = os.path.join(out, "mic_%s.webm" % rec)
        with open(dst, "wb") as w:
            for _, p in lst:
                with open(p, "rb") as f:
                    w.write(f.read())
        missing = [i for i in range(lst[-1][0] + 1) if i not in {s for s, _ in lst}]
        res[rec] = {"path": dst, "parts": len(lst), "missing": missing}
    return res


# ── 믹스 ──
def _np():
    import numpy as np  # 믹스에만 필요
    return np


def _resample(x, sr_from: int, sr_to: int):
    np = _np()
    if sr_from == sr_to or len(x) == 0:
        return x
    n = int(round(len(x) * sr_to / sr_from))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def _pcm16(data: bytes):
    np = _np()
    return np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


def _wav_bytes_to_f32(b: bytes):
    with wave.open(io.BytesIO(b), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        x = _pcm16(w.readframes(w.getnframes()))
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def _decode_ffmpeg(path: str, rate: int):
    if not shutil.which("ffmpeg"):
        return None
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-af", "aresample=async=1", "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
                             capture_output=True, timeout=600, check=True).stdout
        return _pcm16(out)
    except Exception as e:
        print("  ffmpeg 해독 실패(%s): %s" % (os.path.basename(path), e))
        return None


def _fetch_tts(base: str, voice: str, text: str, cache: dict):
    key = (voice, text)
    if key in cache:
        return cache[key]
    url = base.rstrip("/") + "/api/v1/tts?voice=" + urllib.parse.quote(voice) + "&text=" + urllib.parse.quote(text)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            cache[key] = _wav_bytes_to_f32(r.read())
    except Exception as e:
        print("  길안내 음성 받기 실패(%s…): %s" % (text[:20], e))
        cache[key] = None
    return cache[key]


def build_mix(d: str, out: str, evs: list, off_ms: float, rate: int, tts_url: str, mics: dict) -> None:
    np = _np()
    times = [event_time(e, off_ms) for e in evs] or [time.time()]
    t_start = min(times)
    t_end = max(times) + 30
    n = int((t_end - t_start) * rate) + 1
    left = np.zeros(n, dtype=np.float32)
    right = np.zeros(n, dtype=np.float32)

    def place(ch, x, t):
        i = int((t - t_start) * rate)
        if i >= n or len(x) == 0:
            return
        if i < 0:
            x = x[-i:]
            i = 0
        m = min(len(x), n - i)
        ch[i:i + m] += x[:m]

    # 왼쪽 — 단말 연속 녹음. 녹음 시작(t0, 단말 ms)에 놓는다
    starts = {e.get("rec"): e.get("t0") for e in evs if e.get("type") == "mic_rec_start"}
    for rec, info in mics.items():
        t0 = starts.get(rec)
        if t0 is None:
            continue
        x = _decode_ffmpeg(info["path"], rate)
        if x is not None:
            place(left, x, (t0 + off_ms) / 1000.0)

    # 오른쪽 — 상담원 음성. 연결(conn)마다 ai_play 기준점부터 다음 기준점·중단까지 이어 놓는다
    conns = [e.get("conn") for e in evs if e.get("type") == "ws_open"]
    for conn in conns:
        p = os.path.join(d, "ai_%s.pcm" % conn)
        if not os.path.isfile(p):
            continue
        with open(p, "rb") as f:
            data = f.read()
        o_ev = next((e for e in evs if e.get("type") == "ws_open" and e.get("conn") == conn), None)
        c_ev = next((e for e in evs if e.get("type") == "ws_close" and e.get("conn") == conn), None)
        lo = o_ev["ts"] if o_ev else 0
        hi = c_ev["ts"] if c_ev else float("inf")
        # 단말 기준점(ai_play: 재생을 새로 시작한 위치, ai_stop: 멈춘 위치)은 연결 구간 안의 것만
        marks = sorted([e for e in evs if e.get("type") in ("ai_play", "ai_stop") and lo <= e.get("ts", 0) <= hi + 10],
                       key=lambda e: event_time(e, off_ms))
        ai_rate = 24000
        mime = next((e.get("mime") for e in evs if e.get("type") == "ai_audio" and e.get("conn") == conn and e.get("mime")), "")
        m = re.search(r"rate=(\d+)", mime or "")
        if m:
            ai_rate = int(m.group(1))
        for i, mk in enumerate(marks):
            if mk.get("type") != "ai_play":
                continue
            start = int(mk.get("off") or 0)
            end = len(data)
            for nx in marks[i + 1:]:
                end = min(end, int(nx.get("off") or end))
                break
            seg = data[start:end]
            if seg:
                place(right, _resample(_pcm16(seg), ai_rate, rate), event_time(mk, off_ms))

    # 오른쪽 — 길안내 음성. 재생 시작부터, 끊겼으면 끊긴 시각까지
    cache = {}
    plays = sorted([e for e in evs if e.get("type") in ("navi_play", "navi_cut", "navi_end")],
                   key=lambda e: event_time(e, off_ms))
    for i, e in enumerate(plays):
        if e.get("type") != "navi_play" or e.get("via") == "device" or not tts_url:
            continue
        got = _fetch_tts(tts_url, e.get("voice") or "female", e.get("text") or "", cache)
        if not got:
            continue
        x, sr = got
        x = _resample(x, sr, rate)
        t0 = event_time(e, off_ms)
        nxt = next((p for p in plays[i + 1:] if p.get("type") in ("navi_cut", "navi_play")), None)
        if nxt is not None and nxt.get("type") == "navi_cut":
            x = x[: max(0, int((event_time(nxt, off_ms) - t0) * rate))]
        place(right, x * 0.9, t0)

    stereo = np.stack([np.clip(left, -1, 1), np.clip(right, -1, 1)], axis=1)
    with wave.open(os.path.join(out, "mix.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((stereo * 32767).astype("<i2").tobytes())
    print("  mix.wav 시작 시각(KST) %s — 영상과 맞출 때 이 시각이 0초" % fmt(t_start))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out")
    ap.add_argument("--mix", action="store_true")
    ap.add_argument("--tts-url", default=os.environ.get("TRIAL_TTS_URL", "http://127.0.0.1:18000"))
    ap.add_argument("--rate", type=int, default=16000)
    a = ap.parse_args(argv)
    d = a.dir.rstrip("/")
    out = a.out or os.path.join(d, "_out")
    os.makedirs(out, exist_ok=True)
    evs = load_events(d)
    if not evs:
        print("사건 기록이 없습니다: %s" % d)
        return 1
    off = clock_offset_ms(evs)
    rows = build_timeline(evs, off)
    write_timeline(rows, os.path.join(out, "timeline.md"), os.path.basename(d))
    for p in glob.glob(os.path.join(d, "ai_*.pcm")):
        conn = os.path.basename(p)[3:-4]
        mime = next((e.get("mime") for e in evs if e.get("type") == "ai_audio" and e.get("conn") == conn and e.get("mime")), "")
        m = re.search(r"rate=(\d+)", mime or "")
        pcm_to_wav(p, os.path.join(out, os.path.basename(p)[:-4] + ".wav"), int(m.group(1)) if m else 24000)
    for p in glob.glob(os.path.join(d, "in_*.pcm")):
        pcm_to_wav(p, os.path.join(out, os.path.basename(p)[:-4] + ".wav"), 16000)
    mics = concat_mic(d, out)
    print("사건 %d건 · 타임라인 %d줄 · 단말 시계 보정 %+.0fms" % (len(evs), len(rows), off))
    for rec, info in mics.items():
        print("  단말 녹음 %s: 조각 %d개%s" % (rec, info["parts"],
                                        (" · 빠진 조각 %s" % info["missing"]) if info["missing"] else ""))
    if a.mix:
        build_mix(d, out, evs, off, a.rate, a.tts_url, mics)
    print("출력: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
