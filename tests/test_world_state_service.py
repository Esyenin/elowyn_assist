from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from elowyn.db.base import Base
from elowyn.db.models import (
    Decision,
    Entity,
    EntityRelation,
    Event,
    Operation,
    Source,
    SourceDependency,
    SuccessCriterion,
    Task,
    TaskDependency,
    TaskGoalLink,
)
from elowyn.domain.commands import (
    DecisionAlternativeCreate,
    DecisionCreate,
    EntityRelationCreate,
    EntityRelationInference,
    GoalAssessment,
    GoalCreate,
    ProjectAssessment,
    ProjectCreate,
    ProjectSummaryCacheUpdate,
    SuccessCriterionAssessment,
    SuccessCriterionCreate,
    SuccessCriterionUpdate,
    TaskAssessment,
    TaskCreate,
    TaskDependencyCreate,
    TaskUpdate,
)
from elowyn.domain.enums import (
    ActorType,
    DeadlineType,
    DecisionStatus,
    RelationType,
    SourceType,
    SuccessCriterionStatus,
    TransportType,
)
from elowyn.domain.errors import DomainValidationError, EntityNotFoundError
from elowyn.domain.messages import IncomingMessage
from elowyn.services.conversation import ConversationService
from elowyn.services.query import WorldStateQueryService
from elowyn.services.world_state import ActionContext, WorldStateService


class _AsyncTransaction:
    def __init__(self, transaction: AbstractContextManager):
        self.transaction = transaction

    async def __aenter__(self):
        return self.transaction.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.transaction.__exit__(exc_type, exc, tb)


class AsyncSessionShim:
    """Small async facade over a real sync SQLAlchemy Session for fast service tests."""

    def __init__(self, session: Session):
        self.sync = session

    def add(self, obj) -> None:
        self.sync.add(obj)

    async def flush(self) -> None:
        self.sync.flush()

    async def get(self, model, ident):
        return self.sync.get(model, ident)

    async def execute(self, statement):
        return self.sync.execute(statement)

    async def commit(self) -> None:
        self.sync.commit()

    async def rollback(self) -> None:
        self.sync.rollback()

    def begin_nested(self):
        return _AsyncTransaction(self.sync.begin_nested())


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as sync_session:
        yield AsyncSessionShim(sync_session)
    engine.dispose()


_counter = 0


async def user_turn(session: AsyncSessionShim, text: str):
    global _counter
    _counter += 1
    service = ConversationService(session)
    return await service.ingest_user_message(
        IncomingMessage(
            transport=TransportType.TELEGRAM,
            external_conversation_id="chat-1",
            external_message_id=str(_counter),
            text=text,
            sent_at=datetime.now(UTC),
        )
    )


@pytest.mark.asyncio
async def test_conversation_message_creates_user_source_and_is_idempotent(session) -> None:
    turn = await user_turn(session, "Создай задачу")
    assert turn.source.source_type == SourceType.USER_MESSAGE
    assert turn.source.message_id == turn.message.id
    assert turn.is_new is True

    duplicate = await ConversationService(session).ingest_user_message(
        IncomingMessage(
            transport=TransportType.TELEGRAM,
            external_conversation_id="chat-1",
            external_message_id=turn.message.external_message_id,
            text="Создай задачу",
            sent_at=turn.message.sent_at,
        )
    )
    assert duplicate.message.id == turn.message.id
    assert duplicate.source.id == turn.source.id
    assert duplicate.is_new is False

    conversation_service = ConversationService(session)
    assert not await conversation_service.has_assistant_reply(
        conversation_id=turn.conversation.id, user_message_id=turn.message.id
    )
    await conversation_service.record_assistant_message(
        conversation_id=turn.conversation.id,
        text="Запомнила.",
        in_reply_to_message_id=turn.message.id,
    )
    assert await conversation_service.has_assistant_reply(
        conversation_id=turn.conversation.id, user_message_id=turn.message.id
    )


@pytest.mark.asyncio
async def test_task_update_records_field_changes_and_message_provenance(session) -> None:
    create_turn = await user_turn(session, "Задача с дедлайном 30-го")
    service = WorldStateService(session)
    task = await service.create_task(
        TaskCreate(
            title="Отправить отчёт",
            deadline_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
            deadline_type=DeadlineType.HARD,
        ),
        ActionContext(ActorType.USER, create_turn.source),
    )

    update_turn = await user_turn(session, "Перенеси дедлайн на 28-е")
    await service.update_task(
        TaskUpdate(entity_id=task.entity_id, deadline_at=datetime(2026, 8, 28, 12, tzinfo=UTC)),
        ActionContext(ActorType.USER, update_turn.source),
    )

    current = await session.get(Task, task.entity_id)
    assert current.deadline_at.day == 28
    events = (
        (
            await session.execute(
                select(Event).where(Event.entity_id == task.entity_id).order_by(Event.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 2
    update_event = events[-1]
    assert update_event.source_id == update_turn.source.id
    assert update_event.changes == [
        {
            "field": "deadline_at",
            "old": "2026-08-30 12:00:00+00:00",
            "new": "2026-08-28 12:00:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_correction_and_undo_append_history_instead_of_rewriting_it(session) -> None:
    first = await user_turn(session, "Дедлайн 30-го")
    service = WorldStateService(session)
    task = await service.create_task(
        TaskCreate(title="Подать документы", deadline_at=datetime(2026, 8, 30, tzinfo=UTC)),
        ActionContext(ActorType.USER, first.source),
    )
    correction = await user_turn(session, "Нет, я имел в виду 28-е")
    await service.update_task(
        TaskUpdate(entity_id=task.entity_id, deadline_at=datetime(2026, 8, 28, tzinfo=UTC)),
        ActionContext(ActorType.USER, correction.source),
    )
    correction_event = (
        (
            await session.execute(
                select(Event)
                .where(Event.entity_id == task.entity_id, Event.event_type == "TASK_UPDATED")
                .order_by(Event.created_at.desc())
            )
        )
        .scalars()
        .first()
    )

    undo_turn = await user_turn(session, "Верни как было до прошлого сообщения")
    undo_event = await service.undo_last_change(
        ActionContext(ActorType.USER, undo_turn.source), entity_id=task.entity_id
    )

    current = await session.get(Task, task.entity_id)
    assert current.deadline_at.day == 30
    assert undo_event.reverses_event_id == correction_event.id
    all_events = (
        (await session.execute(select(Event).where(Event.entity_id == task.entity_id)))
        .scalars()
        .all()
    )
    assert correction_event in all_events
    assert undo_event in all_events
    assert len(all_events) == 3


@pytest.mark.asyncio
async def test_decision_supersede_updates_old_decision_and_entity_in_one_operation(session) -> None:
    turn = await user_turn(session, "Выбираем PostgreSQL")
    service = WorldStateService(session)
    old = await service.create_decision(
        DecisionCreate(
            title="Основное хранилище",
            chosen_option="PostgreSQL",
            reasoning_summary="Строгие связи и транзакции",
            alternatives=[DecisionAlternativeCreate(option_text="SQLite")],
        ),
        ActionContext(ActorType.USER, turn.source),
    )

    revise = await user_turn(session, "Пересмотрели: используем другой вариант")
    new = await service.create_decision(
        DecisionCreate(
            title="Основное хранилище",
            chosen_option="PostgreSQL + read replica",
            reasoning_summary="Нужны фоновые процессы",
            supersedes_decision_id=old.entity_id,
            alternatives=[DecisionAlternativeCreate(option_text="PostgreSQL single-node")],
        ),
        ActionContext(ActorType.USER, revise.source),
    )

    old_row = await session.get(Decision, old.entity_id)
    old_entity = await session.get(Entity, old.entity_id)
    assert old_row.status == DecisionStatus.SUPERSEDED
    assert old_entity.superseded_by_entity_id == new.entity_id
    supersede_event = (
        await session.execute(
            select(Event).where(
                Event.entity_id == old.entity_id,
                Event.event_type == "DECISION_SUPERSEDED",
            )
        )
    ).scalar_one()
    new_event = (
        await session.execute(
            select(Event).where(
                Event.entity_id == new.entity_id,
                Event.event_type == "DECISION_CREATED",
            )
        )
    ).scalar_one()
    assert supersede_event.operation_id == new_event.operation_id


@pytest.mark.asyncio
async def test_strict_relations_and_controlled_semantic_relation(session) -> None:
    turn = await user_turn(session, "Свяжи сущности")
    ctx = ActionContext(ActorType.USER, turn.source)
    service = WorldStateService(session)
    goal = await service.create_goal(GoalCreate(title="Выпустить v0.1"), ctx)
    project = await service.create_project(ProjectCreate(name="Elowyn"), ctx)
    prerequisite = await service.create_task(TaskCreate(title="Схема"), ctx)
    parent = await service.create_task(TaskCreate(title="Backend"), ctx)
    task = await service.create_task(
        TaskCreate(
            title="Acceptance tests",
            parent_task_id=parent.entity_id,
            primary_project_id=project.entity_id,
            goal_ids=[goal.entity_id],
            prerequisite_task_ids=[prerequisite.entity_id],
        ),
        ctx,
    )
    relation = await service.create_relation(
        EntityRelationCreate(
            source_entity_id=task.entity_id,
            target_entity_id=goal.entity_id,
            relation_type=RelationType.SUPPORTS,
        ),
        ctx,
    )

    assert await session.get(TaskGoalLink, (task.entity_id, goal.entity_id)) is not None
    assert await session.get(TaskDependency, (prerequisite.entity_id, task.entity_id)) is not None
    assert relation.relation_type == RelationType.SUPPORTS
    assert (
        await session.execute(select(EntityRelation).where(EntityRelation.id == relation.id))
    ).scalar_one() is relation

    with pytest.raises(ValidationError):
        EntityRelationCreate(
            source_entity_id=task.entity_id,
            target_entity_id=goal.entity_id,
            relation_type="MADE_UP",
        )


@pytest.mark.asyncio
async def test_assistant_assessment_has_own_source_and_user_correction_replaces_provenance(
    session,
) -> None:
    turn = await user_turn(session, "Нужно закончить тесты")
    service = WorldStateService(session)
    task = await service.create_task(
        TaskCreate(title="Закончить acceptance tests"),
        ActionContext(ActorType.USER, turn.source),
    )
    await service.assess_task(
        TaskAssessment(
            entity_id=task.entity_id,
            importance=4,
            estimated_duration_minutes=90,
            confidence=0.8,
            reason_summary="Нужно для завершения v0.1",
        ),
        evidence_source=turn.source,
    )
    assessed = await session.get(Task, task.entity_id)
    inference = await session.get(Source, assessed.importance_source_id)
    assert inference.source_type == SourceType.ASSISTANT_INFERENCE
    assert inference.confidence == 0.8
    assert assessed.estimate_source_id == inference.id
    dependency = await session.get(SourceDependency, (inference.id, turn.source.id))
    assert dependency is not None

    correction = await user_turn(session, "Нет, важность 5")
    await service.update_task(
        TaskUpdate(entity_id=task.entity_id, importance=5),
        ActionContext(ActorType.USER, correction.source),
    )
    corrected = await session.get(Task, task.entity_id)
    assert corrected.importance == 5
    assert corrected.importance_source_id == correction.source.id


@pytest.mark.asyncio
async def test_invalid_reference_rolls_back_domain_action_without_event_or_operation(
    session,
) -> None:
    turn = await user_turn(session, "Создай задачу в несуществующем проекте")
    service = WorldStateService(session)
    before_events = (await session.execute(select(Event))).scalars().all()
    before_operations = (await session.execute(select(Operation))).scalars().all()
    before_entities = (await session.execute(select(Entity))).scalars().all()

    with pytest.raises(EntityNotFoundError):
        await service.create_task(
            TaskCreate(title="Невалидная", primary_project_id=uuid4()),
            ActionContext(ActorType.USER, turn.source),
        )

    assert (await session.execute(select(Event))).scalars().all() == before_events
    assert (await session.execute(select(Operation))).scalars().all() == before_operations
    assert (await session.execute(select(Entity))).scalars().all() == before_entities


@pytest.mark.asyncio
async def test_parent_and_dependency_cycles_are_rejected_without_domain_event(session) -> None:
    turn = await user_turn(session, "Структура задач")
    service = WorldStateService(session)
    ctx = ActionContext(ActorType.USER, turn.source)
    a = await service.create_task(TaskCreate(title="A"), ctx)
    b = await service.create_task(TaskCreate(title="B", parent_task_id=a.entity_id), ctx)

    events_before = len((await session.execute(select(Event))).scalars().all())
    with pytest.raises(DomainValidationError):
        await service.update_task(
            TaskUpdate(entity_id=a.entity_id, parent_task_id=b.entity_id), ctx
        )
    assert len((await session.execute(select(Event))).scalars().all()) == events_before

    await service.add_task_dependency(
        TaskDependencyCreate(prerequisite_task_id=a.entity_id, dependent_task_id=b.entity_id), ctx
    )
    events_before = len((await session.execute(select(Event))).scalars().all())
    with pytest.raises(DomainValidationError):
        await service.add_task_dependency(
            TaskDependencyCreate(prerequisite_task_id=b.entity_id, dependent_task_id=a.entity_id),
            ctx,
        )
    assert len((await session.execute(select(Event))).scalars().all()) == events_before


@pytest.mark.asyncio
async def test_query_snapshot_is_current_state_and_hides_superseded_decision(session) -> None:
    turn = await user_turn(session, "Контекст")
    service = WorldStateService(session)
    ctx = ActionContext(ActorType.USER, turn.source)
    project = await service.create_project(ProjectCreate(name="Elowyn"), ctx)
    goal = await service.create_goal(
        GoalCreate(
            title="Рабочая v0.1",
            success_criteria=[SuccessCriterionCreate(description="Acceptance 1-10 green")],
        ),
        ctx,
    )
    task = await service.create_task(
        TaskCreate(
            title="Подключить runtime",
            primary_project_id=project.entity_id,
            goal_ids=[goal.entity_id],
        ),
        ctx,
    )
    old = await service.create_decision(
        DecisionCreate(title="Runtime", chosen_option="A", reasoning_summary="first"), ctx
    )
    new = await service.create_decision(
        DecisionCreate(
            title="Runtime",
            chosen_option="B",
            reasoning_summary="revised",
            supersedes_decision_id=old.entity_id,
            alternatives=[
                DecisionAlternativeCreate(option_text="A", rejection_summary="less reliable")
            ],
        ),
        ctx,
    )

    snapshot = await WorldStateQueryService(session).snapshot()
    assert {item["entity_id"] for item in snapshot["tasks"]} == {str(task.entity_id)}
    assert {item["entity_id"] for item in snapshot["projects"]} == {str(project.entity_id)}
    assert {item["entity_id"] for item in snapshot["goals"]} == {str(goal.entity_id)}
    assert snapshot["goals"][0]["success_criteria"][0]["description"] == "Acceptance 1-10 green"
    assert {item["entity_id"] for item in snapshot["decisions"]} == {str(new.entity_id)}
    assert snapshot["decisions"][0]["alternatives"] == [
        {
            "alternative_id": snapshot["decisions"][0]["alternatives"][0]["alternative_id"],
            "option_text": "A",
            "rejection_summary": "less reliable",
        }
    ]


@pytest.mark.asyncio
async def test_turn_operation_id_groups_multiple_domain_actions(session) -> None:
    turn = await user_turn(session, "Создай проект, цель и задачу")
    operation_id = uuid4()
    ctx = ActionContext(
        ActorType.USER,
        turn.source,
        description="Natural-language user turn",
        operation_id=operation_id,
    )
    service = WorldStateService(session)

    await service.create_project(ProjectCreate(name="Elowyn"), ctx)
    await service.create_goal(GoalCreate(title="Рабочая v0.1"), ctx)
    await service.create_task(TaskCreate(title="Подключить runtime"), ctx)

    operations = list((await session.execute(select(Operation))).scalars())
    events = list((await session.execute(select(Event))).scalars())
    assert [operation.id for operation in operations] == [operation_id]
    assert len(events) == 3
    assert {event.operation_id for event in events} == {operation_id}


@pytest.mark.asyncio
async def test_project_goal_and_semantic_relation_inferences_keep_provenance(session) -> None:
    turn = await user_turn(session, "Контекст для оценок")
    service = WorldStateService(session)
    ctx = ActionContext(ActorType.USER, turn.source)
    project = await service.create_project(ProjectCreate(name="Elowyn"), ctx)
    goal = await service.create_goal(GoalCreate(title="Рабочая v0.1"), ctx)

    await service.assess_project(
        ProjectAssessment(
            entity_id=project.entity_id,
            importance=5,
            confidence=0.9,
            reason_summary="Ключевой текущий проект",
        ),
        evidence_source=turn.source,
    )
    await service.assess_goal(
        GoalAssessment(
            entity_id=goal.entity_id,
            importance=5,
            confidence=0.85,
            reason_summary="Главный критерий готовности",
        ),
        evidence_source=turn.source,
    )
    relation = await service.infer_relation(
        EntityRelationInference(
            source_entity_id=project.entity_id,
            target_entity_id=goal.entity_id,
            relation_type=RelationType.SUPPORTS,
            confidence=0.75,
            reason_summary="Проект напрямую поддерживает цель",
        ),
        evidence_source=turn.source,
    )

    project_source = await session.get(Source, project.importance_source_id)
    goal_source = await session.get(Source, goal.importance_source_id)
    relation_source = await session.get(Source, relation.source_id)
    assert project_source.source_type == SourceType.ASSISTANT_INFERENCE
    assert goal_source.source_type == SourceType.ASSISTANT_INFERENCE
    assert relation_source.source_type == SourceType.ASSISTANT_INFERENCE
    assert relation.confidence == 0.75
    assert await session.get(SourceDependency, (relation_source.id, turn.source.id)) is not None


@pytest.mark.asyncio
async def test_success_criterion_assessment_user_correction_and_undo_keep_history(session) -> None:
    turn = await user_turn(session, "Acceptance должен пройти")
    service = WorldStateService(session)
    goal = await service.create_goal(
        GoalCreate(
            title="Рабочая v0.1",
            success_criteria=[SuccessCriterionCreate(description="Acceptance 1-10 green")],
        ),
        ActionContext(ActorType.USER, turn.source),
    )
    criterion = (
        await session.execute(
            select(SuccessCriterion).where(SuccessCriterion.goal_id == goal.entity_id)
        )
    ).scalar_one()

    await service.assess_success_criterion(
        SuccessCriterionAssessment(
            criterion_id=criterion.id,
            status=SuccessCriterionStatus.NOT_MET,
            confidence=0.8,
            evaluation_summary="PostgreSQL suite ещё не прогнана",
            reason_summary="В окружении нет PostgreSQL",
        ),
        evidence_source=turn.source,
    )
    inference_source = await session.get(Source, criterion.evaluation_source_id)
    assert inference_source.source_type == SourceType.ASSISTANT_INFERENCE
    assert criterion.status == SuccessCriterionStatus.NOT_MET

    correction_turn = await user_turn(session, "Нет, критерий уже выполнен")
    await service.update_success_criterion(
        SuccessCriterionUpdate(
            criterion_id=criterion.id,
            status=SuccessCriterionStatus.MET,
            confidence=None,
            evaluation_summary="Пользователь подтвердил выполнение",
        ),
        ActionContext(ActorType.USER, correction_turn.source),
    )
    assert criterion.status == SuccessCriterionStatus.MET
    assert criterion.evaluation_source_id == correction_turn.source.id

    correction_event = (
        await session.execute(
            select(Event)
            .where(Event.event_type == "SUCCESS_CRITERION_UPDATED")
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(1)
        )
    ).scalar_one()

    undo_turn = await user_turn(session, "Верни прошлую оценку критерия")
    undo_event = await service.undo_last_change(
        ActionContext(ActorType.USER, undo_turn.source), entity_id=goal.entity_id
    )
    assert undo_event.reverses_event_id == correction_event.id
    assert criterion.status == SuccessCriterionStatus.NOT_MET
    assert criterion.evaluation_source_id == inference_source.id
    assert await session.get(Event, correction_event.id) is not None


@pytest.mark.asyncio
async def test_project_summary_is_derived_cache_and_domain_events_invalidate_it(session) -> None:
    turn = await user_turn(session, "Проект Elowyn")
    service = WorldStateService(session)
    ctx = ActionContext(ActorType.USER, turn.source)
    project = await service.create_project(ProjectCreate(name="Elowyn"), ctx)
    events_before_cache = len((await session.execute(select(Event))).scalars().all())

    await service.cache_project_summary(
        ProjectSummaryCacheUpdate(
            entity_id=project.entity_id,
            summary="Сейчас готовим вертикальный runtime.",
        )
    )
    assert project.current_summary == "Сейчас готовим вертикальный runtime."
    assert project.current_summary_updated_at is not None
    assert len((await session.execute(select(Event))).scalars().all()) == events_before_cache

    await service.create_task(
        TaskCreate(title="Прогнать acceptance", primary_project_id=project.entity_id),
        ctx,
    )
    assert project.current_summary is None
    assert project.current_summary_updated_at is None
