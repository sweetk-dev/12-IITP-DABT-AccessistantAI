# database.py
# PostgreSQL(welfare_db) 비동기 연결 설정.
# 보안 원칙에 따라 DB 접속 정보는 .env 로 분리 (코드 하드코딩 금지).
import os
from urllib.parse import quote_plus
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "")
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "welfare_db")

# 자격증명에 @ : / 등 특수문자가 있어도 URL 이 깨지지 않도록 인코딩.
DATABASE_URL = (
    f"postgresql+asyncpg://{quote_plus(DB_USER)}:{quote_plus(DB_PASS)}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
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


# --- iitp_db (이동편의 데이터 조회 전용, read-only) — 이슈 #165 -----------------
# 01-IITP-DABT-Database 의 iitp_db 를 콘솔에서 조회만 한다 (쓰기 금지).
# IITP_DB_NAME 미설정 시 기능 비활성 (엔진 미생성) — 콘솔 탭은 unconfigured 표시.
IITP_DB_USER = os.environ.get("IITP_DB_USER", DB_USER)
IITP_DB_PASS = os.environ.get("IITP_DB_PASS", DB_PASS)
IITP_DB_HOST = os.environ.get("IITP_DB_HOST", DB_HOST)
IITP_DB_PORT = os.environ.get("IITP_DB_PORT", DB_PORT)
IITP_DB_NAME = os.environ.get("IITP_DB_NAME", "")

iitp_engine = None
IitpSessionLocal = None
if IITP_DB_NAME:
    IITP_DATABASE_URL = (
        f"postgresql+asyncpg://{quote_plus(IITP_DB_USER)}:{quote_plus(IITP_DB_PASS)}"
        f"@{IITP_DB_HOST}:{IITP_DB_PORT}/{IITP_DB_NAME}"
    )
    iitp_engine = create_async_engine(IITP_DATABASE_URL, echo=False, future=True)
    IitpSessionLocal = sessionmaker(
        iitp_engine, class_=AsyncSession, expire_on_commit=False
    )


def iitp_db_configured() -> bool:
    return IitpSessionLocal is not None


async def get_iitp_db():
    """iitp_db 조회 전용 세션 (IITP_DB_NAME 미설정 시 503)."""
    if IitpSessionLocal is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="IITP DB not configured")
    async with IitpSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
