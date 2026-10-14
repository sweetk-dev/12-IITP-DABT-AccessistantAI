# -*- coding: utf-8 -*-
"""운영 콘솔 데이터 수집 상태·알림 설정 (collect_ops, v2.2.0).

1. 단위: 수신 주소 검증(validate_emails).
2. DB 통합: ITEST_IITP_DB_URL(postgresql+asyncpg://…, 01 v1.9.0 적용 DB)이 있을 때만.
   운영 행은 건드리지 않는다 — 전용 ext_sys('ITEST_CO')의 실행 기록만 만들고 지우며,
   sys_ops_setting 은 테스트 전 값을 되돌린다.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect_ops  # noqa: E402


def test_validate_emails_ok_dedup_and_list_input():
    assert collect_ops.validate_emails("a@x.co.kr, A@x.co.kr; b@x.co.kr") == ["a@x.co.kr", "b@x.co.kr"]
    assert collect_ops.validate_emails(["a@x.co.kr", " "]) == ["a@x.co.kr"]
    assert collect_ops.validate_emails("") == []


def test_validate_emails_rejects_bad_and_too_many():
    with pytest.raises(collect_ops.AlertConfigError, match="형식"):
        collect_ops.validate_emails("a@x.co.kr, nope")
    with pytest.raises(collect_ops.AlertConfigError, match="최대 5개"):
        collect_ops.validate_emails(",".join(f"u{i}@x.co.kr" for i in range(6)))


def test_jobs_have_group_and_breakdown_spec():
    """작업 정의는 (이름, 주기, 분류) 3칸이고 분류는 JOB_GROUPS 안에 있다. 같은 분류는 이웃한다."""
    groups = []
    for ext_sys, spec in collect_ops.JOBS.items():
        assert len(spec) == 3, ext_sys
        assert spec[2] in collect_ops.JOB_GROUPS, ext_sys
        if not groups or groups[-1] != spec[2]:
            groups.append(spec[2])
    assert len(groups) == len(set(groups))            # 분류가 흩어져 있지 않다
    assert collect_ops.JOBS["GG_CULTURE"][2] == "문화·체육"
    for ext_sys, spec in collect_ops.JOB_BREAKDOWN.items():
        assert ext_sys in collect_ops.JOBS
        assert spec["table"].startswith("public.") and spec["labels"]


def test_breakdown_none_for_plain_job():
    # 종류별 현황 대상이 아닌 작업은 DB 를 조회하지 않고 None
    class NoDb:
        async def execute(self, *a, **k):
            raise AssertionError("조회하면 안 된다")
    assert asyncio.new_event_loop().run_until_complete(collect_ops.job_breakdown(NoDb(), "KOSIS")) is None


def test_breakdown_skips_missing_table():
    class FakeResult:
        def __init__(self, v): self.v = v
        def scalar(self): return self.v
        def all(self): return self.v
    class Db:
        def __init__(self): self.calls = []
        async def execute(self, stmt, params=None):
            self.calls.append(str(stmt))
            return FakeResult(False)            # to_regclass → 표 없음
    db = Db()
    assert asyncio.new_event_loop().run_until_complete(collect_ops.job_breakdown(db, "GG_CULTURE")) is None
    assert len(db.calls) == 1                   # 표가 없으면 집계 조회를 하지 않는다


class _Nested:
    """AsyncSession.begin_nested() 대역 — async with 만 받는다."""
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def test_breakdown_counts_in_label_order_with_zero():
    class FakeResult:
        def __init__(self, v): self.v = v
        def scalar(self): return self.v
        def all(self): return self.v
    class Db:
        n = 0
        def begin_nested(self): return _Nested()
        async def execute(self, stmt, params=None):
            Db.n += 1
            return FakeResult(True) if Db.n == 1 else FakeResult([("booknarae_lib", 260), ("braille_lib", 5)])
    out = asyncio.new_event_loop().run_until_complete(collect_ops.job_breakdown(Db(), "GG_CULTURE"))
    assert [b["key"] for b in out] == ["braille_lib", "booknarae_lib", "sports_fac", "sports_org"]
    assert [b["count"] for b in out] == [5, 260, 0, 0]


def test_breakdown_query_error_returns_none():
    class FakeResult:
        def scalar(self): return True
    class Db:
        n = 0
        def begin_nested(self): return _Nested()
        async def execute(self, stmt, params=None):
            Db.n += 1
            if Db.n == 1:
                return FakeResult()
            raise RuntimeError("column does not exist")
    assert asyncio.new_event_loop().run_until_complete(collect_ops.job_breakdown(Db(), "GG_CULTURE")) is None


URL = os.environ.get("ITEST_IITP_DB_URL")
needs_db = pytest.mark.skipif(not URL, reason="ITEST_IITP_DB_URL 미설정 — DB 통합 테스트 생략")
EXT = "ITEST_CO"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _with_session(fn):
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    eng = create_async_engine(URL)
    try:
        async with AsyncSession(eng) as s:
            return await fn(s)
    finally:
        await eng.dispose()


async def _exec(s, sql, **kw):
    from sqlalchemy import text
    r = await s.execute(text(sql), kw)
    await s.commit()
    return r


@needs_db
def test_status_groups_runs_and_counts_regular_failures():
    async def body(s):
        saved = {r[0]: r[1] for r in (await _exec(s, "SELECT setting_key, setting_value FROM sys_ops_setting")).all()}
        await _exec(s, "DELETE FROM sys_batch_run_log WHERE ext_sys = :e", e=EXT)
        ins = ("INSERT INTO sys_batch_run_log (ext_sys, run_mode, run_scope, started_at, exit_code, status,"
               " targets, ok_cnt, fail_cnt, failures, alert_status, alert_detail)"
               " VALUES (:e, :m, :sc, CAST(CAST(:t AS text) AS timestamptz), :x, :st, 3, :ok, :f, CAST(CAST(:fl AS text) AS jsonb), :a, :d)")
        fl = json.dumps([{"id": "DT_A", "stage": "collect", "reason": "err=20"}])
        try:
            await _exec(s, ins, e=EXT, m="db", sc="ALL", t="2026-09-03 03:00+09", x=0, st="SUCCESS", ok=3, f=0, fl="[]", a="NONE", d="정상")
            await _exec(s, ins, e=EXT, m="db", sc="ALL", t="2026-09-18 03:00+09", x=2, st="PARTIAL", ok=2, f=1, fl=fl, a="SENT", d="이상 알림 1명")
            await _exec(s, ins, e=EXT, m="db", sc="ALL", t="2026-10-03 03:00+09", x=2, st="PARTIAL", ok=2, f=1, fl=fl, a="SENT", d="이상 알림 1명")
            await _exec(s, ins, e=EXT, m="file", sc="PARTIAL", t="2026-10-13 15:00+09", x=0, st="SUCCESS", ok=1, f=0, fl="[]", a="NONE", d="수동")
            d = await collect_ops.collect_status(s)
            job = next(j for j in d["jobs"] if j["ext_sys"] == EXT)
            assert d["ready"] is True
            assert job["consecutive_fail"] == 2
            assert job["last_success"] == "2026-09-03 03:00"
            assert job["last_regular"]["started_at"] == "2026-10-03 03:00"
            assert job["last_regular"]["failures"][0]["id"] == "DT_A"
            assert job["last_run"]["mode"] == "file"          # 최근 실행은 수동 시험
            assert len(job["recent"]) == 4
            assert job["group"] == "기타" and job["breakdown"] is None   # 등록되지 않은 작업
            groups = [j["group"] for j in d["jobs"]]
            assert groups == sorted(groups, key=collect_ops.JOB_GROUPS.index)   # 분류 순서 정렬

            cfg = await collect_ops.save_alert_config(s, True, "ops@example.co.kr, ops2@example.co.kr", actor="itest")
            assert cfg["enabled"] is True and cfg["emails"] == ["ops@example.co.kr", "ops2@example.co.kr"]
            assert cfg["updated_by"] == "itest"
            with pytest.raises(collect_ops.AlertConfigError, match="하나 이상"):
                await collect_ops.save_alert_config(s, True, "")
            with pytest.raises(collect_ops.AlertConfigError, match="true/false"):
                await collect_ops.save_alert_config(s, "Y", "ops@example.co.kr")
            off = await collect_ops.save_alert_config(s, False, "")
            assert off["enabled"] is False and off["emails"] == []
        finally:
            await _exec(s, "DELETE FROM sys_batch_run_log WHERE ext_sys = :e", e=EXT)
            for k, v in saved.items():
                await _exec(s, "UPDATE sys_ops_setting SET setting_value = :v WHERE setting_key = :k", k=k, v=v)
    _run(_with_session(body))


@needs_db
def test_breakdown_on_real_db_keeps_session_usable():
    """실DB — 표가 있으면 4종 건수, 없는 표를 가리켜도 None 이고 같은 세션의 다음 조회가 된다."""
    async def body(s):
        out = await collect_ops.job_breakdown(s, "GG_CULTURE")
        has_table = (await s.execute(text_("SELECT to_regclass('public.poi_culture_sports_facility') IS NOT NULL"))).scalar()
        if has_table:
            assert [b["key"] for b in out] == ["braille_lib", "booknarae_lib", "sports_fac", "sports_org"]
            assert all(isinstance(b["count"], int) for b in out)
        else:
            assert out is None
        saved = dict(collect_ops.JOB_BREAKDOWN["GG_CULTURE"])
        try:
            collect_ops.JOB_BREAKDOWN["GG_CULTURE"]["table"] = "public.no_such_table_itest"
            assert await collect_ops.job_breakdown(s, "GG_CULTURE") is None
            if has_table:
                # 표는 있는데 칸이 없는 경우 — 세이브포인트로 되돌리고 세션은 계속 쓸 수 있어야 한다
                collect_ops.JOB_BREAKDOWN["GG_CULTURE"]["table"] = saved["table"]
                collect_ops.JOB_BREAKDOWN["GG_CULTURE"]["sql"] = "SELECT no_such_col, 1 FROM public.poi_culture_sports_facility"
                assert await collect_ops.job_breakdown(s, "GG_CULTURE") is None
            assert (await s.execute(text_("SELECT 1"))).scalar() == 1
        finally:
            collect_ops.JOB_BREAKDOWN["GG_CULTURE"].clear()
            collect_ops.JOB_BREAKDOWN["GG_CULTURE"].update(saved)
    from sqlalchemy import text as text_
    _run(_with_session(body))
