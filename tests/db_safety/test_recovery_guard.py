from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from scripts.recovery_drill import asyncpg_url, quote_identifier, recreate_database


async def test_recovery_refuses_existing_database_without_sentinel() -> None:
    admin_url = os.environ.get("ELOWYN_ADMIN_DATABASE_URL")
    target_base = os.environ.get("TEST_DATABASE_URL")
    if not admin_url or not target_base:
        pytest.fail("admin and test database URLs are required for the recovery guard test")
    database = f"elowyn_recovery_guard_test_{uuid4().hex}"
    target_url = make_url(target_base).set(database=database).render_as_string(hide_password=False)
    connection = await asyncpg.connect(asyncpg_url(admin_url))
    try:
        await connection.execute(
            f"CREATE DATABASE {quote_identifier(database)} OWNER elowyn_test_owner"
        )
        with pytest.raises(RuntimeError, match="recovery-test sentinel"):
            await recreate_database(admin_url, target_url)
        assert await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)", database
        )
    finally:
        await connection.execute(f"DROP DATABASE IF EXISTS {quote_identifier(database)}")
        await connection.close()
