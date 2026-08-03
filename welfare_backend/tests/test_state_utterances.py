"""상태 발화 회귀 테스트셋 (#226 #227 #232).

'배고파 죽겠어' 처럼 정책을 직접 묻지 않고 겪는 어려움만 말하는 발화는
잡담이 아니라 정책 질의다. 이 테스트는 두 가지를 고정한다.

  1) 어떤 상태 발화도 적재 제외 화이트리스트에 걸리지 않는다.
  2) 상태 발화를 도구 호출 없이 답한 turn 은 반드시 미답변 질의로 남는다.

확장 질의의 '품질'은 외부 모델에 의존하므로 여기서 검증하지 않는다.
여기서 막는 것은 발화가 관측 대상에서 통째로 사라지는 회귀다.
"""
import json
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from models import FallbackReason                                  # noqa: E402
from unresolved_logger import classify_fallback, is_excluded_utterance  # noqa: E402

_FIXTURE = Path(__file__).parent / "fixtures" / "state_utterances.json"
CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_fixture_is_not_empty():
    assert len(CASES) >= 30, "상태 발화 표본이 30개 미만이면 회귀 감지력이 떨어진다"


@pytest.mark.parametrize("case", CASES, ids=[c["utterance"] for c in CASES])
def test_state_utterance_is_not_whitelisted(case):
    assert is_excluded_utterance(case["utterance"]) is False


@pytest.mark.parametrize("case", CASES, ids=[c["utterance"] for c in CASES])
def test_state_utterance_without_tool_is_logged(case):
    assert classify_fallback(
        has_grounding=False,
        tool_steps=[],
        user_text=case["utterance"],
        ai_text="그러셨군요. 많이 힘드셨겠어요.",
    ) is FallbackReason.NO_TOOL_CALL


def test_need_areas_are_covered():
    """한 영역에만 쏠린 표본이면 회귀 감지 범위가 좁아진다."""
    areas = {c["need_area"] for c in CASES}
    assert len(areas) >= 8, f"영역이 {len(areas)}종뿐: {sorted(areas)}"
