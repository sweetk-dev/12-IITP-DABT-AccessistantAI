"""scripts/backfill_embeddings.py
Track A — unresolved_queries.embedding 비동기 백필 cron.

목적:
  적재 hot path 에서 Gemini Embedding API 의존을 제거하기 위해,
  INSERT 시 embedding 컬럼은 NULL 로 두고 본 cron 이 batch 로 채움.

흐름:
  1) SELECT id, user_query WHERE embedding IS NULL LIMIT --batch-size
  2) Gemini Embedding API 단건 호출 (재시도 포함)
  3) UPDATE embedding, embedded_at
  4) --max-rows 또는 처리할 행 없을 때까지 반복

실행:
  cd welfare_backend
  python -m scripts.backfill_embeddings --batch-size 50 --max-rows 500

cron 등록 예 (Linux):
  */15 * * * * cd /opt/welfare_backend && /usr/bin/python3 -m scripts.backfill_embeddings >> /var/log/welfare/backfill.log 2>&1
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 패키지 임포트 컨텍스트 — welfare_backend/ 를 sys.path 에 추가
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
from sqlalchemy import text, update
from sqlalchemy.exc import SQLAlchemyError

load_dotenv(_ROOT / ".env")

from database import engine, AsyncSessionLocal  # noqa: E402
from models import UnresolvedQuery               # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_embeddings")


# ─────────────────────────────────────────────────────────────
# Gemini Embedding (main.py 의 _embed 와 동일 모델/차원)
# ─────────────────────────────────────────────────────────────
EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM   = int(os.environ.get("GEMINI_EMBED_DIM", "768"))

_client = None
def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def embed_text(text_query: str, *, retries: int = 2) -> list[float]:
    """단건 임베딩. 일시 장애에 가벼운 재시도."""
    from google.genai import types as _gtypes
    client = _get_client()
    last_err = None
    for attempt in range(retries + 1):
        try:
            cfg = _gtypes.EmbedContentConfig(output_dimensionality=EMBED_DIM)
            resp = client.models.embed_content(
                model=EMBED_MODEL, contents=text_query, config=cfg,
            )
            return resp.embeddings[0].values
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"임베딩 API 실패: {last_err}")


# ─────────────────────────────────────────────────────────────
# 백필 메인
# ─────────────────────────────────────────────────────────────
async def backfill_batch(batch_size: int, *, dry_run: bool) -> int:
    """한 배치(batch_size) 처리. 처리된 행 수 반환 (0 이면 더 이상 할 일 없음)."""
    async with AsyncSessionLocal() as ses:
        rows = (await ses.execute(text(
            "SELECT id, user_query FROM unresolved_queries "
            "WHERE embedding IS NULL ORDER BY id LIMIT :n"
        ), {"n": batch_size})).all()
        if not rows:
            return 0

        processed = 0
        for row in rows:
            qid, query = row
            try:
                if dry_run:
                    logger.info("  [dry-run] id=%d query=%r", qid, query[:60])
                    processed += 1
                    continue
                vec = embed_text(query)
                await ses.execute(update(UnresolvedQuery)
                                  .where(UnresolvedQuery.id == qid)
                                  .values(embedding=vec,
                                          embedded_at=datetime.now(timezone.utc)))
                processed += 1
            except Exception as e:
                logger.warning("  id=%d 임베딩 실패 — 건너뜀: %s", qid, e)
        if not dry_run:
            await ses.commit()
        return processed


async def main(batch_size: int, max_rows: int, dry_run: bool) -> int:
    logger.info("=== 임베딩 백필 시작 (batch=%d, max=%d, dry_run=%s) ===",
                batch_size, max_rows, dry_run)
    total = 0
    try:
        while total < max_rows:
            n_this = min(batch_size, max_rows - total)
            processed = await backfill_batch(n_this, dry_run=dry_run)
            if processed == 0:
                logger.info("→ 처리 대상 없음, 종료")
                break
            total += processed
            logger.info("→ 누적 처리: %d (방금 +%d)", total, processed)
    except SQLAlchemyError as e:
        logger.exception("DB 오류: %s", e)
        return 1
    except Exception as e:
        logger.exception("예상치 못한 오류: %s", e)
        return 1
    finally:
        await engine.dispose()
    logger.info("=== 백필 완료: 총 %d 행 처리 ===", total)
    return 0


def _parse_args():
    p = argparse.ArgumentParser(description="UnresolvedQuery embedding 백필")
    p.add_argument("--batch-size", type=int, default=50,
                   help="한 번에 처리할 행 수 (기본 50)")
    p.add_argument("--max-rows", type=int, default=500,
                   help="한 실행에서 최대 처리 행 수 (기본 500)")
    p.add_argument("--dry-run", action="store_true",
                   help="실제 임베딩/업데이트 없이 대상 행만 출력")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(asyncio.run(main(args.batch_size, args.max_rows, args.dry_run)))
