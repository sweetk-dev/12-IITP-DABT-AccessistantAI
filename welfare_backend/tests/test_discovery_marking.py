"""신규 발굴의 '처리됨' 표시 · 보존기간 파기 예외 · 파기 잡 등록 테스트.

DB·LLM 없이 동작한다(조회·분류·초안·보강·표시를 대역으로 바꾼다).

지키려는 것:
  1) 결과가 확정된 군집(후보 저장 / 보강 staging 적재 / covered / 정책 무관)의 질의만 표시한다
  2) 초안 생성 실패, 보강 실패(ok: False), 분류 응답에서 빠진 군집의 질의는 표시하지 않는다
     — 다음 회차에 다시 대상이 된다
  3) 초안을 얻지 못한 군집은 후보 파일을 만들지 않는다(회차마다 중복 후보가 쌓이지 않게)
  4) 발굴 산출물이 참조 중인 질의 id 는 보존기간 파기에서 빠진다(반려된 후보 제외)
  5) 인앱 스케줄러에 일 1회 파기 잡이 등록된다
"""
import json
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

dc = pytest.importorskip("discovery_core")


def _unit(i, n=8):
    """서로 직교하는 임베딩 — 질의마다 별도 군집이 되게 한다."""
    v = [0.0] * n
    v[i] = 1.0
    return v


# 군집 번호 = 행 순서. id 는 100 + 번호.
_ROWS = [{"id": 100 + i, "q": f"질문{i}", "emb": _unit(i)} for i in range(7)]

_CLF = [
    {"idx": 0, "policy_related": True, "klass": "new", "topic": "초안 성공"},
    {"idx": 1, "policy_related": True, "klass": "new", "topic": "초안 실패"},
    {"idx": 2, "policy_related": True, "klass": "gap", "covered_by": "B001", "gap_detail": "적재 성공"},
    {"idx": 3, "policy_related": True, "klass": "gap", "covered_by": "B002", "gap_detail": "적재 실패"},
    {"idx": 4, "policy_related": True, "klass": "covered", "covered_by": "B003"},
    {"idx": 5, "policy_related": False, "klass": "covered"},
    # idx 6 은 분류 응답에서 빠짐
    {"idx": 99, "policy_related": False},   # 범위를 벗어난 번호 — 무시돼야 한다
]


@pytest.fixture
def disc(tmp_path, monkeypatch):
    marked = {"processed": [], "excluded": []}

    def fake_gemini(prompt, grounding=False, **kw):
        if not grounding:
            return json.dumps(_CLF, ensure_ascii=False)
        if "초안 실패" in prompt:
            return ""                       # 빈 응답 → 초안 파싱 실패
        return json.dumps({"id": "", "title": "새 정책", "sources": []}, ensure_ascii=False)

    def fake_gap(pid, member_qs, member_ids, gap_detail):
        if pid == "B001":
            return {"ok": True, "policy_id": pid, "changed": ["faq"]}
        return {"ok": False, "error": "보강 응답 없음"}

    def fake_mark(ids, excluded=False):
        marked["excluded" if excluded else "processed"].extend(ids)

    monkeypatch.setattr(dc, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(dc, "_ensure_processed_col", lambda: None)
    monkeypatch.setattr(dc, "_load_unresolved", lambda limit=500: [dict(r) for r in _ROWS])
    monkeypatch.setattr(dc, "_existing_titles", lambda: [("B001", "기존 정책", "교통")])
    monkeypatch.setattr(dc, "_gemini", fake_gemini)
    monkeypatch.setattr(dc, "_make_gap_staged", fake_gap)
    monkeypatch.setattr(dc, "_mark_processed", fake_mark)
    monkeypatch.setattr(dc, "_CAND_DIR", tmp_path / "discovery" / "candidates")
    monkeypatch.setattr(dc, "_REPORT_DIR", tmp_path / "discovery" / "reports")
    monkeypatch.setattr(dc, "_STAGING_DIR", tmp_path / "crawler" / "staging")
    return marked


def test_only_successful_clusters_are_marked(disc):
    res = dc.run_discovery()
    # 0: 후보 저장, 2: 보강 적재, 4: covered → 처리됨 / 5: 정책 무관 → 제외됨
    assert sorted(disc["processed"]) == [100, 102, 104]
    assert disc["excluded"] == [105]
    assert res["processed"] == 3 and res["excluded"] == 1


def test_failed_and_missing_clusters_stay_unmarked(disc):
    res = dc.run_discovery()
    marked = set(disc["processed"]) | set(disc["excluded"])
    assert 101 not in marked, "초안 생성이 실패한 군집을 표시하면 다시 시도되지 않는다"
    assert 103 not in marked, "보강이 ok: False 인 군집을 표시하면 다시 시도되지 않는다"
    assert 106 not in marked, "분류 응답에서 빠진 군집을 표시하면 판정 없이 대상에서 빠진다"
    assert res["retry"] == 3


def test_failed_draft_does_not_create_candidate_file(disc):
    res = dc.run_discovery()
    files = sorted(dc._CAND_DIR.glob("C*.json"))
    assert len(files) == 1 and res["new_candidates"] == 1
    cand = json.loads(files[0].read_text(encoding="utf-8"))
    assert cand["query_ids"] == [100] and cand["draft_item"]


def test_gap_exception_leaves_cluster_unmarked(disc, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("외부검색 오류")

    monkeypatch.setattr(dc, "_make_gap_staged", boom)
    dc.run_discovery()
    assert 102 not in disc["processed"] and 103 not in disc["processed"]


def test_non_list_classification_marks_nothing(disc, monkeypatch):
    monkeypatch.setattr(dc, "_gemini", lambda *a, **k: json.dumps({"idx": 0}))
    res = dc.run_discovery()
    assert "error" in res
    assert disc["processed"] == [] and disc["excluded"] == []


@pytest.mark.parametrize("raw,expected", [
    (0, 0), ("2", 2), (" 3 ", 3), (-1, None), (7, None), ("x", None), (None, None), (True, None), (1.0, None),
])
def test_cluster_idx_parsing(raw, expected):
    assert dc._cluster_idx({"idx": raw}, 7) == expected


# ── 보존기간 파기 예외 ───────────────────────────────────────
def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_referenced_query_ids_collects_live_references(tmp_path, monkeypatch):
    cand, stag = tmp_path / "candidates", tmp_path / "staging"
    monkeypatch.setattr(dc, "_CAND_DIR", cand)
    monkeypatch.setattr(dc, "_STAGING_DIR", stag)
    _write(cand / "C1.json", {"status": "pending", "query_ids": [1, 2]})
    _write(cand / "C2.json", {"status": "approved", "query_ids": [3]})
    _write(cand / "C3.json", {"status": "rejected", "query_ids": [4]})       # 반려 → 참조 아님
    _write(stag / "B001_disc1.disc.json", {"query_ids": [5, "6"]})            # 검토 대기 보강 제안
    _write(stag / ".rejected" / "B002_disc1.disc.json", {"query_ids": [7]})   # 반려된 제안 → 참조 아님
    assert dc.referenced_query_ids() == {1, 2, 3, 5, 6}


def test_referenced_query_ids_empty_when_no_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "_CAND_DIR", tmp_path / "none1")
    monkeypatch.setattr(dc, "_STAGING_DIR", tmp_path / "none2")
    assert dc.referenced_query_ids() == set()


def test_referenced_query_ids_raises_on_unreadable_candidate(tmp_path, monkeypatch):
    """읽지 못한 후보를 건너뛰면 그 후보가 참조하는 행이 파기 대상에 들어간다 → 예외로 중단."""
    cand = tmp_path / "candidates"
    monkeypatch.setattr(dc, "_CAND_DIR", cand)
    monkeypatch.setattr(dc, "_STAGING_DIR", tmp_path / "staging")
    cand.mkdir()
    (cand / "C1.json").write_text('{"status": "pending", "query_ids": [1', encoding="utf-8")
    with pytest.raises(ValueError):
        dc.referenced_query_ids()


def _purge_module():
    """파기 스크립트 import — DB 드라이버가 없는 환경에서는 건너뛴다."""
    try:
        import scripts.purge_old_queries as purge
        from sqlalchemy.dialects import postgresql
    except Exception as e:      # noqa: BLE001 — 선택 의존성 부재는 건너뜀 사유
        pytest.skip(f"파기 스크립트를 불러올 수 없음: {e}")
    return purge, postgresql


def test_purge_where_excludes_referenced_ids():
    purge, postgresql = _purge_module()
    from sqlalchemy import text
    where, binds, vals = purge._purge_where({30, 10, 20})
    assert vals == {"keep": [10, 20, 30]}
    vals["d"] = 90
    stmt = text("DELETE FROM unresolved_queries WHERE " + where).bindparams(*binds).bindparams(**vals)
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True})
    sql = str(compiled)
    # 목록 길이만큼 자리표시자가 펼쳐지고, 값이 그대로 바인드된다
    assert "id NOT IN (" in sql and sql.count("keep_") == 3
    assert sorted(v for k, v in compiled.params.items() if k.startswith("keep_")) == [10, 20, 30]
    assert compiled.params["d"] == 90
    assert "created_at < NOW() - make_interval(days :=" in sql


def test_purge_where_without_references_is_age_only():
    purge, _ = _purge_module()
    where, binds, vals = purge._purge_where(set())
    assert "NOT IN" not in where and binds == [] and vals == {}


def test_purge_aborts_when_references_cannot_be_read(monkeypatch):
    purge, _ = _purge_module()
    import asyncio

    def boom():
        raise ValueError("후보 파일 손상")

    class _NoSession:
        def __call__(self):
            raise AssertionError("참조 목록 없이 DB 에 접속하면 안 된다")

    class _Engine:
        async def dispose(self):
            pass

    monkeypatch.setattr(purge, "_load_keep_ids", boom)
    monkeypatch.setattr(purge, "AsyncSessionLocal", _NoSession())
    monkeypatch.setattr(purge, "engine", _Engine())
    assert asyncio.run(purge.main(90, False)) == 1


# ── 스케줄러 잡 등록 ─────────────────────────────────────────
def test_scheduler_registers_daily_purge_job(monkeypatch):
    pytest.importorskip("apscheduler")
    import scheduler as ops

    captured = {}

    class _FakeSched:
        def __init__(self, **kw):
            captured["tz"] = kw.get("timezone")
            captured["jobs"] = {}

        def add_job(self, func, trigger=None, **kw):
            captured["jobs"][kw.get("id")] = (func, trigger)

        def start(self):
            captured["started"] = True

    import apscheduler.schedulers.background as bg
    monkeypatch.setattr(bg, "BackgroundScheduler", _FakeSched)
    monkeypatch.setattr(ops, "_sched", None)
    monkeypatch.setattr(ops, "_load_cfg", lambda: dict(ops.DEFAULT_CFG))
    ops.start()
    monkeypatch.setattr(ops, "_sched", None)

    func, trig = captured["jobs"]["purge_scheduled"]
    assert func is ops._run_purge
    assert captured["tz"] == ops.KST
    fields = {f.name: str(f) for f in trig.fields}
    assert fields["hour"] == "3" and fields["minute"] == "40"
    assert fields["day"] == "*", "일 1회(매일) 실행이어야 한다"
    # 다른 잡과 같은 시각에 겹치지 않는다: 임베딩 백필은 0·15·30·45분, 백업·발굴은 04:00
    assert int(fields["minute"]) % 15 != 0
    assert "purge" in ops.get_status()


def test_run_purge_invokes_script_with_retention_days(monkeypatch):
    import scheduler as ops
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw

        class R:
            returncode = 0
            stdout = "삭제 완료: 0 행"
            stderr = ""
        return R()

    monkeypatch.setattr(ops.subprocess, "run", fake_run)
    monkeypatch.setattr(ops, "_load_cfg", lambda: dict(ops.DEFAULT_CFG))
    ops._run_purge()
    assert seen["cmd"][0] == sys.executable
    assert seen["cmd"][1:] == ["-m", "scripts.purge_old_queries", "--days", "90"]
    assert seen["kw"]["cwd"] == str(ops._APP) and seen["kw"]["timeout"]
    st = ops.get_status()["purge"]
    assert st["running"] is False and st["last_status"] == "ok"


def test_run_embed_uses_same_interpreter_and_app_dir(monkeypatch):
    import scheduler as ops
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(ops.subprocess, "run", fake_run)
    ops._run_embed()
    assert seen["cmd"][0] == sys.executable
    assert seen["kw"]["cwd"] == str(ops._APP)


# ── 관리자 API 진입점 ────────────────────────────────────────
def _admin_module():
    """admin_router import — 웹 프레임워크·DB 드라이버가 없는 환경에서는 건너뛴다."""
    try:
        import admin_router
        from fastapi import HTTPException
    except Exception as e:      # noqa: BLE001 — 선택 의존성 부재는 건너뜀 사유
        pytest.skip(f"admin_router 를 불러올 수 없음: {e}")
    return admin_router, HTTPException


@pytest.mark.parametrize("pid", ["*", "B0*", "../B001", "B01", "crawl-coverage"])
def test_admin_rejects_malformed_policy_id_with_400(pid):
    admin, HTTPException = _admin_module()
    with pytest.raises(HTTPException) as ei:
        admin._check_policy_id(pid)
    assert ei.value.status_code == 400


def test_admin_policy_endpoints_validate_id_before_use(monkeypatch):
    """경로 파라미터의 정책 ID 는 아래 계층(파일 glob·하위 프로세스)에 닿기 전에 걸러진다."""
    admin, HTTPException = _admin_module()

    def must_not_run(*a, **k):
        raise AssertionError("형식이 틀린 ID 가 아래 계층까지 내려갔다")

    for mod, names in ((admin.pc, ("get_policy", "update_policy", "deactivate", "reactivate")),
                       (admin.rc, ("get_review", "apply_selected", "reject", "set_triage")),
                       (admin.ops, ("run_crawl_policy", "run_init_baseline"))):
        for n in names:
            monkeypatch.setattr(mod, n, must_not_run)
    calls = [
        lambda: admin.staging_review("*"),
        lambda: admin.staging_apply("*", {}),
        lambda: admin.staging_reject("*"),
        lambda: admin.staging_triage("*", {}),
        lambda: admin.policy_get("*"),
        lambda: admin.policy_update("*", {}),
        lambda: admin.policy_deactivate("*"),
        lambda: admin.policy_reactivate("*"),
        lambda: admin.policy_crawl("*"),
        lambda: admin.policy_init_baseline("*"),
        lambda: admin.policy_register_crawl("*"),
        lambda: admin.ops_init_baseline({"policy_id": "*"}),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as ei:
            call()
        assert ei.value.status_code == 400


def test_admin_background_reingest_logs_failure_and_does_not_raise(monkeypatch, caplog):
    admin, _ = _admin_module()
    monkeypatch.setattr(admin.rc, "trigger_reingest",
                        lambda ids: {"ok": False, "reingested": False, "reingest_error": "ingest_sync 실패(exit 1)"})
    with caplog.at_level("ERROR"):
        admin._reingest_job(["B001"])          # 예외 없이 끝나야 한다
    assert any("재적재 실패" in r.getMessage() and "B001" in r.getMessage() for r in caplog.records)

    def boom(ids):
        raise RuntimeError("예상하지 못한 오류")

    monkeypatch.setattr(admin.rc, "trigger_reingest", boom)
    with caplog.at_level("ERROR"):
        admin._reingest_job(["B002"])          # 스레드 본체에서 예외가 새어 나가면 안 된다
    assert any("B002" in r.getMessage() for r in caplog.records)
