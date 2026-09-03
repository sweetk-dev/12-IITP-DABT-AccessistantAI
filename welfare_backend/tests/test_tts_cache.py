"""v1.43.2 — 길안내 음성 합성 디스크 캐시 · 일일 한도 판정.

main.py 는 import 시 앱·DB 엔진을 만들므로, 캐시 헬퍼 함수 소스만 떼어 내 검증한다.
"""
import ast
import os
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SRC = (BASE / "main.py").read_text(encoding="utf-8")


def _load_helpers(tmp_dir):
    tree = ast.parse(SRC)
    wanted = {"_tts_disk_path", "_tts_disk_get", "_tts_disk_put", "_tts_quota_delay_sec"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in nodes} == wanted
    mod = ast.Module(body=nodes, type_ignores=[])
    import logging
    ns = {"os": os, "_re": re, "_hashlib": __import__("hashlib"), "logging": logging,
          "_TTS_CACHE_DIR": str(tmp_dir)}
    exec(compile(mod, "main.py", "exec"), ns)
    return ns


def test_disk_cache_roundtrip(tmp_path):
    ns = _load_helpers(tmp_path)
    assert ns["_tts_disk_get"]("Zephyr", "횡단보도를 건너 12m 이동합니다.") is None
    ns["_tts_disk_put"]("Zephyr", "횡단보도를 건너 12m 이동합니다.", b"RIFF-wav")
    assert ns["_tts_disk_get"]("Zephyr", "횡단보도를 건너 12m 이동합니다.") == b"RIFF-wav"
    # 보이스·문장이 다르면 다른 항목
    assert ns["_tts_disk_get"]("Kore", "횡단보도를 건너 12m 이동합니다.") is None
    assert ns["_tts_disk_get"]("Zephyr", "횡단보도를 건너 13m 이동합니다.") is None
    p = Path(ns["_tts_disk_path"]("Zephyr", "횡단보도를 건너 12m 이동합니다."))
    assert p.suffix == ".wav" and p.parent.parent.name == "Zephyr"
    assert not p.with_suffix(".wav.tmp").exists()


def test_quota_delay_parsing():
    ns = _load_helpers("/nonexistent")
    f = ns["_tts_quota_delay_sec"]
    daily = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Quota exceeded for metric: "
             "generativelanguage.googleapis.com/generate_requests_per_model_per_day, limit: 100', "
             "'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '77511s'}]}}")
    assert f(daily) == 77511
    assert f("429 RESOURCE_EXHAUSTED per_day without retry info") == 3600
    assert f("429 RESOURCE_EXHAUSTED generate_requests_per_model_per_minute limit: 10") == 0
    assert f("503 Service Unavailable") is None
    assert f("timeout") is None


def test_endpoint_uses_disk_cache_and_cooldown():
    # 소스 수준 가드 — 엔드포인트가 디스크 캐시·쿨다운·세마포어를 실제로 거치는지
    assert "disk = _tts_disk_get(vname, text)" in SRC
    assert '"X-TTS-Cache": "disk"' in SRC
    assert "_time.time() < _TTS_QUOTA_BLOCK_UNTIL" in SRC
    assert "async with _TTS_SEM:" in SRC
    assert "_tts_disk_put(vname, text, wav)" in SRC


# ── v1.45.0 Cloud TTS(Neural2) provider ──
def _load_gcloud_helpers():
    tree = ast.parse(SRC)
    wanted = {"_tts_provider_order", "_gcloud_voice_for", "_gcloud_tts_payload"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in nodes} == wanted
    ns = {"GCLOUD_TTS_VOICE_FEMALE": "ko-KR-Neural2-A", "GCLOUD_TTS_VOICE_MALE": "ko-KR-Neural2-C",
          "GCLOUD_TTS_RATE": 24000}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)
    return ns


def test_provider_order():
    f = _load_gcloud_helpers()["_tts_provider_order"]
    assert f("gcloud", "key", True) == ["gcloud", "gemini"]
    assert f("gcloud", "key", False) == ["gcloud"]
    assert f("gcloud", "", True) == ["gemini"]          # 키 없으면 종전 그대로
    assert f("gemini", "key", True) == ["gemini", "gcloud"]
    assert f("gemini", "", False) == []


def test_gcloud_voice_mapping_and_payload():
    ns = _load_gcloud_helpers()
    v = ns["_gcloud_voice_for"]
    assert v("female", "Zephyr", "Algieba") == "ko-KR-Neural2-A"
    assert v("male", "Algieba", "Algieba") == "ko-KR-Neural2-C"
    assert v("Algieba", "Algieba", "Algieba") == "ko-KR-Neural2-C"   # 남성 기본 보이스명으로 요청해도 남성
    assert v("Zephyr", "Zephyr", "Algieba") == "ko-KR-Neural2-A"
    assert v("ko-KR-Neural2-B", "Zephyr", "Algieba") == "ko-KR-Neural2-B"
    assert v("", "Zephyr", "Algieba") == "ko-KR-Neural2-A"
    body = ns["_gcloud_tts_payload"]("횡단보도를 건너세요.", "ko-KR-Neural2-A")
    assert body == {"input": {"text": "횡단보도를 건너세요."},
                    "voice": {"languageCode": "ko-KR", "name": "ko-KR-Neural2-A"},
                    "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000}}


def test_endpoint_gcloud_first_then_gemini():
    assert 'if order[0] == "gcloud":' in SRC
    assert "wav = await _gcloud_synthesize(text, gc_vname[3:])" in SRC
    assert '"X-TTS-Provider": "gcloud"' in SRC
    assert 'wav[:4] != b"RIFF"' in SRC                    # LINEAR16 = WAV 헤더 포함
    assert "Cloud TTS 합성 실패" in SRC                    # 실패 시 gemini 로
    # 캐시 디렉터리는 provider 별 보이스명(gc-…)으로 분리된다
    assert 'gc_vname = "gc-" + _gcloud_voice_for(' in SRC
