"""크롤 대상 자동 등록 테스트 (#235).

지키려는 것:
  1) 콘솔에서 만든 정책이 크롤 대상에서 누락되지 않는다
  2) 사람이 지정한 crawl 설정(sources[].crawl)을 자동 추정이 덮어쓰지 않는다
  3) 감지가 불가능한 출처(도메인 루트)를 page_hash 로 등록해 오탐을 만들지 않는다
  4) 같은 URL 을 여러 정책이 쓸 때 타겟을 중복 생성하지 않는다
  5) 여러 번 실행해도 결과가 같다(멱등)
"""
import json
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1]
_CRAWLER = _APP / "policy_db" / "crawler"
for _p in (str(_APP), str(_CRAWLER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import target_sync as ts  # noqa: E402


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    """오버레이 파일을 임시 경로로 돌려 실제 데이터를 건드리지 않는다."""
    monkeypatch.setattr(ts, "LOCAL_TARGETS", tmp_path / "crawl_targets.local.json")
    return tmp_path


# ── 감지 방식 추정 ───────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://www.gg.go.kr", "manual_review"),
    ("https://www.bokjiro.go.kr", "manual_review"),
    ("https://www.bokjiro.go.kr/", "manual_review"),
    ("https://www.law.go.kr/법령/장애인복지법", "last_modified_field"),
    ("https://easylaw.go.kr/CSP/CnpClsMain.laf?csmSeq=90", "last_modified_field"),
    ("https://example.go.kr/files/guide_2026.pdf", "pdf_hash"),
    ("https://www.mohw.go.kr/board.es?mid=a10409020000&list_no=1", "page_hash"),
])
def test_guess_method(url, expected):
    assert ts.guess_method(url) == expected


def test_bare_domain_detection():
    assert ts._is_bare_domain("https://www.gg.go.kr") is True
    assert ts._is_bare_domain("https://www.gg.go.kr/") is True
    assert ts._is_bare_domain("https://www.gg.go.kr/welfare/disabled") is False
    # 쿼리스트링만 있어도 구체 페이지로 본다
    assert ts._is_bare_domain("https://www.bokjiro.go.kr?wlfareInfoId=WLF001") is False


# ── 파생 ─────────────────────────────────────────────────────
def test_derive_respects_explicit_crawl_block():
    """사람이 지정한 crawl 설정을 자동 추정이 덮어쓰면 안 된다."""
    policy = {"id": "B900", "title": "테스트", "sources": [{
        "url": "https://www.mohw.go.kr/board.es?mid=a1",
        "publisher": "보건복지부", "priority": "primary",
        "crawl": {"frequency": "quarterly", "change_detection_method": "css_selector_text",
                  "css_selector_hint": "#content .amount", "notes": "지원 금액 표"},
    }]}
    t = ts.derive_targets(policy)[0]
    assert t["change_detection_method"] == "css_selector_text"
    assert t["frequency"] == "quarterly"
    assert t["css_selector_hint"] == "#content .amount"


def test_derive_falls_back_when_crawl_block_invalid():
    """crawl 블록이 있어도 값이 유효하지 않으면 추정으로 넘어간다."""
    policy = {"id": "B901", "title": "테스트", "sources": [{
        "url": "https://www.law.go.kr/법령/장애인복지법",
        "crawl": {"frequency": "매월", "change_detection_method": "아무거나"},
    }]}
    t = ts.derive_targets(policy)[0]
    assert t["change_detection_method"] == "last_modified_field"
    assert t["frequency"] == "monthly"


def test_derive_root_domain_gets_manual_review_with_reason():
    policy = {"id": "B902", "title": "테스트",
              "sources": [{"url": "https://www.gg.go.kr", "publisher": "경기도청"}]}
    t = ts.derive_targets(policy)[0]
    assert t["change_detection_method"] == "manual_review"
    assert "도메인 루트" in (t["notes"] or "")


def test_derive_skips_sources_without_url():
    policy = {"id": "B903", "title": "테스트",
              "sources": [{"publisher": "어딘가"}, {"url": "  "},
                          {"url": "https://a.go.kr/x"}]}
    assert len(ts.derive_targets(policy)) == 1


def test_derive_marks_auto_registered():
    policy = {"id": "B904", "title": "테스트", "sources": [{"url": "https://a.go.kr/x"}]}
    t = ts.derive_targets(policy)[0]
    assert t["auto_registered"] is True
    assert t["used_by_items"] == ["B904"]


# ── 등록 ─────────────────────────────────────────────────────
def _policy(pid, *urls):
    return {"id": pid, "title": f"{pid} 정책",
            "sources": [{"url": u, "publisher": "테스트기관"} for u in urls]}


def test_register_is_idempotent(overlay):
    p = _policy("B905", "https://a.go.kr/one", "https://a.go.kr/two")
    first = ts.register_policy(p)
    second = ts.register_policy(p)
    assert len(first["added"]) == 2
    assert second["added"] == []
    assert len(second["skipped"]) == 2
    assert len(ts.load_local()["targets"]) == 2


def test_register_links_shared_url_instead_of_duplicating(overlay):
    ts.register_policy(_policy("B906", "https://shared.go.kr/page"))
    r = ts.register_policy(_policy("B907", "https://shared.go.kr/page"))
    assert r["added"] == []
    assert len(r["linked"]) == 1
    # 타겟은 하나뿐이고 두 정책이 모두 연결돼야 한다
    assert len(ts.load_local()["targets"]) == 1
    tid = ts.load_local()["targets"][0]["target_id"]
    used = [t["used_by_items"] for t in ts.load_all()["targets"] if t["target_id"] == tid][0]
    assert set(used) == {"B906", "B907"}


def test_register_normalizes_trailing_slash(overlay):
    ts.register_policy(_policy("B908", "https://x.go.kr/page"))
    r = ts.register_policy(_policy("B909", "https://x.go.kr/page/"))
    assert r["added"] == [], "끝 슬래시만 다른 URL 을 다른 출처로 보면 안 된다"


def test_register_reports_manual_review_targets(overlay):
    r = ts.register_policy(_policy("B910", "https://www.gg.go.kr"))
    assert len(r["manual_review"]) == 1


def test_register_without_id_fails(overlay):
    assert ts.register_policy({"title": "id 없음", "sources": []})["ok"] is False


# ── 병합 ─────────────────────────────────────────────────────
def test_load_all_merges_base_and_overlay(overlay):
    base_count = len(ts._load_json(ts.BASE_TARGETS, {"targets": []})["targets"])
    ts.register_policy(_policy("B911", "https://brand.new.go.kr/page"))
    assert len(ts.load_all()["targets"]) == base_count + 1


def test_load_all_survives_missing_overlay(overlay):
    """오버레이 파일이 아직 없어도 기준 파일만으로 동작해야 한다."""
    assert not ts.LOCAL_TARGETS.exists()
    assert len(ts.load_all()["targets"]) > 0


def test_overlay_is_valid_json_after_write(overlay):
    ts.register_policy(_policy("B912", "https://y.go.kr/page"))
    data = json.loads(ts.LOCAL_TARGETS.read_text(encoding="utf-8"))
    assert "targets" in data and "updated_at" in data


# ── 커버리지 ─────────────────────────────────────────────────
def test_coverage_map_counts_targets_per_policy(overlay):
    ts.register_policy(_policy("B913", "https://c1.go.kr/a", "https://c1.go.kr/b"))
    assert ts.coverage_map().get("B913") == 2


def test_real_base_targets_cover_the_handcrafted_policies():
    """기준 파일이 B001~B043 을 실제로 커버하는지 — 회귀 감시."""
    covered = ts.covered_policy_ids()
    for pid in ("B001", "B020", "B043"):
        assert pid in covered, f"{pid} 가 크롤 대상에서 빠졌다"
