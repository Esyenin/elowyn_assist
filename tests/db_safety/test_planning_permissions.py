from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.models import Message, PlanVersion, PlanVersionPresentation
from elowyn.domain.enums import MessageAuthor, TransportType
from elowyn.domain.messages import IncomingMessage
from elowyn.runtime import ElowynRuntime
from elowyn.support.database_safety import assert_test_database_url

pytestmark = pytest.mark.postgres


async def assert_forbidden(connection, statement: str, params: dict | None = None) -> None:
    transaction = await connection.begin_nested()
    with pytest.raises(DBAPIError) as caught:
        await connection.execute(text(statement), params or {})
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    await transaction.rollback()


def runtime_url() -> str:
    url = os.environ.get("TEST_RUNTIME_DATABASE_URL")
    if not url:
        pytest.fail("TEST_RUNTIME_DATABASE_URL is required for permission tests")
    assert_test_database_url(url)
    return url


async def test_runtime_planning_permissions_preserve_immutable_content(safety_engine) -> None:
    engine = create_async_engine(runtime_url())
    plan_id = str(uuid4())
    version_id = str(uuid4())
    item_id = str(uuid4())
    source_id = str(uuid4())
    strategy_id = str(uuid4())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO sources (id, source_type) VALUES (:id, 'SYSTEM')"),
                {"id": source_id},
            )
            await connection.execute(
                text("INSERT INTO entities (id, entity_type) VALUES (:id, 'PLAN')"),
                {"id": plan_id},
            )
            await connection.execute(
                text("INSERT INTO plans (entity_id, title) VALUES (:id, 'Permission plan')"),
                {"id": plan_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO plan_versions "
                    "(id, plan_id, version_number, status, summary, "
                    "proposed_strategy_snapshot, created_source_id) "
                    "VALUES (:id, :plan, 1, 'CANDIDATE', 'Immutable', 'Strategy', :source)"
                ),
                {"id": version_id, "plan": plan_id, "source": source_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO plan_version_items (id, plan_version_id, ordinal, title) "
                    "VALUES (:id, :version, 1, 'First')"
                ),
                {"id": item_id, "version": version_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO plan_item_progress "
                    "(plan_version_item_id, status, source_id) "
                    "VALUES (:item, 'NOT_STARTED', :source)"
                ),
                {"item": item_id, "source": source_id},
            )
            await connection.execute(
                text(
                    "UPDATE plan_item_progress SET status='IN_PROGRESS', source_id=:source "
                    "WHERE plan_version_item_id=:item"
                ),
                {"source": source_id, "item": item_id},
            )
            await connection.execute(
                text("UPDATE plan_versions SET status='REJECTED' WHERE id=:id"),
                {"id": version_id},
            )
            await connection.execute(
                text("INSERT INTO entities (id, entity_type) VALUES (:id, 'STRATEGY')"),
                {"id": strategy_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO strategies "
                    "(entity_id, approach, accepted_from_plan_version_id, "
                    "accepted_source_id, accepted_at) "
                    "VALUES (:id, 'Accepted', :version, :source, now())"
                ),
                {"id": strategy_id, "version": version_id, "source": source_id},
            )
            await connection.execute(
                text("UPDATE plans SET strategy_id=:strategy WHERE entity_id=:plan"),
                {"strategy": strategy_id, "plan": plan_id},
            )
            await connection.execute(
                text("UPDATE strategies SET approach='Refined' WHERE entity_id=:id"),
                {"id": strategy_id},
            )

            insert_privileges = (
                await connection.execute(
                    text(
                        "SELECT bool_and(has_table_privilege(current_user, table_name, 'INSERT')) "
                        "FROM unnest(ARRAY['plans','strategies','plan_versions',"
                        "'plan_version_items','plan_version_item_dependencies',"
                        "'plan_item_progress','plan_version_presentations',"
                        "'plan_version_basis','plan_goal_links']) AS table_name"
                    )
                )
            ).scalar_one()
            assert insert_privileges is True

            await assert_forbidden(
                connection,
                "UPDATE plan_versions SET summary='tampered' WHERE id=:id",
                {"id": version_id},
            )
            await assert_forbidden(
                connection,
                "UPDATE plan_version_items SET title='tampered' WHERE id=:id",
                {"id": item_id},
            )
            await assert_forbidden(
                connection,
                "DELETE FROM plan_item_progress WHERE plan_version_item_id=:id",
                {"id": item_id},
            )
            await assert_forbidden(
                connection,
                "DELETE FROM plan_versions WHERE id=:id",
                {"id": version_id},
            )
    finally:
        await engine.dispose()


async def test_runtime_role_can_persist_candidate_message_and_presentation(safety_engine) -> None:
    def model_function(messages, info):
        for message in reversed(messages):
            for part in message.parts:
                content = getattr(part, "content", None)
                if isinstance(content, dict) and "presentation_placeholder" in content:
                    return ModelResponse(parts=[TextPart(content["presentation_placeholder"])])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="create_plan_with_candidate",
                    args={
                        "plan": {"title": "Runtime permission Plan"},
                        "candidate": {
                            "summary": "Permission Candidate",
                            "proposed_strategy_snapshot": "Use the validated runtime boundary",
                            "items": [{"ordinal": 1, "title": "Persist atomically"}],
                        },
                    },
                )
            ]
        )

    engine = create_async_engine(runtime_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        response = await ElowynRuntime(
            session_factory=factory,
            model=FunctionModel(model_function),
        ).handle_message(
            IncomingMessage(
                transport=TransportType.INTERNAL,
                external_conversation_id=f"planning-permission-{uuid4()}",
                external_message_id=str(uuid4()),
                text="Propose a synthetic concrete plan",
                sent_at=datetime.now(UTC),
            )
        )
        assert "Use the validated runtime boundary" in response
        async with factory() as session:
            version = (await session.execute(select(PlanVersion))).scalar_one()
            presentation = (
                await session.execute(select(PlanVersionPresentation))
            ).scalar_one()
            message = await session.get(Message, presentation.message_id)
            assert presentation.plan_version_id == version.id
            assert message.author == MessageAuthor.ASSISTANT
            assert message.text == response
    finally:
        await engine.dispose()
