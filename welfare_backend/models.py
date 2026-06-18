# models.py
# welfare_policies + policy_chunks 테이블 정의. 스키마 기준(SoT)은 policy_db/ingest_sync.py 의 ensure_schema().
# PostgreSQL JSONB / ARRAY / pgvector(VECTOR) 타입 활용.
#
# Phase 5 Track A: UnresolvedQuery (데이터 플라이휠) 모델 추가됨.
import enum
import uuid

from sqlalchemy import (
    Column, String, Text, Integer, Boolean, Date, DateTime, BigInteger, ForeignKey,
    Index, Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector

Base = declarative_base()


class WelfarePolicy(Base):
    """마스터 테이블 — 정책별 1행. 메타데이터 필터링 + full_data JSONB."""
    __tablename__ = "welfare_policies"

    id = Column(String(10), primary_key=True)
    leaflet_section = Column(String(50))
    leaflet_number = Column(Integer)
    title = Column(String(200))
    short_summary = Column(Text)
    category = Column(String(20))
    benefit_type = Column(String(20))
    severity_levels = Column(ARRAY(Text))  # @> Contains 검색 필수
    has_companion_benefit = Column(Boolean)
    has_income_criteria = Column(Boolean)
    age_min = Column(Integer)
    age_max = Column(Integer)
    full_data = Column(JSONB)  # 전체 JSON 통째로 (Fat Tool Response 활용)
    last_verified = Column(Date)
    version = Column(String(50))  # ingest_sync 가 파일 MD5 해시를 변경감지 키로 저장(정책 버전 자체는 full_data 참조)
    active = Column(Boolean, nullable=False, server_default="true")  # soft delete
    deactivated_at = Column(DateTime(timezone=True))                 # 비활성 적용 일시
    created_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))


class PolicyChunk(Base):
    """벡터 검색용 청크 테이블 — 정책 1개당 평균 10여 개 청크.
    chunk_type: summary / eligibility / how_to_use / application / faq /
                exceptions / legal_basis / agency_specific / validity /
                penalties / contact (11종).
    """
    __tablename__ = "policy_chunks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    policy_id = Column(String(10), ForeignKey("welfare_policies.id", ondelete="CASCADE"))
    chunk_type = Column(String(30), nullable=False)
    chunk_subtype = Column(String(100))  # 예: faq_q03, agency_1
    content = Column(Text, nullable=False)
    embedding = Column(Vector(768))  # text-embedding-004 768차원
    embedding_model_version = Column(String(30))
    # NOTE: 'metadata' 는 SQLAlchemy Base 의 예약 어트리뷰트라 직접 사용 불가.
    # 컬럼명은 DB 상에서 'metadata' 그대로 유지하되 Python 속성은 metadata_ 로 우회.
    metadata_ = Column("metadata", JSONB)
    created_at = Column(DateTime(timezone=True))


# ─────────────────────────────────────────────────────────────
# Phase 5 Track A — UnresolvedQuery (데이터 플라이휠 적재 테이블)
#
# 적재 시점: AI 가 DB 도구로 답을 못 찾고 google_search 폴백, 또는
#            "정보가 없습니다" 형태로 응답한 turn 마다.
# 분석 시점: 주간 cron 으로 fallback_reason 별 분포, intent_group_id 클러스터,
#            embedding 클러스터링으로 신규 정책 발굴 후보 도출.
# ─────────────────────────────────────────────────────────────


class FallbackReason(str, enum.Enum):
    """폴백 사유 정형화 — 자유 문자열 방지로 GROUP BY 안정성 확보."""
    LOW_SIMILARITY    = "low_similarity"      # 벡터 검색 유사도가 임계값 미만
    EMPTY_RESULT      = "empty_result"        # 도구 호출했지만 결과 0건
    CATEGORY_MISMATCH = "category_mismatch"   # category 미지정/오분류로 metadata 검색 실패
    EXPLICIT_NO_INFO  = "explicit_no_info"    # AI 가 명시적으로 "정보 없음" 답변
    GOOGLE_SEARCH     = "google_search"       # 폴백으로 외부 검색 발동
    TOOL_ERROR        = "tool_error"          # DB 도구 호출 예외
    UNKNOWN           = "unknown"             # 분류 불가


class UnresolvedQuery(Base):
    """미해결 질의 로그 — 답변 실패 패턴을 분석해 신규 정책 발굴에 활용."""
    __tablename__ = "unresolved_queries"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 세션·의도 그룹은 모두 UUID 로 통일 (타입 일관성)
    session_id      = Column(UUID(as_uuid=True), nullable=False, index=True)
    intent_group_id = Column(UUID(as_uuid=True), nullable=False,
                             default=uuid.uuid4, index=True)
    turn_in_group   = Column(Integer, nullable=False, default=0)

    # 사용자 발화 텍스트 — 적재 직전 PII 스크러빙(전화·주민번호 등 고정 패턴) 적용
    user_query = Column(Text, nullable=False)

    # 정형화된 도구 호출 시퀀스 — schemas.ToolStep 으로 직렬화한 결과 (.model_dump(mode='json'))
    tool_chain      = Column(JSONB, nullable=True)
    # values_callable: enum 멤버의 *value*(소문자, 'low_similarity' 등)를 DB 에 저장.
    # 기본 동작(name 저장: 'LOW_SIMILARITY')은 raw SQL 분석 시 직관에 어긋남.
    fallback_reason = Column(SAEnum(
        FallbackReason,
        name="fallback_reason_enum",
        values_callable=lambda enum_cls: [e.value for e in enum_cls],
    ), nullable=False)
    ai_final_answer = Column(Text, nullable=True)
    grounding_info  = Column(JSONB, nullable=True)  # google_search grounding metadata
    asr_raw         = Column(JSONB, nullable=True)  # Live API input_transcription 원본

    # 임베딩은 비동기 백필 — INSERT 경로에서 외부 API 의존 제거
    embedding   = Column(Vector(768), nullable=True)
    embedded_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), nullable=False, index=True)

    # 분석 쿼리 가속용 복합 인덱스.
    # HNSW 벡터 인덱스는 create_unresolved_table.py 에서 raw DDL 로 별도 생성
    # (partial index + pgvector 버전 호환 처리 위해 metadata 외부 분리).
    __table_args__ = (
        Index("idx_unresolved_fallback_created", "fallback_reason", "created_at"),
        Index("idx_unresolved_group_turn", "intent_group_id", "turn_in_group"),
    )
