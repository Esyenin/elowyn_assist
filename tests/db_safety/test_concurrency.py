from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from elowyn.db.models import (
    Decision,
    EntityRelation,
    Event,
    Operation,
    Source,
    Task,
    TaskDependency,
)
from elowyn.domain.commands import (
    DecisionCreate,
    EntityRelationCreate,
    TaskCreate,
    TaskDependencyCreate,
    TaskUpdate,
)
from elowyn.domain.enums import ActorType, DecisionStatus, RelationType, SourceType
from elowyn.domain.errors import DomainValidationError
from elowyn.services.world_state import ActionContext, WorldStateService

pytestmark = pytest.mark.postgres


async def seed_sources(session, count: int) -> list[Source]:
    sources = [
        Source(source_type=SourceType.SYSTEM, reason_summary=f"concurrency-{i}")
        for i in range(count)
    ]
    session.add_all(sources)
    await session.flush()
    return sources


def context(source: Source, label: str) -> ActionContext:
    return ActionContext(ActorType.SYSTEM, source, description=label)


async def task_events(factory, task_id: UUID) -> list[Event]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(Event)
                    .where(Event.entity_id == task_id)
                    .order_by(Event.created_at, Event.id)
                )
            )
            .scalars()
            .all()
        )


async def test_two_task_updates_serialize_history_without_lost_update(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as seed:
        sources = await seed_sources(seed, 3)
        task = await WorldStateService(seed).create_task(
            TaskCreate(title="initial"), context(sources[0], "create")
        )
        task_id = task.entity_id
        source_ids = [source.id for source in sources]
        await seed.commit()

    async with factory() as first, factory() as second:
        first_sources = [await first.get(Source, source_id) for source_id in source_ids]
        second_sources = [await second.get(Source, source_id) for source_id in source_ids]
        await first.get(Task, task_id)
        await second.get(Task, task_id)

        await WorldStateService(first).update_task(
            TaskUpdate(entity_id=task_id, title="first"), context(first_sources[1], "first")
        )
        pending = asyncio.create_task(
            WorldStateService(second).update_task(
                TaskUpdate(entity_id=task_id, title="second"),
                context(second_sources[2], "second"),
            )
        )
        await asyncio.sleep(0.05)
        await first.commit()
        await asyncio.wait_for(pending, timeout=3)
        await second.commit()

    events = await task_events(factory, task_id)
    assert events[-2].changes[0] == {"field": "title", "old": "initial", "new": "first"}
    assert events[-1].changes[0] == {"field": "title", "old": "first", "new": "second"}
    assert events[-2].source_id == source_ids[1]
    assert events[-1].source_id == source_ids[2]
    assert events[-2].operation_id != events[-1].operation_id


async def test_update_then_concurrent_undo_reverses_actual_latest_state(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as seed:
        sources = await seed_sources(seed, 4)
        service = WorldStateService(seed)
        task = await service.create_task(TaskCreate(title="A"), context(sources[0], "create"))
        await service.update_task(
            TaskUpdate(entity_id=task.entity_id, title="B"), context(sources[1], "baseline")
        )
        task_id = task.entity_id
        source_ids = [source.id for source in sources]
        await seed.commit()

    async with factory() as update_session, factory() as undo_session:
        update_sources = [await update_session.get(Source, source_id) for source_id in source_ids]
        undo_sources = [await undo_session.get(Source, source_id) for source_id in source_ids]
        await update_session.get(Task, task_id)
        await undo_session.get(Task, task_id)

        update_service = WorldStateService(update_session)
        concurrent_update = await update_service.update_task(
            TaskUpdate(entity_id=task_id, title="C"),
            context(update_sources[2], "concurrent update"),
        )
        assert concurrent_update.title == "C"
        pending_undo = asyncio.create_task(
            WorldStateService(undo_session).undo_last_change(context(undo_sources[3], "undo"))
        )
        await asyncio.sleep(0.05)
        await update_session.commit()
        undo_event = await asyncio.wait_for(pending_undo, timeout=3)
        await undo_session.commit()

    async with factory() as verify:
        current = await verify.get(Task, task_id)
        update_event = (
            (
                await verify.execute(
                    select(Event)
                    .where(Event.entity_id == task_id, Event.source_id == source_ids[2])
                    .order_by(Event.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        persisted_undo = await verify.get(Event, undo_event.id)
        assert current.title == "B"
        assert persisted_undo.reverses_event_id == update_event.id
        assert persisted_undo.changes == [{"field": "title", "old": "C", "new": "B"}]
        assert persisted_undo.source_id == source_ids[3]
        assert await verify.get(Operation, persisted_undo.operation_id) is not None


async def test_global_undo_waits_for_change_on_another_entity(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as seed:
        sources = await seed_sources(seed, 4)
        service = WorldStateService(seed)
        first = await service.create_task(TaskCreate(title="first"), context(sources[0], "first"))
        second = await service.create_task(
            TaskCreate(title="second"), context(sources[1], "second")
        )
        first_id = first.entity_id
        second_id = second.entity_id
        source_ids = [source.id for source in sources]
        await seed.commit()

    async with factory() as update_session, factory() as undo_session:
        update_source = await update_session.get(Source, source_ids[2])
        undo_source = await undo_session.get(Source, source_ids[3])
        await WorldStateService(update_session).update_task(
            TaskUpdate(entity_id=second_id, title="newest"),
            context(update_source, "concurrent update"),
        )
        pending_undo = asyncio.create_task(
            WorldStateService(undo_session).undo_last_change(context(undo_source, "global undo"))
        )
        await asyncio.sleep(0.05)
        assert not pending_undo.done()
        await update_session.commit()
        undo = await asyncio.wait_for(pending_undo, timeout=3)
        await undo_session.commit()

    async with factory() as verify:
        assert (await verify.get(Task, first_id)).title == "first"
        assert (await verify.get(Task, second_id)).title == "second"
        newest = (
            (
                await verify.execute(
                    select(Event)
                    .where(Event.entity_id == second_id, Event.source_id == source_ids[2])
                    .order_by(Event.created_at.desc(), Event.id.desc())
                )
            )
            .scalars()
            .first()
        )
        assert undo.reverses_event_id == newest.id


async def test_two_decision_supersedes_allow_exactly_one_successor(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as seed:
        sources = await seed_sources(seed, 3)
        old = await WorldStateService(seed).create_decision(
            DecisionCreate(title="Storage", chosen_option="A"), context(sources[0], "create")
        )
        old_id = old.entity_id
        source_ids = [source.id for source in sources]
        await seed.commit()

    async with factory() as first, factory() as second:
        first_source = await first.get(Source, source_ids[1])
        second_source = await second.get(Source, source_ids[2])
        winner = await WorldStateService(first).create_decision(
            DecisionCreate(title="Storage", chosen_option="B", supersedes_decision_id=old_id),
            context(first_source, "winner"),
        )
        pending = asyncio.create_task(
            WorldStateService(second).create_decision(
                DecisionCreate(title="Storage", chosen_option="C", supersedes_decision_id=old_id),
                context(second_source, "loser"),
            )
        )
        await asyncio.sleep(0.05)
        await first.commit()
        with pytest.raises(DomainValidationError):
            await asyncio.wait_for(pending, timeout=3)
        await second.rollback()

    async with factory() as verify:
        successors = list(
            (
                await verify.execute(
                    select(Decision).where(Decision.supersedes_decision_id == old_id)
                )
            )
            .scalars()
            .all()
        )
        old = await verify.get(Decision, old_id)
        assert [item.entity_id for item in successors] == [winner.entity_id]
        assert old.status == DecisionStatus.SUPERSEDED
        losing_events = list(
            (await verify.execute(select(Event).where(Event.source_id == source_ids[2]))).scalars()
        )
        assert losing_events == []


async def test_duplicate_relation_creation_is_concurrently_idempotent(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as seed:
        sources = await seed_sources(seed, 3)
        service = WorldStateService(seed)
        left = await service.create_task(TaskCreate(title="left"), context(sources[0], "left"))
        right = await service.create_task(TaskCreate(title="right"), context(sources[0], "right"))
        ids = (left.entity_id, right.entity_id)
        source_ids = [source.id for source in sources]
        await seed.commit()

    command = EntityRelationCreate(
        source_entity_id=ids[0], target_entity_id=ids[1], relation_type=RelationType.SUPPORTS
    )
    async with factory() as first, factory() as second:
        source1 = await first.get(Source, source_ids[1])
        source2 = await second.get(Source, source_ids[2])
        created = await WorldStateService(first).create_relation(command, context(source1, "first"))
        pending = asyncio.create_task(
            WorldStateService(second).create_relation(command, context(source2, "duplicate"))
        )
        await asyncio.sleep(0.05)
        await first.commit()
        reused = await asyncio.wait_for(pending, timeout=3)
        await second.commit()
        assert reused.id == created.id

    async with factory() as verify:
        relations = list((await verify.execute(select(EntityRelation))).scalars())
        relation_events = list(
            (
                await verify.execute(select(Event).where(Event.event_type == "RELATION_CREATED"))
            ).scalars()
        )
        assert len(relations) == 1
        assert len(relation_events) == 1
        assert relation_events[0].source_id == source_ids[1]


@pytest.mark.parametrize("kind", ["parent", "dependency"])
async def test_concurrent_graph_cycle_is_rejected(safety_engine, kind: str) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as seed:
        sources = await seed_sources(seed, 3)
        service = WorldStateService(seed)
        left = await service.create_task(TaskCreate(title="left"), context(sources[0], "left"))
        right = await service.create_task(TaskCreate(title="right"), context(sources[0], "right"))
        ids = (left.entity_id, right.entity_id)
        source_ids = [source.id for source in sources]
        await seed.commit()

    async with factory() as first, factory() as second:
        source1 = await first.get(Source, source_ids[1])
        source2 = await second.get(Source, source_ids[2])
        first_service = WorldStateService(first)
        second_service = WorldStateService(second)
        if kind == "parent":
            await first_service.update_task(
                TaskUpdate(entity_id=ids[0], parent_task_id=ids[1]), context(source1, "first")
            )
            pending = asyncio.create_task(
                second_service.update_task(
                    TaskUpdate(entity_id=ids[1], parent_task_id=ids[0]),
                    context(source2, "cycle"),
                )
            )
        else:
            await first_service.add_task_dependency(
                TaskDependencyCreate(prerequisite_task_id=ids[0], dependent_task_id=ids[1]),
                context(source1, "first"),
            )
            pending = asyncio.create_task(
                second_service.add_task_dependency(
                    TaskDependencyCreate(prerequisite_task_id=ids[1], dependent_task_id=ids[0]),
                    context(source2, "cycle"),
                )
            )
        await asyncio.sleep(0.05)
        await first.commit()
        with pytest.raises(DomainValidationError):
            await asyncio.wait_for(pending, timeout=3)
        await second.rollback()

    async with factory() as verify:
        if kind == "parent":
            left_row = await verify.get(Task, ids[0])
            right_row = await verify.get(Task, ids[1])
            assert (left_row.parent_task_id, right_row.parent_task_id) == (ids[1], None)
        else:
            edges = list((await verify.execute(select(TaskDependency))).scalars())
            assert [(edge.prerequisite_task_id, edge.dependent_task_id) for edge in edges] == [ids]
        assert (
            list(
                (
                    await verify.execute(select(Event).where(Event.source_id == source_ids[2]))
                ).scalars()
            )
            == []
        )


async def test_opposite_relation_lock_order_does_not_deadlock(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    async with factory() as seed:
        sources = await seed_sources(seed, 3)
        service = WorldStateService(seed)
        left = await service.create_task(TaskCreate(title="left"), context(sources[0], "left"))
        right = await service.create_task(TaskCreate(title="right"), context(sources[0], "right"))
        ids = (left.entity_id, right.entity_id)
        source_ids = [source.id for source in sources]
        await seed.commit()

    async with factory() as first, factory() as second:
        source1 = await first.get(Source, source_ids[1])
        source2 = await second.get(Source, source_ids[2])
        forward = asyncio.create_task(
            WorldStateService(first).create_relation(
                EntityRelationCreate(
                    source_entity_id=ids[0],
                    target_entity_id=ids[1],
                    relation_type=RelationType.RELATED_TO,
                ),
                context(source1, "forward"),
            )
        )
        reverse = asyncio.create_task(
            WorldStateService(second).create_relation(
                EntityRelationCreate(
                    source_entity_id=ids[1],
                    target_entity_id=ids[0],
                    relation_type=RelationType.RELATED_TO,
                ),
                context(source2, "reverse"),
            )
        )
        await asyncio.wait_for(forward, timeout=3)
        await first.commit()
        await asyncio.wait_for(reverse, timeout=3)
        await second.commit()

    async with factory() as verify:
        assert len(list((await verify.execute(select(EntityRelation))).scalars())) == 2
