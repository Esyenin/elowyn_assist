from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elowyn.db.models import Source
from elowyn.domain.commands import TaskCreate, TaskUpdate
from elowyn.domain.enums import ActorType, SourceType
from elowyn.services.world_state import ActionContext, WorldStateService
from elowyn.support.consistency import ConsistencyVerifier


async def test_verifier_detects_cross_table_and_history_corruption(safety_engine) -> None:
    async with safety_engine.connect() as connection:
        transaction = await connection.begin()
        wrong_type_id = str(uuid4())
        missing_typed_id = str(uuid4())
        orphan_event_id = str(uuid4())
        inference_id = str(uuid4())
        conversation_id = str(uuid4())
        message_id = str(uuid4())
        await connection.execute(
            text("INSERT INTO entities (id, entity_type) VALUES (:id, 'GOAL')"),
            {"id": wrong_type_id},
        )
        await connection.execute(
            text(
                "INSERT INTO tasks (entity_id, title, status, auto_complete_from_children) "
                "VALUES (:id, 'wrong type', 'TODO', false)"
            ),
            {"id": wrong_type_id},
        )
        await connection.execute(
            text("INSERT INTO entities (id, entity_type) VALUES (:id, 'TASK')"),
            {"id": missing_typed_id},
        )
        await connection.execute(
            text(
                "INSERT INTO sources (id, source_type, confidence, reason_summary) "
                "VALUES (:id, 'ASSISTANT_INFERENCE', 0.5, 'missing evidence')"
            ),
            {"id": inference_id},
        )
        await connection.execute(
            text(
                "INSERT INTO conversations (id, transport, external_conversation_id) "
                "VALUES (:id, 'TELEGRAM', 'verifier-corruption')"
            ),
            {"id": conversation_id},
        )
        await connection.execute(
            text(
                "INSERT INTO messages (id, conversation_id, author, text, sent_at) "
                "VALUES (:id, :conversation_id, 'USER', 'missing source', now())"
            ),
            {"id": message_id, "conversation_id": conversation_id},
        )
        await connection.execute(
            text("ALTER TABLE events DROP CONSTRAINT events_operation_id_fkey")
        )
        await connection.execute(
            text(
                "INSERT INTO events (id, operation_id, event_type, changes) "
                "VALUES (:id, :operation_id, 'TASK_UPDATED', '[]'::jsonb)"
            ),
            {"id": orphan_event_id, "operation_id": str(uuid4())},
        )
        await connection.execute(text("ALTER TABLE entity_relations DROP CONSTRAINT relation_type"))
        await connection.execute(
            text(
                "ALTER TABLE entity_relations "
                "DROP CONSTRAINT entity_relations_source_entity_id_fkey"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO entity_relations "
                "(id, source_entity_id, target_entity_id, relation_type) "
                "VALUES (:id, :source, :target, 'MADE_UP')"
            ),
            {"id": str(uuid4()), "source": str(uuid4()), "target": wrong_type_id},
        )

        old_decision_id = str(uuid4())
        new_decision_id = str(uuid4())
        await connection.execute(
            text(
                "INSERT INTO entities (id, entity_type) "
                "VALUES (:old_id, 'DECISION'), (:new_id, 'DECISION')"
            ),
            {"old_id": old_decision_id, "new_id": new_decision_id},
        )
        await connection.execute(
            text(
                "INSERT INTO decisions (entity_id, title, chosen_option, status) "
                "VALUES (:old_id, 'old', 'A', 'ACTIVE')"
            ),
            {"old_id": old_decision_id},
        )
        await connection.execute(
            text(
                "INSERT INTO decisions "
                "(entity_id, title, chosen_option, status, supersedes_decision_id) "
                "VALUES (:new_id, 'new', 'B', 'ACTIVE', :old_id)"
            ),
            {"new_id": new_decision_id, "old_id": old_decision_id},
        )

        session = AsyncSession(bind=connection, expire_on_commit=False)
        report = await ConsistencyVerifier(session).verify()
        codes = {issue.code for issue in report.issues}
        assert "ENTITY_TYPE_MISMATCH" in codes
        assert "PHYSICAL_TYPED_ROW_MISSING" in codes
        assert "EVENT_OPERATION_MISSING" in codes
        assert "BROKEN_INFERENCE_PROVENANCE" in codes
        assert "BROKEN_MESSAGE_PROVENANCE" in codes
        assert "DANGLING_ENTITY_RELATION" in codes
        assert "INVALID_RELATION_TYPE" in codes
        assert "INVALID_SUPERSEDE_CHAIN" in codes
        await session.close()
        await transaction.rollback()


async def test_verifier_is_clean_after_one_thousand_domain_mutations(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as session:
        source = Source(source_type=SourceType.SYSTEM, reason_summary="stress sequence")
        session.add(source)
        await session.flush()
        service = WorldStateService(session)
        ctx = ActionContext(ActorType.SYSTEM, source, description="1000 mutation stress")
        task = await service.create_task(TaskCreate(title="mutation-0"), ctx)
        for index in range(1, 1001):
            await service.update_task(
                TaskUpdate(entity_id=task.entity_id, title=f"mutation-{index}"), ctx
            )
        await session.commit()

    async with factory() as verify:
        report = await ConsistencyVerifier(verify).verify()
        report.require_ok()


async def test_verifier_does_not_autoflush_pending_changes(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as session:
        pending = Source(source_type=SourceType.SYSTEM, reason_summary="must remain pending")
        session.add(pending)
        report = await ConsistencyVerifier(session).verify()
        report.require_ok()
        assert pending.id is None
        await session.rollback()
