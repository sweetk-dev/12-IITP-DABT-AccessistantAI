# unresolved_logger.py
# Phase 5 Track A — 미해결 질의 적재 헬퍼.
#
# 책임:
#   - 한 Gemini Live turn 의 상태 누적 (사용자 발화, AI 응답, 도구 호출, grounding)
#   - PII 스크러빙 (전화/주민/카드번호 정규식)
#   - 폴백 사유 분류 (GOOGLE_SEARCH > TOOL_ERROR > EMPTY_RESULT)
#   - 비동기 INSERT (fire-and-forget — 응답 지연 0)
#
# 설계 원칙:
#   - live_bridge.py 의 hot path 를 가능한 한 짧게 (5~6 곳의 짧은 호출만 추가)
#   - DB 적재 실패가 음성 대화를 끊으면 안 됨 — 모든 예외는 logger.warning 으로 흡수
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.ext.asyncio import async_sessionmaker

from models import UnresolvedQuery, FallbackReason
from schemas import ToolStep, ToolChain

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# PII 스크러빙 (고정 패턴만 — 이름/주소는 분석 손실 위험으로 미적용)
# ─────────────────────────────────────────────────────────────
# ⚠️ 적용 순서 중요: 긴 패턴 → 짧은 패턴 (긴 게 먼저 매칭돼야 부분 침범 방지)
_PII_PATTERNS = [
    # 카드번호 (가장 긴 4-4-4-4) — 먼저 매칭해야 1577-1000 패턴이 부분 침범 안 함
    (re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{4}\b"), "[CARD]"),
    # 주민등록번호: 901231-1234567
    (re.compile(r"\b\d{6}-[1-4]\d{6}\b"), "[RRN]"),
    # 일반 전화/휴대전화: 02-1234-5678 / 010-1234-5678
    (re.compile(r"\b\d{2,3}-\d{3,4}-\d{4}\b"), "[PHONE]"),
    # 대표번호: 1577-1000 (1로 시작하는 4자리 + 4자리)
    (re.compile(r"\b1\d{3}-\d{4}\b"), "[PHONE]"),
    # 이메일 (정책 답변에 거의 안 나오지만 안전망)
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
]


def scrub_pii(text: Optional[str]) -> Optional[str]:
    """고정 패턴 PII 만 마스킹. 이름·주소는 분석 손실 위험으로 보존."""
    if not text:
        return text
    for pat, repl in _PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


# ─────────────────────────────────────────────────────────────
# 폴백 분류
# ─────────────────────────────────────────────────────────────
# 적재 제외 화이트리스트 — 상담 실패로 볼 수 없는 발화만 좁게 제외한다.
# ⚠️ 여기 없는 발화는 전부 적재한다. "도구를 안 불렀으니 잡담" 이라는 역추론은 금지:
#    '배고파 죽겠어' 같은 상태 발화가 잡담으로 오분류되어 도구 없이 답해도
#    그 사실 자체가 미답변 질의로 남아야 발굴 대상이 된다.
_SYSTEM_SIGNAL = re.compile(r"^\s*\[SYSTEM:")            # 백엔드가 보낸 시스템 신호

_SMALLTALK_PATTERNS = [
    re.compile(r"^(안녕|반가|반갑|처음 ?뵙|하이|헬로|여보세요)"),  # 인사
    re.compile(r"(고마워|고맙습|감사합|감사해|감사드)"),        # 감사
    re.compile(r"(잘 ?있어|안녕히|수고하|그만할|이만 ?끊|종료할)"),  # 작별
]

# 질문 표지 — 인사말에 이런 표지가 섞여 있으면 실제 문의로 보고 제외하지 않는다.
_QUESTION_MARKERS = re.compile(
    r"[?？]|알려|어떻|어디|언제|얼마|무엇|뭐|신청|방법|되나|될까|가능|받을")

# 인사·감사·작별로 보고 제외할 수 있는 최대 길이. 이보다 길면 인사말 뒤에
# 다른 내용이 붙어 있을 가능성이 높으므로 제외하지 않는다(과잉 적재가 과소 관측보다 낫다).
_SMALLTALK_MAX_LEN = 20

# 상담원이 스스로 "모른다"고 답한 문구 — SYSTEM_INSTRUCTION 이 지시하는 표현과 그 변형.
# 도구를 호출해 결과를 받고도 안내에 실패한 turn 을 잡기 위한 판정이다.
_NO_INFO_PATTERNS = [
    re.compile(r"안내(해 ?)?드리기(가)? ?어렵"),
    re.compile(r"정확(히|한)[^.]{0,12}(안내|답변)[^.]{0,8}(어렵|힘들)"),
    re.compile(r"정확한 정보를 찾지 ?못"),
    re.compile(r"(정보|자료)(가|를)[^.]{0,10}(없습|없어|없네|없다|찾을 수 없|확인할 수 없|확인되지 않)"),
    re.compile(r"제 정보로는"),
]


def _matches_any(text: Optional[str], patterns) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in patterns)


def is_excluded_utterance(user_text: Optional[str]) -> bool:
    """적재 제외 대상(인사·감사·작별·시스템 신호·빈 발화) 여부.

    좁게 판정한다 — "안녕하세요 지하철 할인 알려주세요" 처럼 인사 뒤에
    질문이 이어지는 발화를 여기서 삼키면 #227 이 고치려던 문제가 재발한다.
    인사·감사·작별은 (짧고) 그리고 (질문 표지가 없는) 발화만 제외한다.
    """
    if not user_text or not user_text.strip():
        return True
    t = user_text.strip()
    if _SYSTEM_SIGNAL.search(t):
        return True
    if len(t) > _SMALLTALK_MAX_LEN or _QUESTION_MARKERS.search(t):
        return False
    return _matches_any(t, _SMALLTALK_PATTERNS)


def is_explicit_no_info(ai_text: Optional[str]) -> bool:
    """상담원이 명시적으로 '정보 없음'을 답한 응답인지."""
    return _matches_any(ai_text, _NO_INFO_PATTERNS)


def classify_fallback(
    *,
    has_grounding: bool,
    tool_steps: list[ToolStep],
    user_text: Optional[str] = None,
    ai_text: Optional[str] = None,
) -> Optional[FallbackReason]:
    """폴백 사유 판정. None 반환 시 적재하지 않음 (정상 응답).

    우선순위 (강한 시그널 우선):
      1) grounding_metadata 수신          → GOOGLE_SEARCH
      2) 도구 호출 중 error 발생          → TOOL_ERROR
      3) 모든 도구 결과가 empty           → EMPTY_RESULT
      4) 상담원이 '정보 없음'으로 답함     → EXPLICIT_NO_INFO
      5) 도구를 아예 호출하지 않음         → NO_TOOL_CALL (화이트리스트 제외)
      6) 그 외                            → None (적재 안 함)
    """
    if has_grounding:
        return FallbackReason.GOOGLE_SEARCH

    if tool_steps:
        if any(s.error for s in tool_steps):
            return FallbackReason.TOOL_ERROR
        if all(s.result_count == 0 for s in tool_steps):
            return FallbackReason.EMPTY_RESULT
        # 도구가 결과를 돌려줬는데도 안내에 실패한 경우
        if is_explicit_no_info(ai_text):
            return FallbackReason.EXPLICIT_NO_INFO
        return None

    # ── 도구 미호출 경로 ──
    if is_excluded_utterance(user_text):
        return None
    if is_explicit_no_info(ai_text):
        return FallbackReason.EXPLICIT_NO_INFO
    return FallbackReason.NO_TOOL_CALL


def estimate_result_count(result: Any) -> int:
    """도구 응답 dict 에서 결과 개수 추정. 도구마다 응답 모양이 달라 보수적으로."""
    if not isinstance(result, dict):
        return 0
    if "error" in result:
        return 0
    # 일반적인 키들 시도
    for key in ("results", "items", "policies", "agencies", "matches", "data"):
        v = result.get(key)
        if isinstance(v, list):
            return len(v)
    # policy_id 단건 응답
    if "policy_id" in result or "id" in result:
        return 1
    return 0  # 모르면 보수적으로 0 (false positive empty_result 가능 — 향후 도구 응답 표준화 시 개선)


# ─────────────────────────────────────────────────────────────
# Turn 상태 누적기
# ─────────────────────────────────────────────────────────────
@dataclass
class TurnTracker:
    """한 Gemini Live turn 의 상태 누적기.

    live_bridge.py 의 pump_gemini_to_client 루프에서 각 이벤트마다 on_* 호출.
    turn_complete 시 finalize_turn() 으로 비동기 적재.
    """
    session_id: uuid.UUID
    session_factory: async_sessionmaker

    user_text_parts: list[str] = field(default_factory=list)
    ai_text_parts: list[str] = field(default_factory=list)
    tool_steps: list[ToolStep] = field(default_factory=list)
    grounding_info: Optional[dict] = None
    asr_raw_parts: list[dict] = field(default_factory=list)

    def reset(self) -> None:
        """다음 turn 을 위해 상태 초기화."""
        self.user_text_parts.clear()
        self.ai_text_parts.clear()
        self.tool_steps.clear()
        self.grounding_info = None
        self.asr_raw_parts.clear()

    # ─ 이벤트 hook ─────────────────────────────────────────
    def on_user_transcript(self, text: Optional[str], raw: Any = None) -> None:
        if text:
            self.user_text_parts.append(text)
        if raw is not None:
            # SDK 객체를 dict 로 가능한 한 변환 (디버깅용 — 첫 적재 시 모양 확인 목적)
            try:
                if hasattr(raw, "model_dump"):
                    self.asr_raw_parts.append(raw.model_dump(mode="json"))
                elif hasattr(raw, "to_dict"):
                    self.asr_raw_parts.append(raw.to_dict())
                else:
                    self.asr_raw_parts.append({"text": text, "_type": type(raw).__name__})
            except Exception:
                self.asr_raw_parts.append({"text": text})

    def on_ai_transcript(self, text: Optional[str]) -> None:
        if text:
            self.ai_text_parts.append(text)

    def on_tool_call(self, name: str, args: dict, result: Any) -> None:
        step = ToolStep(
            name=name,
            args=dict(args or {}),
            top_sim=_extract_top_sim(result),
            result_count=estimate_result_count(result),
            error=(result.get("error") if isinstance(result, dict) and "error" in result else None),
        )
        self.tool_steps.append(step)

    def on_grounding(self, gm: Any) -> None:
        try:
            if hasattr(gm, "model_dump"):
                self.grounding_info = gm.model_dump(mode="json")
            elif hasattr(gm, "to_dict"):
                self.grounding_info = gm.to_dict()
            else:
                self.grounding_info = {"raw": str(gm)[:1000]}
        except Exception:
            self.grounding_info = {"raw": str(gm)[:1000]}

    # ─ turn 완료 시 호출 ───────────────────────────────────
    async def finalize_turn(self) -> None:
        """폴백 판정 후 비동기 INSERT (fire-and-forget). 실패는 무해 흡수."""
        try:
            user_query_raw = " ".join(self.user_text_parts).strip()
            ai_final_raw = " ".join(self.ai_text_parts).strip()

            reason = classify_fallback(
                has_grounding=self.grounding_info is not None,
                tool_steps=self.tool_steps,
                user_text=user_query_raw,
                ai_text=ai_final_raw,
            )
            if reason is None:
                # 폴백 아님 — 적재 안 함
                return

            if not user_query_raw:
                # 사용자 발화 텍스트 없으면(텍스트 모드 등) 적재 의미 작음 — skip
                return

            user_query = scrub_pii(user_query_raw) or "(empty)"
            ai_final = scrub_pii(ai_final_raw) or None

            row = UnresolvedQuery(
                session_id=self.session_id,
                intent_group_id=uuid.uuid4(),   # 사후 클러스터링으로 묶음 — 적재 시점은 새 그룹
                turn_in_group=0,
                user_query=user_query,
                tool_chain=ToolChain(steps=self.tool_steps).to_json(),
                fallback_reason=reason,
                ai_final_answer=ai_final,
                grounding_info=self.grounding_info,
                asr_raw=self.asr_raw_parts or None,
            )
            async with self.session_factory() as ses:
                ses.add(row)
                await ses.commit()
                logger.info("📥 UnresolvedQuery 적재: id=%s reason=%s query=%r",
                            row.id, reason.value, user_query[:60])
        except Exception as e:
            # 절대 hot path 를 깨지 않도록 흡수
            logger.warning("UnresolvedQuery 적재 실패 (무시): %s", e)


# ─────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────
def _extract_top_sim(result: Any) -> Optional[float]:
    """도구 응답에서 최고 유사도 점수 추출 — 도구 응답 표준화 전까지는 best-effort."""
    if not isinstance(result, dict):
        return None
    # 흔한 키 후보들
    for key in ("top_similarity", "top_sim", "best_score", "max_score"):
        v = result.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    # results 배열 안에 score 가 있는 경우
    for key in ("results", "items", "matches"):
        arr = result.get(key)
        if isinstance(arr, list) and arr:
            first = arr[0]
            if isinstance(first, dict):
                for sk in ("similarity", "score", "distance"):
                    v = first.get(sk)
                    if isinstance(v, (int, float)):
                        return float(v)
    return None


