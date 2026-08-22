from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://elowyn_runtime@localhost:5432/elowyn",
    )


engine = create_async_engine(database_url(), pool_pre_ping=True, hide_parameters=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
