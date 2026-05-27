"""scripts/purge_old_queries.py
Track A — 오래된 unresolved_queries 자동 파기 cron.

목적:
  PII 리스크 분산 + 테이블 비대화 방지.
  user_query 에 사용자 발화 텍스트(이름·주소 등 미마스킹 PII 포함 가능)가
  남아있으므로, 분석 가치가 떨어진 오래된 행은 정기 파기.

기본 정책: 90일 (개인정보 보관 기준의 보수적 적용)

실행:
  python -m scripts.purge_old_queries                # 기본 90일
  python -m scripts.purge_old_queries --days 60      # 60일로 조정
  python -m scripts.purge_old_queries --dry-run      # 삭제 없이 영향 행수만

cron 등록 예 (Linux):
  30 3 * * * cd /opt/welfare_backend && /usr/bin/python3 -m scripts.purge_old_queries >> /var/log/welfare/purge.log 2>&1
  (매일 03:30)
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

load_dotenv(_ROOT / ".env")

from database import engine, AsyncSessionLocal  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("purge_old_queries")


async def main(days: int, dry_run: bool) -> int:
    logger.info("=== %d일 이전 unresolved_queries 파기 (dry_run=%s) ===",
                days, dry_run)
    try:
        async with AsyncSessionLocal() as ses:
            # 영향 행수 미리 카운트 (dry-run 모드와 공통)
            cnt = (await ses.execute(text(
                "SELECT count(*) FROM unresolved_queries "
                "WHERE created_at < NOW() - make_interval(days := :d)"
            ), {"d": days})).scalar()
            logger.info("대상 행수: %d", cnt)

            if dry_run:
                # 샘플 5건 미리보기
                if cnt > 0:
                    rows = (await ses.execute(text(
                        "SELECT id, created_at, LEFT(user_query, 60) "
                        "FROM unresolved_queries "
                        "WHERE created_at < NOW() - make_interval(days := :d) "
                        "ORDER BY created_at LIMIT 5"
                    ), {"d": days})).all()
                    logger.info("[dry-run] 삭제 예정 샘플 (최대 5건):")
                    for r in rows:
                        logger.info("    id=%d created=%s query=%r", r[0], r[1], r[2])
                logger.info("[dry-run] 실제 삭제 안 함")
                return 0

            # 실제 삭제
            result = await ses.execute(text(
                "DELETE FROM unresolved_queries "
                "WHERE created_at < NOW() - make_interval(days := :d)"
            ), {"d": days})
            await ses.commit()
            logger.info("✅ 삭제 완료: %d 행", result.rowcount or 0)
    except SQLAlchemyError as e:
        logger.exception("DB 오류: %s", e)
        return 1
    except Exception as e:
        logger.exception("예상치 못한 오류: %s", e)
        return 1
    finally:
        await engine.dispose()
    return 0


def _parse_args():
    p = argparse.ArgumentParser(description="UnresolvedQuery 오래된 행 파기")
    p.add_argument("--days", type=int, default=90,
                   help="이 일수보다 오래된 행을 삭제 (기본 90)")
    p.add_argument("--dry-run", action="store_true",
                   help="실제 삭제 없이 영향 행수만 출력")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(asyncio.run(main(args.days, args.dry_run)))
