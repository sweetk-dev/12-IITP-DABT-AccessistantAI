# -*- coding: utf-8 -*-
"""설치 버전 고정 파일(constraints.txt) 점검 (v2.27.1). 외부 호출 없음."""
import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> dict:
    out = {}
    for line in (_APP / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, ver = line.partition("==")
        assert ver, "버전이 고정되지 않은 줄: %s" % line
        out[_norm(name)] = ver
    return out


def _required(fname: str) -> list:
    names = []
    for line in (_APP / fname).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        names.append(_norm(re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0]))
    return names


def test_every_requirement_is_pinned():
    pins = _pins()
    missing = [n for f in ("requirements.txt", "requirements-fallback.txt") for n in _required(f) if n not in pins]
    assert missing == [], "constraints.txt 에 버전이 없는 패키지: %s" % missing


def test_pins_satisfy_declared_bounds():
    """고정 버전이 requirements 의 상한을 어기지 않는다 — google-genai 는 3 미만."""
    major = int(_pins()["google-genai"].split(".")[0])
    assert major < 3


def test_dockerfile_installs_with_constraints():
    df = (_APP / "Dockerfile").read_text(encoding="utf-8")
    installs = [l for l in df.splitlines() if l.strip().startswith("RUN pip install") and "-r requirements" in l]
    assert installs and all("-c constraints.txt" in l for l in installs), installs
