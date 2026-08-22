from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.models import Event, Operation, Source, Task
from elowyn.domain.commands import TaskCreate, TaskUpdate
from elowyn.domain.enums import ActorType, SourceType
from elowyn.runtime import ElowynRuntime
from elowyn.services.world_state import ActionContext, WorldStateService
from elowyn.support.consistency import ConsistencyVerifier
from elowyn.support.database_safety import assert_test_database_url
from elowyn.transport.telegram import TelegramAdapter

pytestmark = pytest.mark.postgres


def runtime_url() -> str:
    url = os.environ.get("TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.fail("TEST_RUNTIME_DATABASE_URL is required for permission tests")
    assert_test_database_url(url)
    return url


def admin_url() -> str:
    url = os.environ.get("ELOWYN_ADMIN_DATABASE_URL")
    if not url:
        pytest.fail("ELOWYN_ADMIN_DATABASE_URL is required for permission tests")
    return url


async def assert_forbidden(connection, statement: str) -> None:
    transaction = await connection.begin_nested()
    with pytest.raises(DBAPIError) as caught:
        await connection.execute(text(statement))
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    await transaction.rollback()


async def test_runtime_role_supports_domain_service_but_history_is_append_only() -> None:
    engine = create_async_engine(runtime_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            source = Source(source_type=SourceType.SYSTEM, reason_summary="permission test")
            session.add(source)
            await session.flush()
            ctx = ActionContext(ActorType.SYSTEM, source, description="permission test")
            service = WorldStateService(session)
            task = await service.create_task(TaskCreate(title="permission-safe"), ctx)
            await service.update_task(TaskUpdate(entity_id=task.entity_id, title="updated"), ctx)
            undo = await service.undo_last_change(ctx, entity_id=task.entity_id)
            await session.commit()

        async with factory() as verify:
            current = await verify.get(Task, task.entity_id)
            assert current.title == "permission-safe"
            assert await verify.get(Event, undo.id) is not None
            assert (await verify.execute(select(Operation))).scalars().all()
            assert (await verify.execute(select(Source))).scalars().all()
    finally:
        await engine.dispose()


async def test_runtime_role_cannot_execute_ddl_or_truncate() -> None:
    engine = create_async_engine(runtime_url())
    try:
        async with engine.connect() as connection:
            await assert_forbidden(connection, "CREATE TABLE runtime_escape (id integer)")
            await assert_forbidden(connection, "CREATE TEMP TABLE runtime_escape (id integer)")
            await assert_forbidden(
                connection, "ALTER TABLE tasks ADD COLUMN runtime_escape integer"
            )
            await assert_forbidden(connection, "DROP TABLE tasks")
            await assert_forbidden(connection, "TRUNCATE TABLE tasks CASCADE")
    finally:
        await engine.dispose()


async def test_runtime_role_cannot_mutate_or_delete_history() -> None:
    engine = create_async_engine(runtime_url())
    try:
        async with engine.connect() as connection:
            await assert_forbidden(
                connection, "UPDATE events SET changes = '[]'::jsonb WHERE false"
            )
            await assert_forbidden(connection, "DELETE FROM events WHERE false")
            await assert_forbidden(
                connection, "UPDATE operations SET description = NULL WHERE false"
            )
            await assert_forbidden(connection, "DELETE FROM operations WHERE false")
            await assert_forbidden(
                connection, "UPDATE sources SET reason_summary = NULL WHERE false"
            )
            await assert_forbidden(connection, "DELETE FROM sources WHERE false")
            await assert_forbidden(connection, "DELETE FROM tasks WHERE false")
    finally:
        await engine.dispose()


async def test_runtime_role_attributes_and_schema_ownership_are_minimal() -> None:
    admin_engine = create_async_engine(admin_url())
    target_engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    try:
        async with admin_engine.connect() as connection:
            role = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication "
                        ", rolinherit, rolbypassrls "
                        "FROM pg_roles WHERE rolname = 'elowyn_test_runtime'"
                    )
                )
            ).one()
            assert tuple(role) == (False, False, False, False, False, False)
        async with target_engine.connect() as connection:
            schema_owner = (
                await connection.execute(
                    text(
                        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                        "WHERE nspname = 'public'"
                    )
                )
            ).scalar_one()
            can_create = (
                await connection.execute(
                    text("SELECT has_schema_privilege('elowyn_test_runtime', 'public', 'CREATE')")
                )
            ).scalar_one()
            public_can_create = (
                await connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_namespace n, LATERAL aclexplode(n.nspacl) acl "
                        "WHERE n.nspname = 'public' AND acl.grantee = 0 "
                        "AND acl.privilege_type = 'CREATE')"
                    )
                )
            ).scalar_one()
            can_create_temp = (
                await connection.execute(
                    text(
                        "SELECT has_database_privilege("
                        "'elowyn_test_runtime', current_database(), 'TEMP')"
                    )
                )
            ).scalar_one()
            memberships = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_auth_members membership "
                        "JOIN pg_roles member ON member.oid = membership.member "
                        "WHERE member.rolname = 'elowyn_test_runtime'"
                    )
                )
            ).scalar_one()
            assert schema_owner == "elowyn_test_owner"
            assert can_create is False
            assert public_can_create is False
            assert can_create_temp is False
            assert memberships == 0
    finally:
        await admin_engine.dispose()
        await target_engine.dispose()


async def test_runtime_role_full_adapter_pydantic_core_postgres_chain() -> None:
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="create_task", args={"title": "runtime chain"})]
            )
        return ModelResponse(parts=[TextPart("Сохранила задачу.")])

    class TelegramMessage:
        chat = SimpleNamespace(id=9901)
        message_id = 990001
        text = "Запомни задачу runtime chain"
        date = datetime.now(UTC)

        def model_dump(self, **kwargs):
            return {"chat": {"id": self.chat.id}, "message_id": self.message_id, "text": self.text}

    engine = create_async_engine(runtime_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        adapter = TelegramAdapter(allowed_user_id=9901)
        assert adapter.check_user(9901)
        runtime = ElowynRuntime(session_factory=factory, model=FunctionModel(model_function))
        response = await runtime.handle_message(adapter.to_incoming(TelegramMessage()))
        assert response == "Сохранила задачу."
        async with factory() as session:
            task = (
                await session.execute(select(Task).where(Task.title == "runtime chain"))
            ).scalar_one()
            assert task is not None
            (await ConsistencyVerifier(session).verify()).require_ok()
    finally:
        await engine.dispose()
