"""Executable acceptance contract for Elowyn v0.1 scenarios 1-9."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.base import Base
from elowyn.db.models import (
    Decision,
    Entity,
    Event,
    Goal,
    Message,
    Operation,
    Project,
    Source,
    SourceDependency,
    Task,
    TaskDependency,
    TaskGoalLink,
)
from elowyn.domain.commands import (
    DecisionAlternativeCreate,
    DecisionCreate,
    EntityRelationCreate,
    GoalCreate,
    ProjectCreate,
    TaskAssessment,
    TaskCreate,
    TaskUpdate,
)
from elowyn.domain.enums import (
    ActorType,
    DeadlineType,
    DecisionStatus,
    EventType,
    RelationType,
    SourceType,
    TaskStatus,
    TransportType,
)
from elowyn.domain.errors import EntityNotFoundError
from elowyn.domain.messages import IncomingMessage
from elowyn.runtime import ElowynRuntime
from elowyn.services.conversation import ConversationService
from elowyn.services.world_state import ActionContext, WorldStateService
from elowyn.transport.telegram import TelegramAdapter

pytestmark = pytest.mark.postgres

if not os.environ.get("TEST_DATABASE_URL"):
    raise RuntimeError("TEST_DATABASE_URL is required for the full acceptance suite")


@pytest.fixture
async def session_factory():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    async with engine.begin() as connection:
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def scripted_model(tool_calls, final_text="Готово."):
    call_number = 0

    def model_function(messages, info):
        nonlocal call_number
        call_number += 1
        if call_number == 1:
            calls = tool_calls(messages) if callable(tool_calls) else tool_calls
            return ModelResponse(
                parts=[ToolCallPart(tool_name=name, args=args) for name, args in calls]
            )
        return ModelResponse(parts=[TextPart(final_text)])

    return FunctionModel(model_function)


def prompt_text(messages) -> str:
    chunks: list[str] = []
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                chunks.append(content)
    return "\n".join(chunks)


def task_id_from_prompt(messages, title: str) -> str:
    text = prompt_text(messages)
    pattern = rf'"entity_id": "([0-9a-f-]+)".*?"title": "{re.escape(title)}"'
    match = re.search(pattern, text, re.S)
    assert match is not None, text
    return match.group(1)


class FakeTelegramMessage:
    def __init__(self, *, message_id: int, text: str):
        self.chat = SimpleNamespace(id=1001)
        self.message_id = message_id
        self.text = text
        self.date = datetime.now(UTC)

    def model_dump(self, **kwargs):
        return {"message_id": self.message_id, "chat": {"id": self.chat.id}, "text": self.text}


async def user_source(session, *, external_message_id: str, text: str):
    ingested = await ConversationService(session).ingest_user_message(
        IncomingMessage(
            transport=TransportType.TELEGRAM,
            external_conversation_id="acceptance-chat",
            external_message_id=external_message_id,
            text=text,
            sent_at=datetime.now(UTC),
        )
    )
    return ingested.source


async def test_acceptance_01_persistence_survives_restart_via_telegram_adapter(
    session_factory,
) -> None:
    adapter = TelegramAdapter(allowed_user_id=None)
    incoming = adapter.to_incoming(
        FakeTelegramMessage(
            message_id=1,
            text="У меня проект Elowyn, цель — рабочая v0.1, и задача подключить runtime.",
        )
    )
    create_model = scripted_model(
        [
            ("create_project", {"name": "Elowyn", "status": "ACTIVE"}),
            ("create_goal", {"title": "Рабочая v0.1"}),
            ("create_task", {"title": "Подключить runtime"}),
        ],
        final_text="Запомнила проект, цель и задачу.",
    )
    runtime = ElowynRuntime(session_factory=session_factory, model=create_model)
    assert await runtime.handle_message(incoming) == "Запомнила проект, цель и задачу."

    async with session_factory() as session:
        operation_count = (await session.execute(select(func.count(Operation.id)))).scalar_one()
        event_count = (await session.execute(select(func.count(Event.id)))).scalar_one()
        assert operation_count == 1
        assert event_count == 3

    # Simulate backend restart: a new Runtime instance and fresh DB session.
    update_model = scripted_model(
        lambda messages: [
            (
                "update_task",
                {
                    "entity_id": task_id_from_prompt(messages, "Подключить runtime"),
                    "status": "IN_PROGRESS",
                },
            )
        ],
        final_text="Отметила, что runtime уже в работе.",
    )
    restarted = ElowynRuntime(session_factory=session_factory, model=update_model)
    second = adapter.to_incoming(FakeTelegramMessage(message_id=2, text="Runtime уже делаю."))
    await restarted.handle_message(second)

    async with session_factory() as session:
        project = (
            await session.execute(select(Project).where(Project.name == "Elowyn"))
        ).scalar_one()
        goal = (
            await session.execute(select(Goal).where(Goal.title == "Рабочая v0.1"))
        ).scalar_one()
        task = (
            await session.execute(select(Task).where(Task.title == "Подключить runtime"))
        ).scalar_one()
        assert project is not None and goal is not None
        assert task.status == TaskStatus.IN_PROGRESS


async def test_acceptance_02_natural_language_update_goes_through_domain_tool(
    session_factory,
) -> None:
    async with session_factory() as session:
        source = await user_source(session, external_message_id="10", text="Создай отчёт")
        task = await WorldStateService(session).create_task(
            TaskCreate(title="Отправить отчёт", deadline_at=datetime(2026, 8, 30, tzinfo=UTC)),
            ActionContext(ActorType.USER, source),
        )
        task_id = task.entity_id
        await session.commit()

    model = scripted_model(
        lambda messages: [
            (
                "update_task",
                {
                    "entity_id": task_id_from_prompt(messages, "Отправить отчёт"),
                    "deadline_at": "2026-08-28T00:00:00Z",
                },
            )
        ],
        final_text="Перенесла дедлайн на 28 августа.",
    )
    runtime = ElowynRuntime(session_factory=session_factory, model=model)
    await runtime.handle_message(
        IncomingMessage(
            transport=TransportType.TELEGRAM,
            external_conversation_id="acceptance-chat",
            external_message_id="11",
            text="Перенеси дедлайн отчёта на 28-е.",
            sent_at=datetime.now(UTC),
        )
    )

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        assert task.deadline_at.day == 28


async def test_acceptance_03_history_and_provenance_point_to_original_message(
    session_factory,
) -> None:
    async with session_factory() as session:
        source = await user_source(session, external_message_id="20", text="Дедлайн 30-го")
        service = WorldStateService(session)
        task = await service.create_task(
            TaskCreate(
                title="Подать документы",
                deadline_at=datetime(2026, 8, 30, tzinfo=UTC),
                deadline_type=DeadlineType.HARD,
            ),
            ActionContext(ActorType.USER, source),
        )
        change_source = await user_source(session, external_message_id="21", text="Теперь 28-го")
        await service.update_task(
            TaskUpdate(entity_id=task.entity_id, deadline_at=datetime(2026, 8, 28, tzinfo=UTC)),
            ActionContext(ActorType.USER, change_source),
        )
        await session.commit()

        event = (
            (
                await session.execute(
                    select(Event)
                    .where(
                        Event.entity_id == task.entity_id,
                        Event.event_type == EventType.TASK_UPDATED,
                    )
                    .order_by(Event.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        source_row = await session.get(Source, event.source_id)
        message = await session.get(Message, source_row.message_id)
        assert event.changes[0]["old"].startswith("2026-08-30")
        assert event.changes[0]["new"].startswith("2026-08-28")
        assert message.external_message_id == "21"
        assert message.text == "Теперь 28-го"


async def test_acceptance_04_correction_creates_new_event_and_preserves_previous(
    session_factory,
) -> None:
    async with session_factory() as session:
        source = await user_source(session, external_message_id="30", text="Дедлайн 30-го")
        service = WorldStateService(session)
        task = await service.create_task(
            TaskCreate(title="Оплата", deadline_at=datetime(2026, 8, 30, tzinfo=UTC)),
            ActionContext(ActorType.USER, source),
        )
        first = await user_source(session, external_message_id="31", text="Перенеси на 29-е")
        await service.update_task(
            TaskUpdate(entity_id=task.entity_id, deadline_at=datetime(2026, 8, 29, tzinfo=UTC)),
            ActionContext(ActorType.USER, first),
        )
        second = await user_source(
            session, external_message_id="32", text="Нет, я имел в виду 28-е"
        )
        await service.update_task(
            TaskUpdate(entity_id=task.entity_id, deadline_at=datetime(2026, 8, 28, tzinfo=UTC)),
            ActionContext(ActorType.USER, second),
        )
        await session.commit()

        events = (
            (
                await session.execute(
                    select(Event)
                    .where(
                        Event.entity_id == task.entity_id,
                        Event.event_type == EventType.TASK_UPDATED,
                    )
                    .order_by(Event.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2
        assert events[0].changes[0]["new"].startswith("2026-08-29")
        assert events[1].changes[0]["new"].startswith("2026-08-28")


async def test_acceptance_05_undo_writes_inverse_event_without_deleting_history(
    session_factory,
) -> None:
    async with session_factory() as session:
        source = await user_source(session, external_message_id="40", text="Дедлайн 30-го")
        service = WorldStateService(session)
        task = await service.create_task(
            TaskCreate(title="Счёт", deadline_at=datetime(2026, 8, 30, tzinfo=UTC)),
            ActionContext(ActorType.USER, source),
        )
        changed = await user_source(session, external_message_id="41", text="Пусть будет 28-го")
        await service.update_task(
            TaskUpdate(entity_id=task.entity_id, deadline_at=datetime(2026, 8, 28, tzinfo=UTC)),
            ActionContext(ActorType.USER, changed),
        )
        target = (
            (
                await session.execute(
                    select(Event)
                    .where(
                        Event.entity_id == task.entity_id,
                        Event.event_type == EventType.TASK_UPDATED,
                    )
                    .order_by(Event.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        undo_source = await user_source(
            session, external_message_id="42", text="Верни как было до прошлого сообщения"
        )
        undo = await service.undo_last_change(
            ActionContext(ActorType.USER, undo_source), entity_id=task.entity_id
        )
        await session.commit()

        task = await session.get(Task, task.entity_id)
        assert task.deadline_at.day == 30
        assert undo.event_type == EventType.UNDO_APPLIED
        assert undo.reverses_event_id == target.id
        assert await session.get(Event, target.id) is not None


async def test_acceptance_06_decision_lifecycle_supersedes_old_decision(session_factory) -> None:
    async with session_factory() as session:
        source = await user_source(session, external_message_id="50", text="Берём вариант A")
        service = WorldStateService(session)
        old = await service.create_decision(
            DecisionCreate(
                title="Выбор runtime",
                chosen_option="A",
                reasoning_summary="Проще",
                alternatives=[DecisionAlternativeCreate(option_text="B")],
            ),
            ActionContext(ActorType.USER, source),
        )
        revised_source = await user_source(
            session, external_message_id="51", text="Пересматриваем на B"
        )
        new = await service.create_decision(
            DecisionCreate(
                title="Выбор runtime",
                chosen_option="B",
                reasoning_summary="Надёжнее",
                supersedes_decision_id=old.entity_id,
                alternatives=[
                    DecisionAlternativeCreate(option_text="A", rejection_summary="Ограничен")
                ],
            ),
            ActionContext(ActorType.USER, revised_source),
        )
        await session.commit()

        old = await session.get(Decision, old.entity_id)
        old_entity = await session.get(Entity, old.entity_id)
        assert old.status == DecisionStatus.SUPERSEDED
        assert old_entity.superseded_by_entity_id == new.entity_id
        assert (
            await session.execute(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.entity_id == old.entity_id,
                    Event.event_type == EventType.DECISION_SUPERSEDED,
                )
            )
        ).scalar_one() == 1


async def test_acceptance_07_strict_and_semantic_relations(session_factory) -> None:
    async with session_factory() as session:
        source = await user_source(session, external_message_id="60", text="Связи")
        ctx = ActionContext(ActorType.USER, source)
        service = WorldStateService(session)
        goal_a = await service.create_goal(GoalCreate(title="Goal A"), ctx)
        goal_b = await service.create_goal(GoalCreate(title="Goal B"), ctx)
        project = await service.create_project(ProjectCreate(name="Project A"), ctx)
        parent = await service.create_task(TaskCreate(title="Parent"), ctx)
        prerequisite = await service.create_task(TaskCreate(title="Prerequisite"), ctx)
        task = await service.create_task(
            TaskCreate(
                title="Child",
                parent_task_id=parent.entity_id,
                primary_project_id=project.entity_id,
                goal_ids=[goal_a.entity_id, goal_b.entity_id],
                prerequisite_task_ids=[prerequisite.entity_id],
            ),
            ctx,
        )
        relation = await service.create_relation(
            EntityRelationCreate(
                source_entity_id=task.entity_id,
                target_entity_id=project.entity_id,
                relation_type=RelationType.RELATED_TO,
            ),
            ctx,
        )
        await session.commit()

        links = (
            (
                await session.execute(
                    select(TaskGoalLink).where(TaskGoalLink.task_id == task.entity_id)
                )
            )
            .scalars()
            .all()
        )
        assert {link.goal_id for link in links} == {goal_a.entity_id, goal_b.entity_id}
        assert (
            await session.get(TaskDependency, (prerequisite.entity_id, task.entity_id)) is not None
        )
        assert relation.relation_type == RelationType.RELATED_TO
        with pytest.raises(ValidationError):
            EntityRelationCreate(
                source_entity_id=task.entity_id,
                target_entity_id=project.entity_id,
                relation_type="ARBITRARY_RELATION",
            )


async def test_acceptance_08_assistant_inference_has_confidence_and_user_correction_wins(
    session_factory,
) -> None:
    async with session_factory() as session:
        source = await user_source(session, external_message_id="70", text="Новая задача")
        service = WorldStateService(session)
        task = await service.create_task(
            TaskCreate(title="Проверить миграции"),
            ActionContext(ActorType.USER, source),
        )
        await service.assess_task(
            TaskAssessment(
                entity_id=task.entity_id,
                importance=3,
                estimated_duration_minutes=45,
                confidence=0.7,
                reason_summary="Оценка по объёму текущего scaffold",
            ),
            evidence_source=source,
        )
        assessed = await session.get(Task, task.entity_id)
        inference_source = await session.get(Source, assessed.importance_source_id)
        assert inference_source.source_type == SourceType.ASSISTANT_INFERENCE
        assert inference_source.confidence == 0.7
        assert await session.get(SourceDependency, (inference_source.id, source.id)) is not None

        correction = await user_source(session, external_message_id="71", text="Важность 5")
        await service.update_task(
            TaskUpdate(entity_id=task.entity_id, importance=5),
            ActionContext(ActorType.USER, correction),
        )
        await session.commit()
        corrected = await session.get(Task, task.entity_id)
        assert corrected.importance == 5
        assert corrected.importance_source_id == correction.id


async def test_acceptance_09_invalid_domain_command_changes_nothing_and_writes_no_event(
    session_factory,
) -> None:
    async with session_factory() as session:
        source = await user_source(session, external_message_id="80", text="Невалидная ссылка")
        service = WorldStateService(session)
        before_entities = (
            await session.execute(select(func.count()).select_from(Entity))
        ).scalar_one()
        before_events = (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one()
        before_operations = (
            await session.execute(select(func.count()).select_from(Operation))
        ).scalar_one()

        with pytest.raises(EntityNotFoundError):
            await service.create_task(
                TaskCreate(title="Не должна сохраниться", primary_project_id=uuid4()),
                ActionContext(ActorType.USER, source),
            )
        await session.commit()

        assert (
            await session.execute(select(func.count()).select_from(Entity))
        ).scalar_one() == before_entities
        assert (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one() == before_events
        assert (
            await session.execute(select(func.count()).select_from(Operation))
        ).scalar_one() == before_operations
