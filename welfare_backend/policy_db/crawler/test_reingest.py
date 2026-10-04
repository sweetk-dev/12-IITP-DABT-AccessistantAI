# crawler/test_reingest.py
# DB 재적재 실패 전달 · 원자적 저장 · 정책 ID 검증 · 항목 파일 건너뛰기 테스트.
# DB·네트워크 없이 동작한다(하위 프로세스와 DB 접속은 대역으로 바꾼다).
#
# 지키려는 것:
#   1) ingest_sync 가 0 이 아닌 코드로 끝나거나 제한 시간을 넘기면 _trigger_reingest 가 예외를 올린다
#      (메시지에 접속 문자열·비밀번호가 실리지 않는다)
#   2) 재적재가 실패해도 items/ 파일 변경은 유지되고 응답에 reingested: False 가 실린다
#   3) 백그라운드용 trigger_reingest 는 예외를 밖으로 내지 않는다
#   4) ingest_sync 는 접속·스키마 실패, 읽을 수 없는 항목 파일이 있으면 종료 코드 1 로 끝난다
#      (읽을 수 없는 파일은 건너뛰고 나머지는 계속 처리한다)
#   5) 항목 저장은 중간에 실패해도 기존 파일을 손상시키지 않는다
#   6) 형식이 틀린 정책 ID 는 경로 해석·생성·수정에서 거부되고, B100 이상도 목록·다음 ID 에 잡힌다
#
# 실행: pytest test_reingest.py
import json
import subprocess
import sys
from pathlib import Path

import pytest

try:
    from . import confirm_apply as ca
    from . import policy_core as pc
    from . import review_core as rc
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from crawler import confirm_apply as ca  # type: ignore
    from crawler import policy_core as pc  # type: ignore
    from crawler import review_core as rc  # type: ignore

POLICY_DB = Path(__file__).resolve().parent.parent  # policy_db/
if str(POLICY_DB) not in sys.path:
    sys.path.insert(0, str(POLICY_DB))


@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    """items/·staging/·오버레이를 임시 경로로 돌려 실제 데이터를 건드리지 않는다."""
    items = tmp_path / "items"
    staging = tmp_path / "crawler" / "staging"
    items.mkdir(parents=True)
    staging.mkdir(parents=True)
    monkeypatch.setattr(ca, "ITEMS_DIR", items)
    monkeypatch.setattr(ca, "STAGING_DIR", staging)
    monkeypatch.setattr(ca, "BACKUPS_DIR", items / ".backups")
    monkeypatch.setattr(pc.ts, "LOCAL_TARGETS", tmp_path / "crawl_targets.local.json")
    monkeypatch.setattr(pc.ts, "SNAPSHOTS_DIR", tmp_path / "crawler" / "snapshots")
    return tmp_path


def _real_item(pid="B001"):
    """스키마를 통과하는 실제 항목 1건(읽기 전용으로 복사해 쓴다)."""
    f = sorted((POLICY_DB / "items").glob(f"{pid}_*.json"))[0]
    return json.loads(f.read_text(encoding="utf-8"))


def _fail_reingest(_ids):
    raise ca.ReingestError("ingest_sync 실패(exit 1): 대역")


# ── 1) _trigger_reingest 실패 전달 ───────────────────────────
def test_trigger_reingest_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setenv("DB_PASS", "s3cr3t-pw")
    stderr = ("2026-01-01 - ERROR - DB 접속 실패: connection to server at \"10.1.2.3\", port 5432 failed: "
              "FATAL: password authentication failed for user \"welfare\"\n"
              "dsn postgresql://welfare:s3cr3t-pw@10.1.2.3:5432/welfare_db host=10.1.2.3 password=s3cr3t-pw\n")
    monkeypatch.setattr(ca.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", stderr))
    with pytest.raises(ca.ReingestError) as ei:
        ca._trigger_reingest(["B001"])
    msg = str(ei.value)
    assert "exit 1" in msg
    assert "DB 접속 실패" in msg, "원인 파악에 필요한 문구는 남아야 한다"
    for leaked in ("s3cr3t-pw", "10.1.2.3", "postgresql://", "5432", "\"welfare\""):
        assert leaked not in msg, f"접속 정보가 메시지에 실렸다: {leaked}"


def test_trigger_reingest_raises_on_timeout(monkeypatch):
    seen = {}

    def fake_run(*a, **k):
        seen.update(k)
        raise subprocess.TimeoutExpired(cmd="ingest_sync", timeout=k.get("timeout"))

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    with pytest.raises(ca.ReingestError) as ei:
        ca._trigger_reingest(["B001"])
    assert "시간" in str(ei.value)
    assert seen.get("timeout") == ca.REINGEST_TIMEOUT_SEC, "제한 시간 없이 하위 프로세스를 돌리면 안 된다"


def test_trigger_reingest_raises_when_process_cannot_start(monkeypatch):
    def fake_run(*a, **k):
        raise OSError("실행 불가")

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    with pytest.raises(ca.ReingestError):
        ca._trigger_reingest(["B001"])


def test_trigger_reingest_ok_on_zero_exit(monkeypatch):
    monkeypatch.setattr(ca.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "요약\n", ""))
    assert ca._trigger_reingest(["B001"]) is None


# ── 2) 호출자: 파일 변경 유지 + reingested False ─────────────
def test_update_policy_keeps_file_and_reports_reingest_failure(data_dirs, monkeypatch):
    item = _real_item()
    fp = ca.ITEMS_DIR / "B001_x.json"
    fp.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ca, "_trigger_reingest", _fail_reingest)
    edited = dict(item); edited["short_summary"] = item["short_summary"] + " (수정)"
    r = pc.update_policy("B001", edited)
    assert r["ok"] is True
    assert r["reingested"] is False
    assert "ingest_sync 실패" in r["reingest_error"]
    assert json.loads(fp.read_text(encoding="utf-8"))["short_summary"].endswith("(수정)")


def test_deactivate_keeps_file_and_reports_reingest_failure(data_dirs, monkeypatch):
    fp = ca.ITEMS_DIR / "B001_x.json"
    fp.write_text(json.dumps(_real_item(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ca, "_trigger_reingest", _fail_reingest)
    r = pc.deactivate("B001")
    assert r["ok"] is True and r["reingested"] is False and r["reingest_error"]
    assert json.loads(fp.read_text(encoding="utf-8"))["active"] is False


def test_apply_selected_keeps_file_and_reports_reingest_failure(data_dirs, monkeypatch):
    item = _real_item()
    fp = ca.ITEMS_DIR / "B001_x.json"
    fp.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    staged = dict(item); staged["short_summary"] = "새 요약"
    (ca.STAGING_DIR / "B001_20260101.staged.json").write_text(
        json.dumps(staged, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ca, "_trigger_reingest", _fail_reingest)
    r = rc.apply_selected("B001", ["short_summary"], reingest=True)
    assert r["ok"] is True
    assert r["reingested"] is False and "ingest_sync 실패" in r["reingest_error"]
    assert json.loads(fp.read_text(encoding="utf-8"))["short_summary"] == "새 요약"


# ── 3) 백그라운드용 래퍼는 예외를 내지 않는다 ────────────────
def test_background_trigger_reingest_returns_failure_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(ca, "_trigger_reingest", _fail_reingest)
    with caplog.at_level("ERROR"):
        r = rc.trigger_reingest(["B001"])
    assert r["ok"] is False and r["reingested"] is False
    assert "ingest_sync 실패" in r["reingest_error"]
    assert any("재적재 실패" in rec.getMessage() for rec in caplog.records), "실패가 로그에 남아야 한다"


def test_cli_reports_reingest_failure_with_manual_hint(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["confirm_apply", "--policy-id", "B001", "-y", "--reingest"])
    monkeypatch.setattr(ca, "_apply_one", lambda *a, **k: True)
    monkeypatch.setattr(ca, "_trigger_reingest", _fail_reingest)
    with pytest.raises(SystemExit) as ei:
        ca.main()
    assert ei.value.code == 1
    out = capsys.readouterr().out
    assert "수동 실행 안내" in out
    assert "총 1개 항목 반영 완료" in out, "파일 반영 결과는 그대로 보고돼야 한다"


# ── 4) ingest_sync 종료 코드 ─────────────────────────────────
class _FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def ingest():
    return pytest.importorskip("ingest_sync")


def test_ingest_exits_nonzero_when_db_connect_fails(ingest, monkeypatch):
    def boom(**k):
        raise RuntimeError("접속 불가")

    monkeypatch.setattr(ingest.psycopg2, "connect", boom)
    with pytest.raises(SystemExit) as ei:
        ingest.main([])
    assert ei.value.code == 1


def test_ingest_exits_nonzero_when_schema_fails(ingest, monkeypatch):
    monkeypatch.setattr(ingest.psycopg2, "connect", lambda **k: _FakeConn())
    monkeypatch.setattr(ingest, "register_vector", lambda conn: None)

    def boom(cur, conn, rebuild=False):
        raise RuntimeError("권한 없음")

    monkeypatch.setattr(ingest, "ensure_schema", boom)
    with pytest.raises(SystemExit) as ei:
        ingest.main([])
    assert ei.value.code == 1


def test_ingest_skips_broken_item_and_continues(ingest, monkeypatch, tmp_path):
    """깨진 항목 파일은 건너뛰고 뒤 순서의 정상 항목은 반영하되, 종료 코드는 1 이어야 한다."""
    items = tmp_path / "items"
    items.mkdir()
    (items / "B001_broken.json").write_text('{"id": "B001", "title": ', encoding="utf-8")  # 잘린 JSON
    good = _real_item("B002")
    good["active"] = False   # 비활성 항목은 임베딩 호출 없이 마스터 행만 반영된다
    (items / "B002_good.json").write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")

    conn = _FakeConn()
    monkeypatch.setenv("POLICY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ingest.psycopg2, "connect", lambda **k: conn)
    monkeypatch.setattr(ingest, "register_vector", lambda c: None)
    monkeypatch.setattr(ingest, "ensure_schema", lambda cur, c, rebuild=False: None)
    with pytest.raises(SystemExit) as ei:
        ingest.main([])
    assert ei.value.code == 1, "건너뛴 파일이 있으면 성공(0)으로 끝나면 안 된다"
    inserted = [p[0] for sql, p in conn.cur.executed if "INSERT INTO welfare_policies" in sql]
    assert inserted == ["B002"], "깨진 파일 뒤의 정상 항목이 반영돼야 한다"


def test_process_file_returns_false_for_non_object_json(ingest, tmp_path):
    f = tmp_path / "B003_list.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    conn = _FakeConn()
    assert ingest.process_file(str(f), "hash", conn.cur, conn) is False
    assert conn.cur.executed == []


# ── 5) 원자적 저장 ───────────────────────────────────────────
def test_atomic_write_replaces_content(tmp_path):
    f = tmp_path / "B001_x.json"
    f.write_text("old", encoding="utf-8")
    ca.atomic_write_text(f, "새 내용")
    assert f.read_text(encoding="utf-8") == "새 내용"
    assert [p.name for p in tmp_path.iterdir()] == ["B001_x.json"], "임시 파일이 남으면 안 된다"


def test_atomic_write_failure_leaves_original_intact(tmp_path, monkeypatch):
    f = tmp_path / "B001_x.json"
    f.write_text("old", encoding="utf-8")

    def boom(src, dst):
        raise OSError("교체 실패")

    monkeypatch.setattr(ca.os, "replace", boom)
    with pytest.raises(OSError):
        ca.atomic_write_text(f, "new")
    assert f.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["B001_x.json"]


def test_policy_save_goes_through_atomic_write(data_dirs, monkeypatch):
    """항목 저장 도중 실패하면 기존 항목 파일이 그대로 남아야 한다."""
    item = _real_item()
    fp = ca.ITEMS_DIR / "B001_x.json"
    original = json.dumps(item, ensure_ascii=False)
    fp.write_text(original, encoding="utf-8")

    def boom(src, dst):
        raise OSError("교체 실패")

    monkeypatch.setattr(ca.os, "replace", boom)
    edited = dict(item); edited["short_summary"] = "바뀐 요약"
    with pytest.raises(OSError):
        pc.update_policy("B001", edited, reingest=False)
    assert fp.read_text(encoding="utf-8") == original


# ── 6) 정책 ID 검증 ──────────────────────────────────────────
@pytest.mark.parametrize("pid", ["B001", "B051", "B100", "B1234"])
def test_valid_policy_ids(pid):
    assert ca.validate_policy_id(pid) == pid


@pytest.mark.parametrize("pid", ["*", "B0*", "../B001", "B001/../x", "B01", "b001", "C001",
                                 "B001_x", "B001\n", "", None, 1, "B００１"])
def test_invalid_policy_ids(pid):
    with pytest.raises(ValueError):
        ca.validate_policy_id(pid)


def test_all_existing_items_pass_id_rule():
    files = sorted((POLICY_DB / "items").glob("*.json"))
    assert files
    for f in files:
        ca.validate_policy_id(f.name.split("_")[0])
        ca.validate_policy_id(json.loads(f.read_text(encoding="utf-8"))["id"])


def test_path_rejects_wildcard_id(data_dirs):
    (ca.ITEMS_DIR / "B001_x.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        pc._path("*")
    with pytest.raises(ValueError):
        pc._path("B0*")
    assert pc._path("B001").name == "B001_x.json"


def test_public_functions_reject_bad_id_without_touching_files(data_dirs):
    fp = ca.ITEMS_DIR / "B001_x.json"
    fp.write_text(json.dumps(_real_item(), ensure_ascii=False), encoding="utf-8")
    before = fp.read_text(encoding="utf-8")
    assert "error" in pc.get_policy("*")
    assert pc.deactivate("B0*")["ok"] is False
    assert pc.reactivate("*")["ok"] is False
    assert pc.update_policy("*", {"id": "*"})["ok"] is False
    assert fp.read_text(encoding="utf-8") == before


def test_create_policy_rejects_path_traversal_id(data_dirs):
    data = _real_item()
    data["id"] = "../B999"
    r = pc.create_policy(data, slug="x", reingest=False)
    assert r["ok"] is False and "형식" in r["error"]
    assert list(data_dirs.rglob("*B999*")) == [], "items/ 밖에 파일이 만들어지면 안 된다"


def test_listing_and_next_id_cover_three_digit_overflow(data_dirs):
    """B100 이상도 목록에 잡히고 next_id 가 같은 값을 반복하지 않는다."""
    for pid in ("B099", "B100"):
        d = _real_item(); d["id"] = pid
        (ca.ITEMS_DIR / f"{pid}_x.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert [p["policy_id"] for p in pc.list_policies()] == ["B099", "B100"]
    assert pc.next_id() == "B101"


def test_schema_rejects_malformed_id():
    import jsonschema
    schema = json.loads((POLICY_DB / "schema.json").read_text(encoding="utf-8"))
    d = _real_item(); d["id"] = "B0*"
    assert any(list(e.path) == ["id"] for e in jsonschema.Draft7Validator(schema).iter_errors(d))
