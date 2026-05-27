# create_unresolved_table.py
# Phase 5 Track A — unresolved_queries 테이블 1회성 생성 스크립트.
#
# 절차:
#   1) pgvector extension 활성화 + 버전 확인
#   2) Base.metadata.create_all 로 테이블 + 복합 인덱스 생성
#   3) HNSW partial 벡터 인덱스를 raw DDL 로 별도 생성
#      (pgvector < 0.5.0 환경에서는 자동으로 IVFFLAT 로 폴백)
#
# 실행:
#   cd welfare_backend
#   python create_unresolved_table.py
#
# 보안 원칙에 따라 DB 접속 정보는 .env 로 분리 (코드 하드코딩 금지).
import asyncio
import logging
import sys

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database import engine
from models import Base, UnresolvedQuery  # noqa: F401 — metadata 등록용 import

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _version_at_least(v: str, target: tuple) -> bool:
    try:
        parts = [int(p) for p in v.split(".")[:3]]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts) >= target
    except Exception:
        return False


async def main() -> int:
    logger.info("=== Track A: unresolved_queries 테이블 준비 시작 ===")
    try:
        async with engine.begin() as conn:
            # ─ 1) pgvector 확인 ─────────────────────────────────────
            row = (await conn.execute(text(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            ))).first()
            if row is None:
                logger.error("❌ pgvector extension 미활성. "
                             "DBA에게 'CREATE EXTENSION vector;' 실행 요청 필요.")
                return 1
            pgv_version = row[0]
            hnsw_ok = _version_at_least(pgv_version, (0, 5, 0))
            logger.info("[1/3] pgvector %s 확인 (HNSW 지원: %s)",
                        pgv_version, hnsw_ok)

            # ─ 2) 테이블 + 복합 인덱스 ──────────────────────────────
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[UnresolvedQuery.__table__],
                checkfirst=True,
            )
            logger.info("[2/3] ✅ unresolved_queries 테이블 + 일반/복합 인덱스 생성")

            # ─ 3) 벡터 인덱스 (HNSW 또는 IVFFLAT 폴백) ──────────────
            if hnsw_ok:
                idx_name = "idx_unresolved_embedding_hnsw"
                idx_sql = (
                    f"CREATE INDEX IF NOT EXISTS {idx_name} "
                    "ON unresolved_queries USING hnsw "
                    "(embedding vector_cosine_ops) "
                    "WHERE embedding IS NOT NULL"
                )
                idx_kind = "HNSW"
            else:
                # lists=100 은 ~1만 row 기준 권장값. 데이터 누적 후 ALTER 로 조정.
                idx_name = "idx_unresolved_embedding_ivfflat"
                idx_sql = (
                    f"CREATE INDEX IF NOT EXISTS {idx_name} "
                    "ON unresolved_queries USING ivfflat "
                    "(embedding vector_cosine_ops) "
                    "WITH (lists = 100) "
                    "WHERE embedding IS NOT NULL"
                )
                idx_kind = "IVFFLAT (HNSW 미지원으로 폴백)"
            await conn.execute(text(idx_sql))
            logger.info("[3/3] ✅ 벡터 인덱스 생성: %s (%s)", idx_name, idx_kind)

    except SQLAlchemyError as e:
        logger.exception("DB 오류: %s", e)
        return 1
    except Exception as e:
        logger.exception("예상치 못한 오류: %s", e)
        return 1
    finally:
        await engine.dispose()

    logger.info("🎉 Track A 테이블 준비 완료 — 다음 단계: live_bridge.py 적재 hook")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
