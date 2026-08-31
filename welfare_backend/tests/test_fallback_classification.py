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
    TurnTracker,
    classify_fallback,
    is_excluded_utterance,
    is_explicit_no_info,
    is_nav_chatter,
    mask_coords,
    scrub_pii,
    scrub_structure,
)


def _step(name="search_by_keyword", count=3, error=None,
          status=None, has_result_key=None):
    if has_result_key is None:
        has_result_key = status is None      # 정책 도구는 결과 배열을 돌려준다
    return ToolStep(name=name, args={}, top_sim=None, result_count=count,
                    error=error, status=status, has_result_key=has_result_key)


def _tracked(name, result):
    """실제 on_tool_call 경로를 태워 ToolStep 을 만든다 (판정 근거 필드 포함)."""
    t = TurnTracker(session_id=None, session_factory=None)
    t.on_tool_call(name, {}, result)
    return t.tool_steps[0]


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


# ── 화이트리스트 과잉 제외 회귀 (세션 리뷰에서 발견) ────────
# 인사 뒤에 실제 질문이 이어지는 발화가 제외되면, 도구 미호출 실패가
# 관측 밖으로 사라진다 — #227 이 고치려던 문제의 재발 경로.
@pytest.mark.parametrize("text", [
    "안녕하세요 지하철 요금 할인 좀 알려주세요",
    "고마워 그런데 장애인연금 신청은 어떻게 해?",
    "감사합니다 활동지원 신청 방법도 알려주세요",
    "수고하세요 아 맞다 보청기 지원 되나요?",
])
def test_greeting_with_question_is_not_excluded(text):
    assert is_excluded_utterance(text) is False


def test_greeting_with_question_without_tool_is_logged():
    assert classify_fallback(
        has_grounding=False, tool_steps=[],
        user_text="안녕하세요 지하철 요금 할인 좀 알려주세요",
        ai_text="네 안녕하세요! 좋은 하루 보내세요.",
    ) is FallbackReason.NO_TOOL_CALL


# ══════════════════════════════════════════════════════════════
# #253 — 경로 안내 도메인 오적재 차단 + 개인정보 보존
# ══════════════════════════════════════════════════════════════

# ── 경로 도구 성공 턴은 적재되지 않는다 ─────────────────────
_ROUTE_OK = {
    "status": "success", "tool_name": "plan_accessible_route",
    "mode_used": "walk", "transit": [], "route_id": "R-1",
    "summary": {"distance_m": 820, "duration_min": 12},
    "warnings": [], "first_steps": ["직진하세요"],
}
_SEGMENT_OK = {
    "status": "success", "tool_name": "explain_route_segment",
    "segments": [{"idx": 3, "instruction": "우회전"}],
}
_GUIDANCE_OK = {
    "status": "guiding", "tool_name": "get_current_guidance",
    "route_id": "R-1", "step_no": 4, "total_steps": 11,
    "current_instruction": "횡단보도를 건너세요",
}
_NAVI_OK = {"status": "success", "tool_name": "open_navi_screen"}


@pytest.mark.parametrize("name,result", [
    ("plan_accessible_route", _ROUTE_OK),
    ("explain_route_segment", _SEGMENT_OK),
    ("get_current_guidance", _GUIDANCE_OK),
    ("open_navi_screen", _NAVI_OK),
    ("get_current_guidance", {"status": "idle", "route_id": None}),
    ("get_current_guidance", {"status": "arrived", "route_id": "R-1"}),
])
def test_route_tool_success_is_not_logged(name, result):
    """경로 안내가 정상 동작한 turn 은 미답변 질의가 아니다.

    회귀 대상: 응답에 results/items 키가 없다는 이유만으로 결과 0건으로
    집계되어 empty_result 로 적재되던 문제.
    """
    assert classify_fallback(
        has_grounding=False, tool_steps=[_tracked(name, result)],
        user_text="안양역에서 평촌역까지 어떻게 가요",
        ai_text="총 820미터, 약 12분 걸립니다.",
    ) is None


# ── status=success 인데 결과가 0건이면 빈 결과다 ────────────
@pytest.mark.parametrize("name,result", [
    ("find_nearby_transit",
     {"status": "success", "tool_name": "find_nearby_transit", "count": 0, "items": []}),
    ("find_bf_tour_spots",
     {"status": "success", "tool_name": "find_bf_tour_spots", "count": 0, "results": []}),
])
def test_success_with_empty_list_is_empty_result(name, result):
    """status 만 보고 성공 처리하면 진짜 빈 결과를 놓친다(False Negative).

    두 도구는 결과가 없어도 success 를 반환하므로 배열 길이로 판정해야 한다.
    """
    assert classify_fallback(
        has_grounding=False, tool_steps=[_tracked(name, result)],
        user_text="근처 정류장 알려줘", ai_text="주변에 정류장이 없네요.",
    ) is FallbackReason.EMPTY_RESULT


def test_success_with_nonempty_list_is_not_logged():
    assert classify_fallback(
        has_grounding=False, tool_steps=[_tracked("find_nearby_transit", {
            "status": "success", "count": 2,
            "items": [{"name": "안양역"}, {"name": "안양1번가"}]})],
        user_text="근처 정류장 알려줘", ai_text="안양역이 가깝습니다.",
    ) is None


# ── 경로 API 장애는 tool_error 로 승격된다 ──────────────────
def test_route_client_error_shape_is_tool_error():
    """route_client._err() 는 error 키 없이 status=error 로만 알린다.

    이걸 놓치면 02-Route API 장애가 empty_result 로 뭉개져 관측되지 않는다.
    """
    step = _tracked("plan_accessible_route", {
        "status": "error", "message": "경로 서버에 연결하지 못했습니다", "detail": "timeout"})
    assert step.error == "경로 서버에 연결하지 못했습니다"
    assert classify_fallback(
        has_grounding=False, tool_steps=[step],
        user_text="평촌역까지 길 안내해줘", ai_text="지금은 경로를 안내하기 어렵습니다.",
    ) is FallbackReason.TOOL_ERROR


# ── 범위 밖 / 입력 부족은 정책 공백과 구분된다 ──────────────
def test_out_of_service_area_reason():
    assert classify_fallback(
        has_grounding=False, tool_steps=[_tracked("plan_accessible_route", {
            "status": "out_of_service_area", "tool_name": "plan_accessible_route",
            "service_area": "안양시", "message": "안내 범위 밖입니다"})],
        user_text="부산역까지 어떻게 가요", ai_text="경로 안내는 안양시 안에서만 가능합니다.",
    ) is FallbackReason.OUT_OF_SERVICE_AREA


@pytest.mark.parametrize("status", ["need_location", "need_destination"])
def test_needs_input_reason(status):
    assert classify_fallback(
        has_grounding=False, tool_steps=[_tracked("plan_accessible_route", {
            "status": status, "tool_name": "plan_accessible_route"})],
        user_text="길 안내해줘", ai_text="현재 위치를 알 수 없어요.",
    ) is FallbackReason.NEEDS_INPUT


# ── 길안내 중 수긍·추임새 (guiding=True 일 때만 제외) ───────
@pytest.mark.parametrize("text", [
    "네", "응", "알겠어", "오케이", "다음은?", "다음", "계속",
    "잠깐만", "취소해줘", "어", "음", "아",
])
def test_nav_chatter_detected(text):
    assert is_nav_chatter(text) is True


@pytest.mark.parametrize("text", [
    "여기서 지하철 요금 할인 받을 수 있어?",
    "장애인 콜택시는 어떻게 신청해",
    "다음 달에 활동지원 신청하려는데 서류가 뭐가 필요해요",
])
def test_nav_chatter_not_matched_for_real_questions(text):
    assert is_nav_chatter(text) is False


def test_nav_chatter_excluded_only_while_guiding():
    """같은 발화라도 안내 중이면 제외, 정책 상담 화면이면 기존대로 적재."""
    kw = dict(has_grounding=False, tool_steps=[],
              user_text="다음은?", ai_text="횡단보도를 건너시면 됩니다.")
    assert classify_fallback(guiding=True, **kw) is None
    assert classify_fallback(guiding=False, **kw) is FallbackReason.NO_TOOL_CALL


def test_policy_question_while_guiding_is_still_logged():
    """이동 화면에서 물어본 정책 질문까지 삼키면 안 된다."""
    assert classify_fallback(
        has_grounding=False, tool_steps=[], guiding=True,
        user_text="지하철 요금 할인 받을 수 있어?",
        ai_text="네, 좋은 하루 보내세요.",
    ) is FallbackReason.NO_TOOL_CALL


# ── 개인정보: 좌표 절사 + 구조 스크러빙 ─────────────────────
def test_mask_coords_truncates_precision():
    out = mask_coords({"origin_lat": 37.4016302, "origin_lng": 126.9228826,
                       "profile": "wheelchair_manual"})
    assert out["origin_lat"] == 37.40
    assert out["origin_lng"] == 126.92
    assert out["profile"] == "wheelchair_manual"


def test_mask_coords_ignores_non_numeric():
    out = mask_coords({"lat": None, "lng": "", "place": "안양역"})
    assert out == {"lat": None, "lng": "", "place": "안양역"}


def test_tool_call_args_are_masked():
    t = TurnTracker(session_id=None, session_factory=None)
    t.on_tool_call("plan_accessible_route",
                   {"origin_lat": 37.4016302, "origin_lng": 126.9228826},
                   _ROUTE_OK)
    assert t.tool_steps[0].args["origin_lat"] == 37.40


def test_scrub_structure_reaches_nested_strings():
    """asr_raw / tool_chain 안의 원문도 마스킹돼야 스크러빙이 무력화되지 않는다."""
    out = scrub_structure([{"text": "제 번호는 010-1234-5678 이에요",
                            "nested": {"detail": ["문의 1577-1000"]}}])
    assert "010-1234-5678" not in str(out)
    assert "1577-1000" not in str(out)
    assert "[PHONE]" in str(out)


def test_scrub_structure_preserves_non_strings():
    out = scrub_structure({"count": 3, "ok": True, "score": 0.82, "none": None})
    assert out == {"count": 3, "ok": True, "score": 0.82, "none": None}
