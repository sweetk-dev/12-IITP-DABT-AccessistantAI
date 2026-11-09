# -*- coding: utf-8 -*-
"""용도별 모델 설정·차선 전환·호출 한도·교차검증 테스트 (외부 호출 없음)."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_APP))
sys.path.insert(0, str(_APP / "policy_db"))

import model_router as mr  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.setenv("EXAONE_API_KEY", "test-exaone")
    monkeypatch.setenv("LLM_FALLBACK", "gemma")
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    return tmp_path


# ── 설정 ─────────────────────────────────────────────────────
def test_no_file_means_legacy(data_dir):
    assert mr.explicit_route("policy_update") is None
    assert mr.effective_route("policy_update")["primary"] == "gemini"
    assert mr.effective_route("discovery_classify")["fallback"] == ["gemma"]


def test_save_and_load(data_dir):
    mr.save_routing({"policy_update": {"primary": "gemini", "fallback": ["exaone", "gemma"], "verifier": "exaone"}})
    r = mr.explicit_route("policy_update")
    assert r == {"primary": "gemini", "fallback": ["exaone", "gemma"], "verifier": "exaone"}
    # null 은 종전 동작으로 되돌림
    mr.save_routing({"policy_update": None})
    assert mr.explicit_route("policy_update") is None


@pytest.mark.parametrize("route,msg", [
    ({"primary": "nope"}, "등록되지 않았습니다"),
    ({"primary": "gemini", "fallback": ["gemini"]}, "두 번"),
    ({"primary": "gemini", "verifier": "gemini"}, "달라야"),
    ({"primary": "gemini", "fallback": ["exaone", "gemma", "exaone"]}, "두 번"),
])
def test_validate_rejects(data_dir, route, msg):
    with pytest.raises(mr.RouterError) as e:
        mr.validate_route("policy_update", route)
    assert msg in str(e.value)


def test_verifier_only_for_supported_role(data_dir):
    with pytest.raises(mr.RouterError):
        mr.validate_route("weekly_report", {"primary": "gemini", "verifier": "exaone"})


def test_describe_shape(data_dir):
    d = mr.describe()
    ids = {m["id"] for m in d["models"]}
    assert {"gemini", "exaone", "gemma"} <= ids
    ex = next(m for m in d["models"] if m["id"] == "exaone")
    assert ex["configured"] is True and ex["model_name"] == "k-exaone_v2.5"
    assert {r["id"] for r in d["roles"]} == set(mr.ROLES)
    # 키 값은 화면 응답에 나오지 않는다
    assert "test-exaone" not in json.dumps(d, ensure_ascii=False)


# ── 차선 전환 ─────────────────────────────────────────────────
def test_generate_falls_back(data_dir, monkeypatch):
    calls = []

    def fail(role, **kw):
        calls.append("gemini"); raise mr.RouterError("Gemini HTTP 429")

    def ok(role, **kw):
        calls.append("exaone"); return '{"patches": []}'

    monkeypatch.setitem(mr._CALLERS, "gemini", fail)
    monkeypatch.setitem(mr._CALLERS, "exaone", ok)
    res = mr.generate("policy_update", prompt="x", route={"primary": "gemini", "fallback": ["exaone"], "verifier": None})
    assert res["model"] == "exaone" and calls == ["gemini", "exaone"]
    assert res["attempts"][0]["ok"] is False and res["attempts"][1]["ok"] is True


def test_generate_skips_unconfigured(data_dir, monkeypatch):
    monkeypatch.delenv("EXAONE_API_KEY")
    monkeypatch.setitem(mr._CALLERS, "gemini", lambda role, **kw: (_ for _ in ()).throw(mr.RouterError("down")))
    with pytest.raises(mr.RouterError) as e:
        mr.generate("policy_update", prompt="x", route={"primary": "gemini", "fallback": ["exaone"], "verifier": None})
    assert "설정 없음" in str(e.value)


# ── 엑사원 호출 한도 ──────────────────────────────────────────
class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_exaone_day_limit_exhausted(data_dir, monkeypatch):
    monkeypatch.setitem(mr.MODELS["exaone"], "min_interval_sec", 0)
    n = {"c": 0}

    def post(url, **kw):
        n["c"] += 1
        return _Resp(429, {"result_code": 429}, {"x-day-rate-limit": "100", "x-day-rate-limit-remaining": "0"})

    monkeypatch.setattr(mr.httpx, "post", post)
    with pytest.raises(mr.RouterError) as e:
        mr.call_model("exaone", prompt="x")
    assert "소진" in str(e.value) and n["c"] == 1
    assert mr.usage_today()["exaone"]["exhausted"] is True
    # 같은 날 다시 부르면 요청을 보내지 않고 바로 실패
    with pytest.raises(mr.RouterError):
        mr.call_model("exaone", prompt="x")
    assert n["c"] == 1


def test_exaone_ok_records_usage(data_dir, monkeypatch):
    monkeypatch.setitem(mr.MODELS["exaone"], "min_interval_sec", 0)
    seen = {}

    def post(url, json=None, **kw):
        seen.update(json)
        return _Resp(200, {"choices": [{"message": {"content": '\n\n{"patches": []}', "reasoning_content": "..."}}]},
                     {"x-day-rate-limit": "100", "x-day-rate-limit-remaining": "97"})

    monkeypatch.setattr(mr.httpx, "post", post)
    txt = mr.call_model("exaone", prompt="x", json_mode="object", max_tokens=8000)
    assert txt == '{"patches": []}'
    assert seen["max_tokens"] >= 32000 and seen["reasoning_effort"] == "low"
    assert seen["response_format"] == {"type": "json_object"}
    u = mr.usage_today()["exaone"]
    assert u["calls"] == 1 and u["ok"] == 1 and u["day_remaining"] == 97


def test_exaone_array_mode_has_no_object_format(data_dir, monkeypatch):
    monkeypatch.setitem(mr.MODELS["exaone"], "min_interval_sec", 0)
    seen = {}

    def post(url, json=None, **kw):
        seen.update(json)
        return _Resp(200, {"choices": [{"message": {"content": "[]"}}]}, {})

    monkeypatch.setattr(mr.httpx, "post", post)
    mr.call_model("exaone", prompt="x", json_mode="array")
    assert "response_format" not in seen


# ── 교차검증 비교 ─────────────────────────────────────────────
def test_facts_ignore_wording():
    assert mr._facts("중증 50% / 경증 30%") == mr._facts("경증은 30%, 중증은 50% 감면")
    assert "2026-12-01" in mr._facts("2026년 12월 1일부터")
    assert mr._facts("2026.12.01 시행") == mr._facts("2026년 12월 1일 시행")


def test_compare_agree_and_disagree():
    a = [{"op": "update", "path": "supported_amount.rate", "new": "중증 50% / 경증 30% (2026년 12월 1일부터)"}]
    same = [{"op": "update", "path": "supported_amount.rate", "new": "2026년 12월 1일부터 경증 30%, 중증 50%"}]
    assert mr.compare_patches(a, same)["status"] == "agree"
    dropped_date = [{"op": "update", "path": "supported_amount.rate", "new": "중증 50% / 경증 30%"}]
    c = mr.compare_patches(a, dropped_date)
    assert c["status"] == "disagree" and c["fact_diff"][0]["only_primary"] == ["2026-12-01"]
    extra = same + [{"op": "delete", "path": "faq.2"}]
    c = mr.compare_patches(a, extra)
    assert c["status"] == "disagree" and c["only_verifier"] == ["delete faq.2"]
    assert mr.compare_patches([], [])["status"] == "agree"


# ── 정책 갱신 흐름 (llm_updater) ──────────────────────────────
def _item(tmp_path):
    items = tmp_path / "items"
    items.mkdir()
    p = items / "B999_test.json"
    p.write_text(json.dumps({"id": "B999", "title": "시험 항목", "version": "1.0",
                             "supported_amount": {"rate": "중증 50% / 경증 30%"}}, ensure_ascii=False), encoding="utf-8")
    return p


def _run_update(tmp_path, monkeypatch, primary_text, verifier_text=None, verifier_exc=None, used="gemini"):
    from crawler import llm_updater as lu
    monkeypatch.setattr(lu, "_xcheck_count", 0)

    async def fake_agen(role, **kw):
        return {"text": primary_text, "model": used, "attempts": []}

    async def fake_acall(mid, **kw):
        if verifier_exc:
            raise verifier_exc
        return verifier_text

    monkeypatch.setattr(lu._mr, "agenerate", fake_agen)
    monkeypatch.setattr(lu._mr, "acall_model", fake_acall)
    staging = tmp_path / "staging"
    staged, _ = asyncio.run(lu.update_item_via_llm(item_path=_item(tmp_path), related_changes=[],
                                                    staging_dir=staging, schema_path=tmp_path / "none.json"))
    return staged, staging


P_CHANGE = json.dumps({"patches": [{"op": "update", "path": "supported_amount.rate",
                                    "old": "중증 50% / 경증 30%", "new": "중증 50% / 경증 30% (2026년 12월 1일부터 주말 포함)",
                                    "evidence": "…", "confidence": "high"}]}, ensure_ascii=False)


def test_updater_writes_xcheck_disagree(data_dir, monkeypatch):
    mr.save_routing({"policy_update": {"primary": "gemini", "fallback": ["exaone"], "verifier": "exaone"}})
    v = json.dumps({"patches": [{"op": "update", "path": "supported_amount.rate", "new": "중증 50% / 경증 30%"}]})
    staged, staging = _run_update(data_dir, monkeypatch, P_CHANGE, verifier_text=v)
    x = json.loads((staging / staged.name.replace(".staged.json", ".xcheck.json")).read_text(encoding="utf-8"))
    assert x["status"] == "disagree" and x["primary"] == "gemini" and x["verifier"] == "exaone"
    assert x["fact_diff"][0]["only_primary"] == ["2026-12-01"]


def test_updater_xcheck_failure_does_not_block(data_dir, monkeypatch):
    mr.save_routing({"policy_update": {"primary": "gemini", "fallback": [], "verifier": "exaone"}})
    staged, staging = _run_update(data_dir, monkeypatch, P_CHANGE, verifier_exc=mr.RouterError("K-EXAONE HTTP 503"))
    assert staged.exists()
    x = json.loads((staging / staged.name.replace(".staged.json", ".xcheck.json")).read_text(encoding="utf-8"))
    assert x["status"] == "verifier_failed"


def test_updater_same_model_skips(data_dir, monkeypatch):
    mr.save_routing({"policy_update": {"primary": "gemini", "fallback": ["exaone"], "verifier": "exaone"}})
    staged, staging = _run_update(data_dir, monkeypatch, P_CHANGE, verifier_text="{}", used="exaone")
    x = json.loads((staging / staged.name.replace(".staged.json", ".xcheck.json")).read_text(encoding="utf-8"))
    assert x["status"] == "skipped"


def test_updater_no_change_skips_verifier(data_dir, monkeypatch):
    mr.save_routing({"policy_update": {"primary": "gemini", "fallback": [], "verifier": "exaone"}})
    staged, staging = _run_update(data_dir, monkeypatch, '{"patches": []}',
                                  verifier_exc=AssertionError("검증 모델을 부르면 안 됨"))
    assert staged is None


def test_updater_legacy_path_without_settings(data_dir, monkeypatch):
    """설정 파일이 없으면 종전 get_backend() 경로 — 설정 경로 함수는 불리지 않는다."""
    from crawler import llm_updater as lu

    class FakeBackend:
        name, model = "gemini", "fake"

        async def generate_json_update(self, **kw):
            return P_CHANGE

    async def boom(*a, **kw):
        raise AssertionError("설정 경로 호출됨")

    monkeypatch.setattr(lu._mr, "agenerate", boom)
    monkeypatch.setattr(lu, "get_backend", lambda: FakeBackend())
    staged, _ = asyncio.run(lu.update_item_via_llm(item_path=_item(data_dir), related_changes=[],
                                                    staging_dir=data_dir / "staging", schema_path=data_dir / "none.json"))
    assert staged.exists()
    assert not list((data_dir / "staging").glob("*.xcheck.json"))


def test_review_core_reads_xcheck(data_dir, monkeypatch):
    mr.save_routing({"policy_update": {"primary": "gemini", "fallback": [], "verifier": "exaone"}})
    v = json.dumps({"patches": [{"op": "update", "path": "supported_amount.rate", "new": "중증 50%"}]})
    staged, staging = _run_update(data_dir, monkeypatch, P_CHANGE, verifier_text=v)
    from crawler import review_core as rc
    monkeypatch.setattr(rc.ca, "STAGING_DIR", staging)
    monkeypatch.setattr(rc.ca, "ITEMS_DIR", data_dir / "items")
    info = rc.get_review("B999", _staged=staged)
    assert info["xcheck"]["status"] == "disagree"
    lst = rc.list_pending()
    assert lst and lst[0]["xcheck_status"] == "disagree"
    # 반려하면 사이드카도 함께 옮긴다
    r = rc.reject("B999")
    assert r["ok"] and not list(staging.glob("*.xcheck.json"))
    assert list((staging / ".rejected").glob("*.xcheck.json"))


# ── 교차검토 지적 반영분 ──────────────────────────────────────
def test_facts_severity_swap_and_qualifiers():
    assert mr._facts("중증 50% / 경증 30%") != mr._facts("중증 30% / 경증 50%")
    assert mr._facts("만 65세 이상") != mr._facts("만 65세 미만")
    assert mr._facts("월 50,000원") == mr._facts("월 5만원")
    assert mr._facts("18~65세") == mr._facts("18세부터 65세까지")
    assert mr._facts("2026년 7월 시행") == mr._facts("2026.07 시행")
    long = "장애의 정도가 심한 장애(중증) : 50% 감면(보호자 1인 포함). 장애의 정도가 심하지 않은 장애(경증) : 30% 감면"
    assert mr._facts(long) == mr._facts("중증 50%(보호자 1명 포함), 경증 30%")


def test_compare_flattens_nested_and_ignores_bad_items():
    a = [{"op": "update", "path": "supported_amount", "new": {"rate": "중증 50%"}}]
    b = [{"op": "update", "path": "supported_amount.rate", "new": "중증 50%"}, None, {"path": ["x"]}, {"op": ["x"], "path": "y"}]
    assert mr.compare_patches(a, b)["status"] == "agree"


def test_validate_rejects_bad_types_and_unconfigured(data_dir, monkeypatch):
    for bad in ("gemini", ["gemini"], {"primary": ["gemini"]}, {"primary": "gemini", "fallback": "exaone"}):
        with pytest.raises(mr.RouterError):
            mr.validate_route("policy_update", bad)
    monkeypatch.delenv("EXAONE_API_KEY")
    with pytest.raises(mr.RouterError) as e:
        mr.validate_route("policy_update", {"primary": "gemini", "verifier": "exaone"})
    assert "설정되지 않아" in str(e.value)
    with pytest.raises(mr.RouterError):
        mr.save_routing({"bogus": None})


def test_after_call_across_midnight_keeps_new_day(data_dir, monkeypatch):
    today = {"v": "2026-11-09"}
    monkeypatch.setattr(mr, "_today", lambda: today["v"])
    monkeypatch.setitem(mr.MODELS["exaone"], "min_interval_sec", 0)
    day = mr._before_call("exaone")                       # 11-09 에 시작
    today["v"] = "2026-11-10"
    mr._after_call("exaone", True, headers={"x-day-rate-limit-remaining": "0"}, day=day)   # 11-10 에 끝남
    d = mr._read_usage()["exaone"]
    assert d.get("date") == "2026-11-09" and not d.get("exhausted")


def test_exaone_non_json_body_is_recorded(data_dir, monkeypatch):
    monkeypatch.setitem(mr.MODELS["exaone"], "min_interval_sec", 0)

    class Bad(_Resp):
        def json(self):
            raise ValueError("html")

    monkeypatch.setattr(mr.httpx, "post", lambda url, **kw: Bad(200, headers={}))
    with pytest.raises(mr.RouterError):
        mr.call_model("exaone", prompt="x")
    assert mr.usage_today()["exaone"]["fail"] == 1


def test_exaone_auth_error_hides_body(data_dir, monkeypatch):
    monkeypatch.setitem(mr.MODELS["exaone"], "min_interval_sec", 0)
    monkeypatch.setattr(mr.httpx, "post", lambda url, **kw: _Resp(401, {"echo": "Bearer test-exaone"}, {}))
    with pytest.raises(mr.RouterError) as e:
        mr.call_model("exaone", prompt="x")
    assert "test-exaone" not in str(e.value)


def test_env_int_empty_value(monkeypatch):
    from crawler import llm_updater as lu
    monkeypatch.setenv("XCHECK_MAX_PER_RUN", "")
    assert lu._env_int("XCHECK_MAX_PER_RUN", 15) == 15
    monkeypatch.setenv("XCHECK_MAX_PER_RUN", "abc")
    assert lu._env_int("XCHECK_MAX_PER_RUN", 15) == 15


def test_updater_compare_error_does_not_block(data_dir, monkeypatch):
    mr.save_routing({"policy_update": {"primary": "gemini", "fallback": [], "verifier": "exaone"}})
    from crawler import llm_updater as lu

    def boom(a, b):
        raise TypeError("x")
    monkeypatch.setattr(lu._mr, "compare_patches", boom)
    staged, staging = _run_update(data_dir, monkeypatch, P_CHANGE, verifier_text=P_CHANGE)
    x = json.loads((staging / staged.name.replace(".staged.json", ".xcheck.json")).read_text(encoding="utf-8"))
    assert staged.exists() and x["status"] == "verifier_failed"


def test_updater_schema_failure_skips_verifier(data_dir, monkeypatch):
    mr.save_routing({"policy_update": {"primary": "gemini", "fallback": [], "verifier": "exaone"}})
    schema = data_dir / "schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"supported_amount": {"type": "string"}}}), encoding="utf-8")
    from crawler import llm_updater as lu
    monkeypatch.setattr(lu, "_xcheck_count", 0)

    async def fake_agen(role, **kw):
        return {"text": P_CHANGE, "model": "gemini", "attempts": []}

    async def must_not(*a, **kw):
        raise AssertionError("스키마 실패인데 검증 모델 호출")
    monkeypatch.setattr(lu._mr, "agenerate", fake_agen)
    monkeypatch.setattr(lu._mr, "acall_model", must_not)
    with pytest.raises(RuntimeError) as e:
        asyncio.run(lu.update_item_via_llm(item_path=_item(data_dir), related_changes=[],
                                           staging_dir=data_dir / "staging", schema_path=schema))
    assert "schema" in str(e.value)
