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


def scrub_structure(obj: Any, _depth: int = 0) -> Any:
    """dict/list 안의 모든 문자열에 재귀적으로 scrub_pii 를 적용한다.

    user_query·ai_final_answer 만 마스킹하면 같은 행의 tool_chain·asr_raw 에
    원문이 그대로 남아 마스킹이 무력화된다. 저장 직전 한 번 통과시킨다.
    """
    if _depth > 8:                      # grounding_info 처럼 깊은 구조 방어
        return obj
    if isinstance(obj, str):
        return scrub_pii(obj)
    if isinstance(obj, dict):
        return {k: scrub_structure(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_structure(v, _depth + 1) for v in obj]
    return obj


# ─────────────────────────────────────────────────────────────
# 위치정보 마스킹
# ─────────────────────────────────────────────────────────────
# 경로 도구 인자에는 프런트가 보낸 실제 현재 위치가 주입된다(live_bridge).
# 그대로 tool_chain 에 남으면 이용자의 거주지·이동 동선이 특정되므로,
# 분석에 필요한 최소 해상도(소수점 2자리 ≒ 1.1km 격자)로 절사해 저장한다.
_COORD_KEYS = ("lat", "lng", "latitude", "longitude",
               "origin_lat", "origin_lng", "dest_lat", "dest_lng")
_COORD_PRECISION = 2


def mask_coords(args: Optional[dict]) -> dict:
    """도구 인자 dict 의 좌표 값을 격자 수준으로 절사한 사본을 돌려준다."""
    out = dict(args or {})
    for k in _COORD_KEYS:
        v = out.get(k)
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            out[k] = round(float(v), _COORD_PRECISION)
    return out


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


# 길안내가 **진행 중일 때만** 추가로 제외할 발화.
# 정책 상담 화면에는 적용하지 않는다 — #227 의 "과잉 적재가 과소 관측보다 낫다"
# 원칙은 정책 도메인에서 그대로 유지해야 한다.
_NAV_CHATTER_MAX_LEN = 25
_NAV_CHATTER_PATTERNS = [
    re.compile(r"^(네|넵|예|응|어|음|아|오케이|오키|그래|맞아|알겠|알았|좋아)"),  # 수긍
    re.compile(r"^(다음|계속|그다음|그 ?다음|다시|한번 ?더)"),                    # 진행 지시
    re.compile(r"^(잠깐|잠시|멈춰|정지|취소|그만|끝|종료)"),                      # 중단 지시
    re.compile(r"^[아어음으흠에헤]+$"),                                            # 비언어 추임새
]


def is_nav_chatter(user_text: Optional[str]) -> bool:
    """길안내 진행 중 나오는 수긍·진행 지시·추임새인지.

    이동 중에는 마이크가 계속 열려 있어 동행자 대화나 주변 소음이 짧은 조각으로
    전사되기도 한다. 짧고(25자 이하) 위 패턴으로 시작하는 발화만 좁게 제외한다.
    """
    if not user_text:
        return True
    t = user_text.strip()
    if not t:
        return True
    if len(t) > _NAV_CHATTER_MAX_LEN:
        return False
    return any(p.search(t) for p in _NAV_CHATTER_PATTERNS)


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


def _step_verdict(step: ToolStep) -> str:
    """도구 1회 호출의 결과 판정.

    반환: ok | error | empty | needs_input | out_of_scope | unknown

    판정 순서가 중요하다.
      ① 오류가 최우선.
      ② **결과 배열 키가 있는데 0건이면 빈 결과** — status 가 success 여도 그렇다.
         find_nearby_transit / find_bf_tour_spots 는 결과가 없어도 success 를
         반환하므로, status 만 보고 성공 처리하면 진짜 빈 결과를 놓친다.
      ③ 도구가 밝힌 status.
      ④ status 가 없는 도구(정책 5종)는 기존 개수 추정으로 폴백.
    """
    if step.error or step.status == "error" or step.status in _TRANSIENT_STATUSES:
        return "error"
    if step.has_result_key and step.result_count == 0:
        return "empty"
    if step.status in _OK_STATUSES:
        return "ok"
    if step.status in _NEEDS_INPUT_STATUSES:
        return "needs_input"
    if step.status in _OUT_OF_SCOPE_STATUSES:
        return "out_of_scope"
    if step.status is None:
        return "ok" if step.result_count > 0 else "empty"
    return "unknown"


def classify_fallback(
    *,
    has_grounding: bool,
    tool_steps: list[ToolStep],
    user_text: Optional[str] = None,
    ai_text: Optional[str] = None,
    guiding: bool = False,
) -> Optional[FallbackReason]:
    """폴백 사유 판정. None 반환 시 적재하지 않음 (정상 응답).

    우선순위 (강한 시그널 우선):
      1) grounding_metadata 수신          → GOOGLE_SEARCH
      2) 도구 호출 중 error 발생          → TOOL_ERROR
      3) 도구가 결과를 돌려줌              → EXPLICIT_NO_INFO 또는 None
      4) 모든 도구 결과가 empty           → EMPTY_RESULT
      5) 서비스 범위 밖 / 위치·목적지 미확보 → OUT_OF_SERVICE_AREA / NEEDS_INPUT
      6) 도구를 아예 호출하지 않음         → NO_TOOL_CALL (화이트리스트 제외)
      7) 그 외                            → None (적재 안 함)

    guiding=True(길안내 진행 중)이면 도구 미호출 경로에서 수긍·진행 지시·추임새를
    추가로 제외한다. 정책 상담 화면(guiding=False)의 동작은 바뀌지 않는다.
    """
    if has_grounding:
        return FallbackReason.GOOGLE_SEARCH

    if tool_steps:
        verdicts = [_step_verdict(s) for s in tool_steps]
        if "error" in verdicts:
            return FallbackReason.TOOL_ERROR
        if "ok" in verdicts:
            # 도구가 결과를 돌려줬는데도 안내에 실패한 경우
            if is_explicit_no_info(ai_text):
                return FallbackReason.EXPLICIT_NO_INFO
            return None
        if "empty" in verdicts:
            return FallbackReason.EMPTY_RESULT
        # 결과를 못 준 이유가 정책 공백이 아니라 범위·입력 부족인 경우.
        # 발굴 대상에서는 빠지되(discovery_core) 커버리지 지표로는 남긴다.
        if "out_of_scope" in verdicts:
            return FallbackReason.OUT_OF_SERVICE_AREA
        if "needs_input" in verdicts:
            return FallbackReason.NEEDS_INPUT
        if is_explicit_no_info(ai_text):
            return FallbackReason.EXPLICIT_NO_INFO
        return FallbackReason.UNKNOWN

    # ── 도구 미호출 경로 ──
    if is_excluded_utterance(user_text):
        return None
    if guiding and is_nav_chatter(user_text):
        return None
    if is_explicit_no_info(ai_text):
        return FallbackReason.EXPLICIT_NO_INFO
    return FallbackReason.NO_TOOL_CALL


# 도구가 status 로 스스로 밝히는 결과 상태 — _step_verdict 가 참조.
_OK_STATUSES = frozenset({"success", "guiding", "idle", "arrived"})
# 입력·해석 부족 — 장소 이름을 못 찾은 것(place_not_found, v1.38.0)·역 이름 없음·주변 정류장 없음도
# 정책 공백이 아니라 입력 문제다. 판정표에 없으면 unknown 으로 떨어져 사유가 보이지 않는다(v1.41.0).
_NEEDS_INPUT_STATUSES = frozenset({"need_location", "need_destination", "place_not_found",
                                   "need_station", "station_not_found", "no_stop_nearby"})
_OUT_OF_SCOPE_STATUSES = frozenset({"out_of_service_area"})
# 외부 실시간 소스 장애(GBIS 도착정보 등) — 도구 오류로 관측한다
_TRANSIENT_STATUSES = frozenset({"unavailable"})

# 결과 개수를 셀 때 살펴보는 배열 키.
_RESULT_KEYS = ("results", "items", "policies", "agencies", "matches", "data")


def has_result_key(result: Any) -> bool:
    """응답에 결과 배열 키가 실제로 존재하는지 (빈 배열이어도 True)."""
    if not isinstance(result, dict):
        return False
    return any(isinstance(result.get(k), list) for k in _RESULT_KEYS)


def estimate_result_count(result: Any) -> int:
    """도구 응답 dict 에서 결과 개수 추정.

    ⚠️ 이 함수만으로는 경로 안내 도구를 판정할 수 없다(응답에 배열 키가 없음).
    _step_verdict 가 status 를 먼저 보고, status 가 없는 도구에만 폴백으로 쓴다.
    """
    if not isinstance(result, dict):
        return 0
    if "error" in result:
        return 0
    # 일반적인 키들 시도
    for key in _RESULT_KEYS:
        v = result.get(key)
        if isinstance(v, list):
            return len(v)
    # policy_id 단건 응답
    if "policy_id" in result or "id" in result:
        return 1
    return 0


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
    # 이 turn 이 끝난 시점에 길안내가 진행 중이었는지 (live_bridge 가 세팅).
    guiding: bool = False

    def reset(self) -> None:
        """다음 turn 을 위해 상태 초기화."""
        self.user_text_parts.clear()
        self.ai_text_parts.clear()
        self.tool_steps.clear()
        self.grounding_info = None
        self.asr_raw_parts.clear()
        self.guiding = False

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
        err = None
        status = None
        if isinstance(result, dict):
            status = result.get("status")
            if "error" in result:
                err = result.get("error")
            elif status == "error":
                # route_client._err() 는 error 키 없이 status="error" 로만 알린다.
                # 이걸 놓치면 경로 API 장애가 empty_result 로 뭉개져 관측되지 않는다.
                err = result.get("message") or "error"
        step = ToolStep(
            name=name,
            args=mask_coords(args),      # 정밀 좌표는 격자 수준으로 절사해 저장
            top_sim=_extract_top_sim(result),
            result_count=estimate_result_count(result),
            error=err,
            status=status if isinstance(status, str) else None,
            has_result_key=has_result_key(result),
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
                guiding=self.guiding,
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
                # JSONB 3종도 저장 직전 스크러빙 — user_query 만 마스킹하면
                # 같은 행의 asr_raw 에 원문이 남아 마스킹이 무력화된다.
                tool_chain=scrub_structure(ToolChain(steps=self.tool_steps).to_json()),
                fallback_reason=reason,
                ai_final_answer=ai_final,
                grounding_info=scrub_structure(self.grounding_info),
                asr_raw=scrub_structure(self.asr_raw_parts) or None,
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


