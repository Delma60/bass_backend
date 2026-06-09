# backend/app/db/postgres.py
from collections.abc import AsyncGenerator
import ssl

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

from app.config import settings


def _build_engine_kwargs(url: str) -> dict:
    """
    asyncpg doesn't accept sslmode= in the URL — strip it and pass ssl via connect_args.
    """
    connect_args = {}
    if "sslmode=require" in url or "sslmode=verify-full" in url or "sslmode=verify-ca" in url:
        url = url.replace("?sslmode=require", "").replace("&sslmode=require", "")
        url = url.replace("?sslmode=verify-full", "").replace("&sslmode=verify-full", "")
        url = url.replace("?sslmode=verify-ca", "").replace("&sslmode=verify-ca", "")
        connect_args["ssl"] = ssl.create_default_context()
    elif "sslmode=disable" in url:
        url = url.replace("?sslmode=disable", "").replace("&sslmode=disable", "")

    # Ensure the asyncpg driver prefix
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return {"url": url, "connect_args": connect_args}


_kwargs = _build_engine_kwargs(settings.database_url)

engine = create_async_engine(
    _kwargs["url"],
    echo=settings.node_env == "development",
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    connect_args=_kwargs["connect_args"],
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def set_tenant_session(session: AsyncSession, schema: str) -> None:
    await session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
    if settings.db_enable_rls and settings.db_restricted_role:
        role = settings.db_restricted_role
        await session.execute(text(f'SET LOCAL ROLE "{role}"'))


async def set_tenant_connection(conn, schema: str) -> None:
    await conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
    if settings.db_enable_rls and settings.db_restricted_role:
        role = settings.db_restricted_role
        await conn.execute(text(f'SET LOCAL ROLE "{role}"'))