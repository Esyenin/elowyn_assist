from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from elowyn.support.consistency import ConsistencyVerifier

pytestmark = pytest.mark.postgres


async def test_verifier_maps_plan_and_strategy_typed_entities(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as session:
        plan_id = uuid4()
        strategy_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO entities (id, entity_type) VALUES "
                "(:plan, 'PLAN'), (:strategy, 'STRATEGY')"
            ),
            {"plan": plan_id, "strategy": strategy_id},
        )
        await session.flush()

        report = await ConsistencyVerifier(session).verify()
        missing = {
            issue.object_id
            for issue in report.issues
            if issue.code == "PHYSICAL_TYPED_ROW_MISSING"
        }
        assert str(plan_id) in missing
        assert str(strategy_id) in missing


async def test_verifier_detects_invalid_presentation_and_basis_semantics(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as session:
        plan_id = uuid4()
        goal_id = uuid4()
        version_id = uuid4()
        conversation_id = uuid4()
        message_id = uuid4()
        source_id = uuid4()
        operation_id = uuid4()
        event_id = uuid4()
        presentation_id = uuid4()
        invalid_approved_id = uuid4()
        system_source_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO entities (id, entity_type) VALUES "
                "(:plan, 'PLAN'), (:goal, 'GOAL')"
            ),
            {"plan": plan_id, "goal": goal_id},
        )
        await session.execute(
            text("INSERT INTO plans (entity_id, title) VALUES (:id, 'Plan')"),
            {"id": plan_id},
        )
        await session.execute(
            text("INSERT INTO goals (entity_id, title, status) VALUES (:id, 'Goal', 'ACTIVE')"),
            {"id": goal_id},
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, transport, external_conversation_id) "
                "VALUES (:id, 'INTERNAL', 'planning-consistency')"
            ),
            {"id": conversation_id},
        )
        await session.execute(
            text(
                "INSERT INTO messages (id, conversation_id, author, text, sent_at) "
                "VALUES (:id, :conversation, 'USER', 'not an assistant presentation', now())"
            ),
            {"id": message_id, "conversation": conversation_id},
        )
        await session.execute(
            text(
                "INSERT INTO sources (id, source_type, message_id) "
                "VALUES (:id, 'USER_MESSAGE', :message)"
            ),
            {"id": source_id, "message": message_id},
        )
        await session.execute(
            text("INSERT INTO sources (id, source_type) VALUES (:id, 'SYSTEM')"),
            {"id": system_source_id},
        )
        await session.execute(
            text(
                "INSERT INTO plan_versions "
                "(id, plan_id, version_number, status, summary, "
                "proposed_strategy_snapshot, created_source_id) "
                "VALUES (:id, :plan, 1, 'CANDIDATE', 'Summary', 'Strategy', :source)"
            ),
            {"id": version_id, "plan": plan_id, "source": source_id},
        )
        await session.execute(
            text(
                "INSERT INTO plan_versions "
                "(id, plan_id, version_number, status, summary, "
                "proposed_strategy_snapshot, created_source_id, approval_source_id, approved_at) "
                "VALUES (:id, :plan, 2, 'APPROVED', 'Invalid approved', 'Strategy', "
                ":source, :source, now())"
            ),
            {"id": invalid_approved_id, "plan": plan_id, "source": system_source_id},
        )
        first_item = uuid4()
        second_item = uuid4()
        await session.execute(
            text(
                "INSERT INTO plan_version_items (id, plan_version_id, ordinal, title) VALUES "
                "(:first, :version, 1, 'First'), (:second, :version, 2, 'Second')"
            ),
            {"first": first_item, "second": second_item, "version": version_id},
        )
        await session.execute(
            text(
                "INSERT INTO plan_version_item_dependencies "
                "(plan_version_id, prerequisite_item_id, dependent_item_id) VALUES "
                "(:version, :first, :second), (:version, :second, :first)"
            ),
            {"version": version_id, "first": first_item, "second": second_item},
        )
        await session.execute(
            text(
                "INSERT INTO plan_item_progress (plan_version_item_id, status, source_id) "
                "VALUES (:item, 'NOT_STARTED', :source)"
            ),
            {"item": first_item, "source": source_id},
        )
        await session.execute(
            text(
                "INSERT INTO operations (id, actor_type, source_id) "
                "VALUES (:id, 'ASSISTANT', :source)"
            ),
            {"id": operation_id, "source": source_id},
        )
        await session.execute(
            text(
                "INSERT INTO events (id, operation_id, event_type, entity_id, source_id, changes) "
                "VALUES (:id, :operation, 'PLAN_VERSION_CREATED', :plan, :source, '[]'::jsonb)"
            ),
            {
                "id": event_id,
                "operation": operation_id,
                "plan": plan_id,
                "source": source_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO plan_version_presentations "
                "(id, plan_version_id, message_id) VALUES (:id, :version, :message)"
            ),
            {"id": presentation_id, "version": version_id, "message": message_id},
        )
        await session.execute(
            text(
                "INSERT INTO plan_version_basis (plan_version_id, entity_id, event_id, role) "
                "VALUES (:version, :goal, :event, 'GOAL')"
            ),
            {"version": version_id, "goal": goal_id, "event": event_id},
        )
        await session.flush()

        report = await ConsistencyVerifier(session).verify()
        codes = {issue.code for issue in report.issues}
        assert "PLAN_PRESENTATION_MESSAGE_INVALID" in codes
        assert "PLAN_BASIS_EVENT_MISMATCH" in codes
        assert "PLAN_APPROVAL_SOURCE_INVALID" in codes
        assert "PLAN_APPROVED_WITHOUT_PRESENTATION" in codes
        assert "PLAN_PROGRESS_WITHOUT_APPROVAL" in codes
        assert "PLAN_ITEM_DEPENDENCY_CYCLE" in codes
