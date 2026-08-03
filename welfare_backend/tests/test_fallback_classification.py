"""폴백 사유 분류 회귀 테스트 (#227 #228 #232).

핵심 회귀 대상: '배고파 죽겠어' 같은 상태 발화를 상담원이 도구 없이 답했을 때
미답변 질의로 남아야 한다. 예전에는 도구 호출이 없으면 무조건 잡담으로 간주해
적재하지 않았고, 그 결과 이 실패 유형이 발굴 대상에서 통째로 빠져 있었다.
"""
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1]
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from models import FallbackReason                      # noqa: E402
from schemas import ToolStep                           # noqa: E402
from unresolved_logger import (                        # noqa: E402
    classify_fallback,
    is_excluded_utterance,
    is_explicit_no_info,
    scrub_pii,
)


def _step(name="search_by_keyword", count=3, error=None):
    return ToolStep(name=name, args={}, top_sim=None, result_count=count, error=error)


# ── 화이트리스트 ─────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "안녕하세요", "안녕", "반갑습니다", "처음 뵙겠습니다",
    "고마워", "고맙습니다", "감사합니다",
    "잘 있어", "안녕히 계세요", "수고하세요", "이만 끊을게",
    "[SYSTEM:GREETING]", "[SYSTEM:IDLE_CHECK]",
    "", "   ", None,
])
def test_excluded_utterances(text):
    assert is_excluded_utterance(text) is True


@pytest.mark.parametrize("text", [
    "배고파 죽겠어",
    "돈이 하나도 없어",
    "집이 너무 추워",
    "병원 가기가 힘들어",
    "지하철 무료로 탈 수 있어?",
    "네 그럼 신청은 어떻게 해요",
])
def test_not_excluded_utterances(text):
    assert is_excluded_utterance(text) is False


# ── 도구 미호출 경로 (#227) ──────────────────────────────────
def test_state_utterance_without_tool_is_logged():
    """상태 발화를 도구 없이 답하면 NO_TOOL_CALL 로 적재된다."""
    assert classify_fallback(
        has_grounding=False, tool_steps=[],
        user_text="배고파 죽겠어",
        ai_text="식사를 잘 챙기시는 게 중요해요. 끼니 거르지 마세요.",
    ) is FallbackReason.NO_TOOL_CALL


def test_greeting_without_tool_is_not_logged():
    assert classify_fallback(
        has_grounding=False, tool_steps=[],
        user_text="안녕하세요", ai_text="안녕하세요! 무엇을 도와드릴까요?",
    ) is None


def test_system_signal_is_not_logged():
    assert classify_fallback(
        has_grounding=False, tool_steps=[],
        user_text="[SYSTEM:GREETING]", ai_text="안녕하세요!",
    ) is None


# ── 정보 없음 판정 (#228) ────────────────────────────────────
@pytest.mark.parametrize("text", [
    "현재 제 정보로는 정확히 안내드리기 어렵습니다.",
    "죄송하지만 안내해 드리기 어렵습니다.",
    "정확한 정보를 찾지 못했습니다.",
    "관련 정보가 없습니다.",
    "해당 자료를 확인할 수 없습니다.",
])
def test_explicit_no_info_detected(text):
    assert is_explicit_no_info(text) is True


@pytest.mark.parametrize("text", [
    "월 만 육천원이 지원됩니다.",
    "주민센터에서 신청하실 수 있어요.",
    "네, 해당되십니다.",
])
def test_explicit_no_info_not_triggered(text):
    assert is_explicit_no_info(text) is False


def test_tool_returned_results_but_answer_says_no_info():
    """도구가 결과를 줬는데도 모른다고 답하면 EXPLICIT_NO_INFO."""
    assert classify_fallback(
        has_grounding=False, tool_steps=[_step(count=3)],
        user_text="보청기 지원 되나요",
        ai_text="현재 제 정보로는 정확히 안내드리기 어렵습니다.",
    ) is FallbackReason.EXPLICIT_NO_INFO


# ── 기존 분기 회귀 ───────────────────────────────────────────
def test_grounding_wins():
    assert classify_fallback(
        has_grounding=True, tool_steps=[_step()],
        user_text="질문", ai_text="답변",
    ) is FallbackReason.GOOGLE_SEARCH


def test_tool_error():
    assert classify_fallback(
        has_grounding=False, tool_steps=[_step(error="boom")],
        user_text="질문", ai_text="답변",
    ) is FallbackReason.TOOL_ERROR


def test_empty_result():
    assert classify_fallback(
        has_grounding=False, tool_steps=[_step(count=0)],
        user_text="질문", ai_text="답변",
    ) is FallbackReason.EMPTY_RESULT


def test_normal_answer_not_logged():
    assert classify_fallback(
        has_grounding=False, tool_steps=[_step(count=2)],
        user_text="지하철 무료인가요", ai_text="네, 무임승차가 가능합니다.",
    ) is None


# ── PII 스크러빙 회귀 (기존 동작 유지) ───────────────────────
def test_scrub_pii_keeps_working():
    out = scrub_pii("연락처는 010-1234-5678 이고 문의는 1577-1000 입니다")
    assert "010-1234-5678" not in out
    assert "[PHONE]" in out
