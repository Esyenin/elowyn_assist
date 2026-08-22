"""Real NVIDIA model cases over an isolated PostgreSQL test database using synthetic data."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.models import Event, Source, Task
from elowyn.domain.commands import TaskCreate
from elowyn.domain.enums import ActorType, EventType, SourceType, TransportType
from elowyn.domain.messages import IncomingMessage
from elowyn.provider import build_runtime_model
from elowyn.runtime import ElowynRuntime
from elowyn.services.world_state import ActionContext, WorldStateService
from elowyn.support.consistency import ConsistencyVerifier
from elowyn.support.database_safety import assert_test_database_url


class CaseFailure(RuntimeError):
    """Safe synthetic-case diagnostic with no provider or credential content."""


def incoming(conversation: str, sequence: int, text: str) -> IncomingMessage:
    return IncomingMessage(
        transport=TransportType.INTERNAL,
        external_conversation_id=conversation,
        external_message_id=f"synthetic-{sequence}",
        text=text,
        sent_at=datetime.now(UTC),
    )


async def task_count(factory: async_sessionmaker) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(Task)) or 0


async def event_count(factory: async_sessionmaker) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(Event)) or 0


async def find_task(factory: async_sessionmaker, text: str) -> Task:
    async with factory() as session:
        tasks = list((await session.execute(select(Task))).scalars().all())
        matches = [task for task in tasks if text.casefold() in task.title.casefold()]
        if len(matches) != 1:
            raise CaseFailure("expected synthetic task was not found")
        task = matches[0]
        session.expunge(task)
        return task


async def run_cases() -> None:
    database_url = os.environ["DATABASE_URL"]
    assert_test_database_url(database_url)
    engine = create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    runtime = ElowynRuntime(session_factory=factory, model=build_runtime_model())
    conversation = "nvidia-db-cases"
    today = datetime.now(UTC).date()

    try:
        before = await task_count(factory)
        await runtime.handle_message(
            incoming(conversation, 1, "Создай задачу купить молоко завтра")
        )
        milk = await find_task(factory, "купить молоко")
        if await task_count(factory) != before + 1:
            raise CaseFailure("case A did not create exactly one Task")
        if milk.deadline_at is None or milk.deadline_at.date() != today + timedelta(days=1):
            raise CaseFailure("case A produced an incorrect relative deadline")
        print("case A passed: create_task")

        await runtime.handle_message(
            incoming(
                conversation,
                2,
                "Измени существующую задачу «купить молоко»: новое название "
                "«купить молоко и хлеб». Не создавай новую задачу.",
            )
        )
        updated = await find_task(factory, "купить молоко и хлеб")
        if updated.entity_id != milk.entity_id or await task_count(factory) != before + 1:
            raise CaseFailure("case B duplicated the Task instead of updating it")
        print("case B passed: update without duplicate")

        async with factory() as session:
            source = Source(
                source_type=SourceType.SYSTEM, reason_summary="synthetic ambiguity seed"
            )
            session.add(source)
            await session.flush()
            service = WorldStateService(session)
            context = ActionContext(ActorType.SYSTEM, source)
            await service.create_task(TaskCreate(title="Подготовить отчёт Альфа"), context)
            await service.create_task(TaskCreate(title="Подготовить отчёт Бета"), context)
            await session.commit()

        before_ambiguous = await event_count(factory)
        response = await runtime.handle_message(
            incoming(conversation, 3, "Отметь задачу «Подготовить отчёт» выполненной.")
        )
        if await event_count(factory) != before_ambiguous:
            raise CaseFailure("case C mutated World State for an ambiguous reference")
        if not response or "?" not in response:
            raise CaseFailure("case C did not return a clarification question")
        print("case C passed: clarification without mutation")

        before_correction = await event_count(factory)
        await runtime.handle_message(
            incoming(
                conversation,
                4,
                "Исправь задачу «купить молоко и хлеб»: дедлайн должен быть послезавтра, "
                "а не завтра.",
            )
        )
        corrected = await find_task(factory, "купить молоко и хлеб")
        if corrected.deadline_at is None or corrected.deadline_at.date() != today + timedelta(
            days=2
        ):
            raise CaseFailure("case D did not apply the correction")
        async with factory() as session:
            correction_event = (
                await session.execute(
                    select(Event)
                    .where(Event.entity_id == corrected.entity_id)
                    .order_by(Event.created_at.desc(), Event.id.desc())
                    .limit(1)
                )
            ).scalar_one()
            correction_source = await session.get(Source, correction_event.source_id)
            if (
                correction_source is None
                or correction_source.source_type != SourceType.USER_MESSAGE
            ):
                raise CaseFailure("case D correction lacks a new user Source")
        if await event_count(factory) <= before_correction:
            raise CaseFailure("case D did not append a correction Event")
        print("case D passed: correction with new Event/Source")

        await runtime.handle_message(
            incoming(conversation, 5, "Отмени последнее изменение задачи «купить молоко и хлеб».")
        )
        undone = await find_task(factory, "купить молоко и хлеб")
        if undone.deadline_at is None or undone.deadline_at.date() != today + timedelta(days=1):
            raise CaseFailure("case E did not restore the previous deadline")
        async with factory() as session:
            undo_event = (
                await session.execute(
                    select(Event)
                    .where(
                        Event.entity_id == undone.entity_id,
                        Event.event_type == EventType.UNDO_APPLIED,
                    )
                    .order_by(Event.created_at.desc(), Event.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if undo_event is None or undo_event.reverses_event_id != correction_event.id:
                raise CaseFailure("case E lacks the expected inverse history link")
            if await session.get(Event, correction_event.id) is None:
                raise CaseFailure("case E removed original history")
        print("case E passed: inverse undo with preserved history")

        async with factory() as session:
            (await ConsistencyVerifier(session).verify()).require_ok()
        print("synthetic NVIDIA DB consistency passed")
    finally:
        await engine.dispose()


async def main() -> int:
    try:
        await run_cases()
    except CaseFailure as exc:
        print(f"NVIDIA DB case assertion failed: {exc}")
        return 1
    except Exception as exc:
        print(f"NVIDIA DB cases failed safely: {type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
