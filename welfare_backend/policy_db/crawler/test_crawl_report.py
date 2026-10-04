# crawler/test_crawl_report.py
# 감지 실패(detect_failed) 결과가 크롤 리포트에 어떻게 실리는지 확인한다.
# 네트워크·LLM 없이 동작한다(감지기를 대역으로 바꾸고 --skip-llm 으로 실행).
#
# 지키려는 것:
#   1) 감지 실패는 리포트의 실패 목록(failures)에 사유와 함께 올라간다
#   2) 정기 검사에서는 변경으로 집계되지 않는다(= LLM 갱신 대상이 아니다)
#   3) 재검증 모드에서는 실패 목록에 올리면서도 재검증 입력에는 포함한다
#   4) 기준 확정 모드에서 감지 실패 타겟은 비교 기준이 저장되지 않는다
#
# 실행: pytest test_crawl_report.py
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from . import crawler as cr
    from .detectors import ChangeResult
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from crawler import crawler as cr  # type: ignore
    from crawler.detectors import ChangeResult  # type: ignore


_TARGETS = {"targets": [
    {"target_id": "t_fail", "title": "법령", "url": "https://example.go.kr/law", "publisher": "기관",
     "change_detection_method": "last_modified_field", "used_by_items": ["B001"]},
    {"target_id": "t_same", "title": "안내", "url": "https://example.go.kr/page", "publisher": "기관",
     "change_detection_method": "page_hash", "used_by_items": ["B002"]},
]}


async def _detect_failed(target, snapshot_dir, *, client, revalidate=False):
    return ChangeResult(False, "detect_failed | last_modified 키 추출 실패",
                        new_content=b"<html>body</html>" if revalidate else None,
                        new_hash=None, status="detect_failed")


async def _detect_same(target, snapshot_dir, *, client, revalidate=False):
    return ChangeResult(False, "변경 없음", new_content=b"<html>x</html>" if revalidate else None,
                        new_hash="h1")


@pytest.fixture
def crawl_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(cr, "ITEMS_DIR", tmp_path / "items")
    monkeypatch.setattr(cr, "SNAPSHOTS_DIR", tmp_path / "crawler" / "snapshots")
    monkeypatch.setattr(cr, "STAGING_DIR", tmp_path / "crawler" / "staging")
    monkeypatch.setattr(cr, "REPORTS_DIR", tmp_path / "crawler" / "reports")
    monkeypatch.setattr(cr, "MANUAL_STATE", tmp_path / "crawler" / "manual_review_state.json")
    monkeypatch.setattr(cr, "_load_targets", lambda: json.loads(json.dumps(_TARGETS)))
    monkeypatch.setitem(cr.DETECTORS, "last_modified_field", _detect_failed)
    monkeypatch.setitem(cr.DETECTORS, "page_hash", _detect_same)
    return tmp_path


def _args(**over):
    base = dict(only=None, policy=None, dry_run=False, skip_llm=True,
                revalidate=False, init_baseline=False)
    base.update(over)
    return SimpleNamespace(**base)


def _report(tmp_path):
    files = sorted((tmp_path / "crawler" / "reports").glob("*.json"))
    assert files, "리포트가 작성돼야 한다"
    return json.loads(files[-1].read_text(encoding="utf-8")), files[-1].with_suffix(".md").read_text(encoding="utf-8")


def test_detect_failed_is_listed_as_failure_not_change(crawl_env):
    assert asyncio.run(cr.run(_args())) == 0
    rep, md = _report(crawl_env)
    assert [f["target_id"] for f in rep["failures"]] == ["t_fail"]
    assert "last_modified" in rep["failures"][0]["reason"]
    assert rep["changes"] == [], "감지 실패가 변경으로 집계되면 LLM 갱신이 호출된다"
    assert rep["summary"]["affected_items"] == []
    assert "t_fail" in md.split("## 실패 목록")[1].split("##")[0], "사람이 읽는 리포트에도 드러나야 한다"


def test_detect_failed_still_feeds_revalidation(crawl_env):
    assert asyncio.run(cr.run(_args(revalidate=True))) == 0
    rep, _ = _report(crawl_env)
    assert [f["target_id"] for f in rep["failures"]] == ["t_fail"]
    assert sorted(c["target_id"] for c in rep["changes"]) == ["t_fail", "t_same"]


def test_detect_failed_gets_no_baseline_on_init(crawl_env):
    assert asyncio.run(cr.run(_args(init_baseline=True))) == 0
    snaps = crawl_env / "crawler" / "snapshots"
    assert (snaps / "t_same" / "page_hash.txt").exists()
    assert not (snaps / "t_fail" / "last_modified.txt").exists()
