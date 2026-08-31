# schemas.py
# Phase 5 Track A — Pydantic 데이터 검증/직렬화 모델.
#
# JSONB 컬럼에 들어가는 데이터 키 표기를 코드 측에서 강제해
# "top_sim" vs "top_similarity" 같은 표기 혼재로 인한 분석 깨짐을 방지.
#
# pydantic 2.x 패턴 (.model_dump(mode='json') 사용).
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class ToolStep(BaseModel):
    """tool_chain JSONB 컬럼 1개 원소 표준 규격.

    적재 패턴:
        step = ToolStep(name="search_by_keyword", args={...},
                        top_sim=0.42, result_count=0)
        chain.steps.append(step)
        ...
        # DB INSERT 시:
        tool_chain_json = ToolChain(steps=chain.steps).to_json()
    """
    name: str
    args: Dict[str, Any] = Field(default_factory=dict)
    top_sim: Optional[float] = None     # 벡터 검색 시 최고 유사도 (없으면 None)
    result_count: int = 0
    error: Optional[str] = None         # 도구 예외 발생 시 메시지
    # 도구가 스스로 밝힌 결과 상태(success/guiding/idle/arrived/need_location/
    # out_of_service_area/error …). 정책 도구 5종은 status 를 반환하지 않아 None.
    status: Optional[str] = None
    # 응답에 결과 배열 키(results/items/…)가 실제로 있었는지.
    # status 가 success 여도 배열이 비어 있으면 "빈 결과"로 판정하기 위한 근거.
    has_result_key: bool = False


class ToolChain(BaseModel):
    """tool_chain JSONB 컬럼 전체 — 직렬화 헬퍼."""
    steps: List[ToolStep] = Field(default_factory=list)

    def to_json(self) -> List[dict]:
        """SQLAlchemy JSONB 컬럼에 그대로 넣을 수 있는 list[dict] 로 변환.

        datetime/uuid 등 비-JSON 객체도 안전하게 직렬화 (mode='json').
        """
        return [s.model_dump(mode="json") for s in self.steps]
