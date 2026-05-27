# database.py
# PostgreSQL(welfare_db) 비동기 연결 설정.
# 보안 원칙에 따라 DB 접속 정보는 .env 로 분리 (코드 하드코딩 금지).
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "")
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "welfare_db")

DATABASE_URL = (
    f"postgresql+asyncpg://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# echo=False 운영, 디버그 시 True 로 변경
engine = create_async_engine(DATABASE_URL, echo=False, future=True)

AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_db():
    """FastAPI 의존성 주입용 비동기 세션 제너레이터."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
