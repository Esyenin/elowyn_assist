from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from elowyn.db.base import Base
from elowyn.support.database_safety import assert_test_database_url


@pytest.fixture
async def safety_engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for DB safety tests")
    assert_test_database_url(url)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
        await engine.dispose()
