from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import elowyn.runtime as runtime_module
from elowyn.db.base import Base
from elowyn.db.models import (
    Event,
    Goal,
    PlanGoalLink,
    PlanItemProgress,
    PlanVersion,
    PlanVersionItem,
)
from elowyn.domain.enums import (
    EventType,
    PlanItemProgressStatus,
    PlanVersionStatus,
    TransportType,
)
from elowyn.domain.messages import IncomingMessage
from elowyn.runtime import ElowynRuntime
from elowyn.services.planning_query import PlanningQueryService
from elowyn.support.database_safety import assert_test_database_url
from elowyn.transport.telegram import TelegramAdapter, build_router

pytestmark = pytest.mark.postgres
# Presentation binding compares domain timestamps with the persisted server time.
# Keep synthetic turns safely in the future so a slow full suite cannot cross them.
_TEST_START = datetime.now(UTC) + timedelta(days=3650)


@pytest.fixture
async def session_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for Telegram Planning runtime tests")
    assert_test_database_url(url)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
    await engine.dispose()


def _has_tool_return(messages) -> bool:
    return any(
        getattr(part, "part_kind", None) == "tool-return"
        for message in messages
        for part in message.parts
    )


def _candidate_model() -> FunctionModel:
    def model_function(messages, info):
        if not _has_tool_return(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="create_plan_with_candidate",
                        args={
                            "plan": {"title": "Прочитать книгу за 14 дней"},
                            "candidate": {
                                "summary": "Четырнадцатидневный план",
                                "proposed_strategy_snapshot": "Читать ежедневно",
                                "items": [
                                    {"ordinal": day, "title": f"Чтение: день {day}"}
                                    for day in range(1, 15)
                                ],
                            },
                        },
                    )
                ]
            )
        placeholder = next(
            part.content["presentation_placeholder"]
            for message in messages
            for part in message.parts
            if isinstance(getattr(part, "content", None), dict)
            and "presentation_placeholder" in part.content
        )
        return ModelResponse(parts=[TextPart(placeholder)])

    return FunctionModel(model_function)


def _approval_model() -> FunctionModel:
    def model_function(messages, info):
        if not _has_tool_return(messages):
            return ModelResponse(
                parts=[ToolCallPart(tool_name="approve_presented_candidate", args={})]
            )
        return ModelResponse(parts=[TextPart("План утверждён.")])

    return FunctionModel(model_function)


def _one_tool_model(name: str, args: dict[str, object]) -> FunctionModel:
    def model_function(messages, info):
        if not _has_tool_return(messages):
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart("Готово.")])

    return FunctionModel(model_function)


def _revision_model(
    *,
    plan_id,
    based_on_version_id,
    summary: str,
    item_title: str,
) -> FunctionModel:
    def model_function(messages, info):
        if not _has_tool_return(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="present_candidate_plan",
                        args={
                            "plan_id": str(plan_id),
                            "based_on_version_id": str(based_on_version_id),
                            "summary": summary,
                            "proposed_strategy_snapshot": summary,
                            "items": [{"ordinal": 1, "title": item_title}],
                        },
                    )
                ]
            )
        placeholder = next(
            part.content["presentation_placeholder"]
            for message in messages
            for part in message.parts
            if isinstance(getattr(part, "content", None), dict)
            and "presentation_placeholder" in part.content
        )
        return ModelResponse(parts=[TextPart(placeholder)])

    return FunctionModel(model_function)


def _model_must_not_run() -> FunctionModel:
    def model_function(messages, info):
        raise AssertionError("deadline basis route must run before the model")

    return FunctionModel(model_function)


async def _seed_legacy_approved(factory) -> tuple[object, object]:
    sent_at = _TEST_START
    await ElowynRuntime(session_factory=factory, model=_candidate_model()).handle_message(
        IncomingMessage(
            transport=TransportType.INTERNAL,
            external_conversation_id="telegram-seed",
            external_message_id="1",
            text="Предложи план чтения на 14 дней.",
            sent_at=sent_at,
        )
    )
    await ElowynRuntime(session_factory=factory, model=_approval_model()).handle_message(
        IncomingMessage(
            transport=TransportType.INTERNAL,
            external_conversation_id="telegram-seed",
            external_message_id="2",
            text="Да.",
            sent_at=sent_at + timedelta(minutes=1),
        )
    )
    async with factory() as session:
        version = (await session.execute(select(PlanVersion))).scalar_one()
        return version.plan_id, version.id


def _incoming(number: int, text_value: str) -> IncomingMessage:
    return IncomingMessage(
        transport=TransportType.INTERNAL,
        external_conversation_id="telegram-seed",
        external_message_id=str(number),
        text=text_value,
        sent_at=_TEST_START + timedelta(minutes=number),
    )


class TelegramMessageDouble:
    def __init__(self, *, text_value: str, sent_at: datetime) -> None:
        self.from_user = SimpleNamespace(id=777)
        self.chat = SimpleNamespace(id=987654)
        self.message_id = 300
        self.text = text_value
        self.date = sent_at
        self.answers: list[str] = []

    def model_dump(self, **kwargs) -> dict[str, object]:
        return {"message_id": self.message_id, "text": self.text}

    async def answer(self, text_value: str, *, parse_mode=None) -> None:
        assert parse_mode is None
        self.answers.append(text_value)


@pytest.mark.asyncio
async def test_exact_russian_deadline_persists_through_telegram_runtime(
    session_factory,
) -> None:
    plan_id, approved_id = await _seed_legacy_approved(session_factory)
    runtime = ElowynRuntime(session_factory=session_factory, model=_model_must_not_run())
    router = build_router(
        runtime.handle_message,
        adapter=TelegramAdapter(allowed_user_id=777),
    )
    sent_at = _TEST_START + timedelta(hours=1)
    message = TelegramMessageDouble(
        text_value="Уточняю: книгу нужно закончить через пять дней.",
        sent_at=sent_at,
    )

    await router.message.handlers[0].callback(message)

    assert len(message.answers) == 1
    assert "сохранён" in message.answers[0]
    async with session_factory() as session:
        versions = list((await session.execute(select(PlanVersion))).scalars())
        goal = (await session.execute(select(Goal))).scalar_one()
        link = (await session.execute(select(PlanGoalLink))).scalar_one()
        event_types = set((await session.execute(select(Event.event_type))).scalars())
        details = await PlanningQueryService(session).get_staleness_details(approved_id)
        assert [(version.id, version.status) for version in versions] == [
            (approved_id, PlanVersionStatus.APPROVED)
        ]
        assert goal.target_date == sent_at + timedelta(days=5)
        assert link.plan_id == plan_id
        assert link.goal_id == goal.entity_id
        assert EventType.GOAL_CREATED in event_types
        assert EventType.PLAN_GOAL_LINKED in event_types
        assert details["is_basis_stale"] is True


@pytest.mark.asyncio
async def test_telegram_never_claims_deadline_persisted_after_mutation_failure(
    session_factory,
    monkeypatch,
) -> None:
    await _seed_legacy_approved(session_factory)

    async def fail_goal_create(self, command, ctx):
        raise RuntimeError("synthetic canonical deadline failure")

    monkeypatch.setattr(runtime_module.WorldStateService, "create_goal", fail_goal_create)
    runtime = ElowynRuntime(session_factory=session_factory, model=_model_must_not_run())
    router = build_router(
        runtime.handle_message,
        adapter=TelegramAdapter(allowed_user_id=777),
    )
    message = TelegramMessageDouble(
        text_value="Уточняю: книгу нужно закончить через пять дней.",
        sent_at=_TEST_START + timedelta(hours=1),
    )

    with pytest.raises(RuntimeError, match="canonical deadline failure"):
        await router.message.handlers[0].callback(message)

    assert message.answers == []
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Goal))).scalar_one() == 0
        assert (
            await session.execute(
                select(func.count())
                .select_from(Event)
                .where(Event.event_type.in_([EventType.GOAL_CREATED, EventType.PLAN_GOAL_LINKED]))
            )
        ).scalar_one() == 0


@pytest.mark.asyncio
async def test_telegram_collaborative_next_uses_canonical_progress_aware_action(
    session_factory,
) -> None:
    plan_id, approved_id = await _seed_legacy_approved(session_factory)
    await ElowynRuntime(
        session_factory=session_factory,
        model=_one_tool_model(
            "update_approved_plan_progress",
            {"plan_id": str(plan_id), "ordinal": 1, "status": "DONE"},
        ),
    ).handle_message(_incoming(3, "Первый пункт выполнен."))
    async with session_factory() as session:
        items = list(
            (
                await session.execute(
                    select(PlanVersionItem)
                    .where(PlanVersionItem.plan_version_id == approved_id)
                    .order_by(PlanVersionItem.ordinal)
                )
            ).scalars()
        )
        before = {
            item.id: (await session.get(PlanItemProgress, item.id)).status for item in items
        }
        assert before[items[0].id] == PlanItemProgressStatus.DONE
        assert (await PlanningQueryService(session).get_next_action(plan_id)).id == items[1].id

    router = build_router(
        ElowynRuntime(
            session_factory=session_factory,
            model=_model_must_not_run(),
        ).handle_message,
        adapter=TelegramAdapter(allowed_user_id=777),
    )
    message = TelegramMessageDouble(
        text_value="Сделай следующий пункт вместе со мной.",
        sent_at=_TEST_START + timedelta(hours=1),
    )

    await router.message.handlers[0].callback(message)

    assert "Пункт 2 — «Чтение: день 2»" in "".join(message.answers)
    assert "Пункт 1" not in "".join(message.answers)
    async with session_factory() as session:
        after = {
            item.id: (await session.get(PlanItemProgress, item.id)).status for item in items
        }
        assert after == before
        version = await session.get(PlanVersion, approved_id)
        assert version is not None
        assert version.status == PlanVersionStatus.APPROVED


@pytest.mark.asyncio
async def test_telegram_rejected_history_semantically_filters_five_day_v7(
    session_factory,
) -> None:
    plan_id, approved_id = await _seed_legacy_approved(session_factory)
    for version_number in range(2, 8):
        five_days = version_number == 7
        await ElowynRuntime(
            session_factory=session_factory,
            model=_revision_model(
                plan_id=plan_id,
                based_on_version_id=approved_id,
                summary=(
                    "Сжатый вариант на 5 дней"
                    if five_days
                    else "Альтернативный вариант на 14 дней"
                ),
                item_title=(
                    "День 5: финал" if five_days else f"Исторический пункт {version_number}"
                ),
            ),
        ).handle_message(_incoming(version_number * 2, "Предложи другой вариант плана."))
        await ElowynRuntime(
            session_factory=session_factory,
            model=_model_must_not_run(),
        ).handle_message(
            _incoming(version_number * 2 + 1, "Отмени текущий предложенный вариант.")
        )
    async with session_factory() as session:
        rejected = list(
            (
                await session.execute(
                    select(PlanVersion).where(PlanVersion.status == PlanVersionStatus.REJECTED)
                )
            ).scalars()
        )
        assert [version.version_number for version in rejected] == [2, 3, 4, 5, 6, 7]
        matches = await PlanningQueryService(session).get_rejected_versions(
            plan_id,
            duration_days=5,
        )
        assert [version.version_number for version in matches] == [7]
        before_events = (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one()

    router = build_router(
        ElowynRuntime(
            session_factory=session_factory,
            model=_model_must_not_run(),
        ).handle_message,
        adapter=TelegramAdapter(allowed_user_id=777),
    )
    message = TelegramMessageDouble(
        text_value="Что стало с предыдущим отклонённым вариантом на 5 дней?",
        sent_at=_TEST_START + timedelta(hours=2),
    )

    await router.message.handlers[0].callback(message)

    response = "".join(message.answers)
    assert "Версия v7 существовала и была отклонена" in response
    assert "несколько отклонённых вариантов" not in response
    async with session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one() == before_events
