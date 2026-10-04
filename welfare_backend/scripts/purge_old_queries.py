"""scripts/purge_old_queries.py
Track A — 오래된 unresolved_queries 자동 파기 cron.

목적:
  PII 리스크 분산 + 테이블 비대화 방지.
  user_query 에 사용자 발화 텍스트(이름·주소 등 미마스킹 PII 포함 가능)가
  남아있으므로, 분석 가치가 떨어진 오래된 행은 정기 파기.

기본 정책: 90일 (개인정보 보관 기준의 보수적 적용)

파기 예외:
  발굴 산출물(신규 후보, 검토 대기 중인 보강 제안)이 query_ids 로 참조 중인 행은
  기간이 지나도 남긴다. 그 행을 지우면 후보의 근거가 끊기고, 반려 시 원 질의를
  재분류 대기로 되돌릴 수 없다. 반려된 후보가 참조하던 행은 예외가 아니다.
  참조 목록을 읽지 못하면 파기를 실행하지 않는다(종료 코드 1).

정기 실행:
  서버의 인앱 스케줄러(scheduler.py)가 매일 1회 이 스크립트를 실행한다.
  아래 cron 예시는 서버를 띄우지 않는 환경에서 따로 돌릴 때만 쓴다(중복 등록 불필요).

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
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

load_dotenv(_ROOT / ".env")

from database import engine, AsyncSessionLocal  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("purge_old_queries")


def _purge_where(keep_ids):
    """파기 대상 WHERE 절과 바인드 값을 만든다.

    인자: keep_ids — 남겨야 하는 행 id 모음(발굴 산출물이 참조 중인 질의).
    반환: (where_sql, bind_params: list, values: dict). days 값(:d)은 호출자가 values 에 넣는다.
    조건: 생성일이 보존기간보다 오래됨 AND id 가 keep_ids 에 없음.
    keep_ids 가 비어 있으면 id 조건을 붙이지 않는다(기간 조건만).
    """
    where = "created_at < NOW() - make_interval(days := :d)"
    keep = sorted({int(i) for i in (keep_ids or [])})
    if not keep:
        return where, [], {}
    # expanding 바인드: 목록 길이만큼 자리표시자를 펼쳐 IN 절을 만든다(드라이버 무관).
    return (where + " AND id NOT IN :keep",
            [bindparam("keep", expanding=True)],
            {"keep": keep})


def _load_keep_ids():
    """발굴 산출물이 참조 중인 질의 id 집합. 읽지 못하면 예외를 올린다(파기 중단)."""
    import discovery_core as dc
    return dc.referenced_query_ids()


async def main(days: int, dry_run: bool) -> int:
    logger.info("=== %d일 이전 unresolved_queries 파기 (dry_run=%s) ===",
                days, dry_run)
    try:
        # 참조 중인 행 목록을 먼저 확정한다. 여기서 실패하면 아무것도 지우지 않는다 —
        # 목록 없이 지우면 후보가 근거로 삼는 행까지 물리 삭제된다.
        keep_ids = _load_keep_ids()
        where, binds, vals = _purge_where(keep_ids)
        vals["d"] = days
        if keep_ids:
            logger.info("발굴 산출물이 참조 중인 질의 %d건은 기간과 무관하게 남깁니다.", len(keep_ids))

        def _stmt(sql):
            st = text(sql)
            return st.bindparams(*binds) if binds else st

        async with AsyncSessionLocal() as ses:
            # 영향 행수 미리 카운트 (dry-run 모드와 공통)
            cnt = (await ses.execute(_stmt(
                "SELECT count(*) FROM unresolved_queries WHERE " + where
            ), vals)).scalar()
            logger.info("대상 행수: %d", cnt)

            if dry_run:
                # 샘플 5건 미리보기
                if cnt > 0:
                    rows = (await ses.execute(_stmt(
                        "SELECT id, created_at, LEFT(user_query, 60) "
                        "FROM unresolved_queries "
                        "WHERE " + where + " "
                        "ORDER BY created_at LIMIT 5"
                    ), vals)).all()
                    logger.info("[dry-run] 삭제 예정 샘플 (최대 5건):")
                    for r in rows:
                        logger.info("    id=%d created=%s query=%r", r[0], r[1], r[2])
                logger.info("[dry-run] 실제 삭제 안 함")
                return 0

            # 실제 삭제
            result = await ses.execute(_stmt(
                "DELETE FROM unresolved_queries WHERE " + where
            ), vals)
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
