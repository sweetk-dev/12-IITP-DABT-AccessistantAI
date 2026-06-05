# crawler/test_snapshot_split.py
# #27 A안 회귀 테스트 — 감지/확정 스냅샷 분리.
#   - 감지(save_content_snapshot): 본문/pending 청크만, baseline(해시·chunks.json)은 미전진
#   - 확정(save_baseline_snapshot): 해시 전진 + pending_chunks -> chunks.json 승격
# 실행: python test_snapshot_split.py
import tempfile
from pathlib import Path

try:
    from .detectors import (ChangeResult, save_content_snapshot, save_baseline_snapshot,
                            save_snapshot, _read_prev_hash, _read_prev_chunks, SNAPSHOT_FILES)
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from crawler.detectors import (ChangeResult, save_content_snapshot, save_baseline_snapshot,
                            save_snapshot, _read_prev_hash, _read_prev_chunks, SNAPSHOT_FILES)


def test_content_snapshot_does_not_advance_baseline():
    with tempfile.TemporaryDirectory() as d:
        snap = Path(d)
        res = ChangeResult(True, "changed", new_content=b"<html>x</html>",
                           new_hash="H1", new_chunks=["chunk one here yes", "chunk two ok"])
        save_content_snapshot(snap, "page_hash", res)
        assert _read_prev_hash(snap, "page_hash") is None      # baseline 미전진
        assert _read_prev_chunks(snap) == []                   # chunks.json 아직 없음
        assert (snap / "pending_chunks.json").exists()
        assert (snap / "latest.html").exists()


def test_baseline_snapshot_advances_and_promotes_chunks():
    with tempfile.TemporaryDirectory() as d:
        snap = Path(d)
        res = ChangeResult(True, "changed", new_content=b"<html>x</html>",
                           new_hash="H1", new_chunks=["chunk one here yes"])
        save_content_snapshot(snap, "page_hash", res)
        assert save_baseline_snapshot(snap, "page_hash", "H1") is True
        assert _read_prev_hash(snap, "page_hash") == "H1"      # baseline 전진
        assert _read_prev_chunks(snap) == ["chunk one here yes"]  # 승격됨
        assert not (snap / "pending_chunks.json").exists()


def test_compat_save_snapshot_writes_hash():
    for method in SNAPSHOT_FILES:
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d)
            save_snapshot(snap, method, ChangeResult(True, "x", None, "HX", "u"))
            assert _read_prev_hash(snap, method) == "HX", method


def test_baseline_noop_without_hash():
    with tempfile.TemporaryDirectory() as d:
        assert save_baseline_snapshot(Path(d), "page_hash", None) is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print("PASS " + fn.__name__)
    print("\n%d passed" % len(fns))
