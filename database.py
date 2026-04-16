import os
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


engine: Optional[AsyncEngine] = None
AsyncSessionLocal: Optional[async_sessionmaker[AsyncSession]] = None


def init_engine() -> AsyncEngine:
    global engine
    global AsyncSessionLocal

    if engine is not None and AsyncSessionLocal is not None:
        return engine

    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")

    engine = create_async_engine(url, pool_pre_ping=True)
    AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine


async def get_session() -> AsyncSession:
    init_engine()
    if AsyncSessionLocal is None:
        raise RuntimeError("Database session factory is not initialized")
    async with AsyncSessionLocal() as session:
        yield session

