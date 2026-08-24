from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import elowyn.runtime as runtime_module
from elowyn.assistant.context import BoundedMemoryContext
from elowyn.db.base import Base
from elowyn.db.models import (
    Event,
    Goal,
    Message,
    Plan,
    PlanGoalLink,
    PlanItemProgress,
    PlanVersion,
    PlanVersionItem,
    PlanVersionPresentation,
    Project,
    Source,
    Strategy,
    Task,
)
from elowyn.domain.enums import (
    EventType,
    MessageAuthor,
    PlanItemProgressStatus,
    PlanVersionStatus,
    TaskStatus,
    TransportType,
)
from elowyn.domain.messages import IncomingMessage
from elowyn.runtime import ElowynRuntime
from elowyn.support.consistency import ConsistencyVerifier
from elowyn.support.database_safety import assert_test_database_url

pytestmark = pytest.mark.postgres


@pytest.fixture
async def session_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for Planning progress runtime tests")
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


def incoming(number: int, value: str) -> IncomingMessage:
    return IncomingMessage(
        transport=TransportType.INTERNAL,
        external_conversation_id="planning-progress-runtime",
        external_message_id=str(number),
        text=value,
        sent_at=datetime.now(UTC),
    )


def tool_returns(messages) -> list[dict]:
    result = []
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, dict):
                result.append(content)
    return result


def has_tool_return(messages) -> bool:
    return any(
        getattr(part, "part_kind", None) == "tool-return"
        for message in messages
        for part in message.parts
    )


def candidate_model(
    *,
    items: list[dict],
    dependencies: list[dict] | None = None,
    plan_id: UUID | None = None,
    based_on_version_id: UUID | None = None,
    summary: str = "Рабочий план",
    strategy: str = "Выполнять последовательно",
    rationale: str | None = None,
    strategy_rationale: str | None = None,
    basis: list[dict] | None = None,
) -> FunctionModel:
    def model_function(messages, info):
        if not has_tool_return(messages):
            candidate = {
                "summary": summary,
                "rationale": rationale,
                "proposed_strategy_snapshot": strategy,
                "strategy_rationale_snapshot": strategy_rationale,
                "items": items,
                "dependencies": dependencies or [],
                "basis": basis or [],
            }
            if based_on_version_id is not None:
                candidate["based_on_version_id"] = str(based_on_version_id)
            if plan_id is None:
                args = {"plan": {"title": "Рабочий план"}, "candidate": candidate}
                name = "create_plan_with_candidate"
            else:
                args = {"plan_id": str(plan_id), **candidate}
                name = "present_candidate_plan"
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        placeholder = next(
            value["presentation_placeholder"]
            for value in tool_returns(messages)
            if "presentation_placeholder" in value
        )
        return ModelResponse(parts=[TextPart(placeholder)])

    return FunctionModel(model_function)


def one_tool_model(name: str, args: dict, response: str = "Готово.") -> FunctionModel:
    def model_function(messages, info):
        if not has_tool_return(messages):
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart(response)])

    return FunctionModel(model_function)


def inspected_tool_model(name: str, args: dict, response_builder) -> FunctionModel:
    def model_function(messages, info):
        returns = tool_returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart(response_builder(returns[-1]))])

    return FunctionModel(model_function)


async def seed_approved(
    factory, *, items: list[dict], dependencies=None, start_number: int = 1
):
    await ElowynRuntime(
        session_factory=factory,
        model=candidate_model(items=items, dependencies=dependencies),
    ).handle_message(incoming(start_number, "Предложи план."))
    await ElowynRuntime(
        session_factory=factory,
        model=one_tool_model("approve_presented_candidate", {}),
    ).handle_message(incoming(start_number + 1, "Да."))
    async with factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        version = (await session.execute(select(PlanVersion))).scalar_one()
        persisted_items = list(
            (
                await session.execute(
                    select(PlanVersionItem).order_by(PlanVersionItem.ordinal)
                )
            ).scalars()
        )
        return plan.entity_id, version.id, [item.id for item in persisted_items]


@pytest.mark.parametrize(
    ("message", "status", "note"),
    [
        ("Начал первый пункт.", PlanItemProgressStatus.IN_PROGRESS, None),
        ("Первый пункт сделал.", PlanItemProgressStatus.DONE, None),
        ("По первому пока ждём ответ.", PlanItemProgressStatus.WAITING, "ждём ответ"),
        ("Первый заблокирован — нет доступа.", PlanItemProgressStatus.BLOCKED, "нет доступа"),
        (
            "Этот необязательный шаг пропускаю, план не меняем.",
            PlanItemProgressStatus.SKIPPED,
            None,
        ),
    ],
)
async def test_natural_progress_states_use_user_source(
    session_factory, message, status, note
) -> None:
    plan_id, _, item_ids = await seed_approved(
        session_factory,
        items=[{"ordinal": 1, "title": "Обновить резюме"}],
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_approved_plan_progress",
            {"plan_id": str(plan_id), "ordinal": 1, "status": status.value, "note": note},
        ),
    ).handle_message(incoming(3, message))
    async with session_factory() as session:
        progress = await session.get(PlanItemProgress, item_ids[0])
        source = await session.get(Source, progress.source_id)
        source_message = await session.get(Message, source.message_id)
        assert progress.status == status
        assert progress.note == note
        assert source_message.author == MessageAuthor.USER
        assert source_message.text == message
        version_count = (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one()
        assert version_count == 1


async def test_explicit_reset_and_retry_are_idempotent(session_factory) -> None:
    plan_id, _, item_ids = await seed_approved(
        session_factory,
        items=[{"ordinal": 1, "title": "Обновить резюме"}],
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_approved_plan_progress",
            {
                "plan_id": str(plan_id),
                "title": "Обновить резюме",
                "status": "IN_PROGRESS",
            },
        ),
    ).handle_message(incoming(3, "Начал первый пункт."))
    reset = incoming(4, "Сбрось первый обратно в не начат.")
    runtime = ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_approved_plan_progress",
            {"plan_id": str(plan_id), "ordinal": 1, "status": "NOT_STARTED"},
        ),
    )
    await runtime.handle_message(reset)
    assert await runtime.handle_message(reset) is None
    async with session_factory() as session:
        progress = await session.get(PlanItemProgress, item_ids[0])
        assert progress.status == PlanItemProgressStatus.NOT_STARTED


async def test_revision_phrase_creates_candidate_without_progress_mutation(
    session_factory,
) -> None:
    items = [{"ordinal": 1, "title": "Обновить резюме"}]
    plan_id, approved_id, item_ids = await seed_approved(session_factory, items=items)
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            plan_id=plan_id,
            based_on_version_id=approved_id,
            items=[{"ordinal": 1, "title": "Собрать портфолио"}],
        ),
    ).handle_message(incoming(3, "Первый пункт больше не нужен, убери его."))
    async with session_factory() as session:
        progress = await session.get(PlanItemProgress, item_ids[0])
        versions = list((await session.execute(select(PlanVersion))).scalars())
        assert progress.status == PlanItemProgressStatus.NOT_STARTED
        assert sorted(version.status for version in versions) == sorted(
            [PlanVersionStatus.APPROVED, PlanVersionStatus.CANDIDATE]
        )


async def test_candidate_item_cannot_receive_progress_and_approved_item_wins(
    session_factory,
) -> None:
    plan_id, approved_id, approved_items = await seed_approved(
        session_factory,
        items=[{"ordinal": 1, "title": "Approved шаг"}],
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            plan_id=plan_id,
            based_on_version_id=approved_id,
            items=[{"ordinal": 1, "title": "Candidate шаг"}],
        ),
    ).handle_message(incoming(3, "Предложи новый вариант."))
    async with session_factory() as session:
        candidate = (
            await session.execute(
                select(PlanVersion).where(
                    PlanVersion.status == PlanVersionStatus.CANDIDATE
                )
            )
        ).scalar_one()
        candidate_item = (
            await session.execute(
                select(PlanVersionItem).where(
                    PlanVersionItem.plan_version_id == candidate.id
                )
            )
        ).scalar_one()
    def approved_next_model(messages, info):
        returns = tool_returns(messages)
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_next_plan_action",
                        args={"plan_id": str(plan_id)},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(f"Дальше: {returns[-1].get('title')}")])

    next_response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(approved_next_model),
    ).handle_message(incoming(4, "Что делать дальше?"))
    assert "Approved шаг" in next_response
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_approved_plan_progress",
            {"plan_id": str(plan_id), "ordinal": 1, "status": "DONE"},
        ),
    ).handle_message(incoming(5, "Первый пункт закончил."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_approved_plan_progress",
            {"plan_version_item_id": str(candidate_item.id), "status": "DONE"},
            response="Этот пункт не относится к действующему плану.",
        ),
    ).handle_message(incoming(6, "Отметь Candidate шаг готовым."))
    async with session_factory() as session:
        progress = await session.get(PlanItemProgress, approved_items[0])
        assert progress.status == PlanItemProgressStatus.DONE
        candidate_progress = await session.get(PlanItemProgress, candidate_item.id)
        assert candidate_progress is None


async def test_ambiguous_and_nonexistent_item_references_do_not_mutate(
    session_factory,
) -> None:
    plan_id, _, item_ids = await seed_approved(
        session_factory,
        items=[
            {"ordinal": 1, "title": "Повтор"},
            {"ordinal": 2, "title": "Повтор"},
        ],
    )
    for number, args in (
        (3, {"plan_id": str(plan_id), "title": "Повтор", "status": "DONE"}),
        (4, {"plan_id": str(plan_id), "ordinal": 99, "status": "DONE"}),
    ):
        await ElowynRuntime(
            session_factory=session_factory,
            model=one_tool_model(
                "update_approved_plan_progress",
                args,
                response="Уточни, пожалуйста, конкретный пункт.",
            ),
        ).handle_message(incoming(number, "Готово."))
    async with session_factory() as session:
        states = [await session.get(PlanItemProgress, item_id) for item_id in item_ids]
        assert all(state.status == PlanItemProgressStatus.NOT_STARTED for state in states)


async def test_done_then_next_action_is_atomic_and_uses_second_item(session_factory) -> None:
    first_id, second_id = uuid4(), uuid4()
    plan_id, _, item_ids = await seed_approved(
        session_factory,
        items=[
            {"id": str(first_id), "ordinal": 1, "title": "Сделать первый"},
            {"id": str(second_id), "ordinal": 2, "title": "Сделать второй"},
        ],
        dependencies=[
            {
                "prerequisite_item_id": str(first_id),
                "dependent_item_id": str(second_id),
            }
        ],
    )

    def combined(messages, info):
        returns = tool_returns(messages)
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="update_approved_plan_progress",
                        args={"plan_id": str(plan_id), "ordinal": 1, "status": "DONE"},
                    ),
                    ToolCallPart(
                        tool_name="get_next_plan_action",
                        args={"plan_id": str(plan_id)},
                    ),
                ]
            )
        next_action = next(value for value in returns if value.get("result") == "next_action")
        return ModelResponse(parts=[TextPart(f"Дальше: {next_action['title']}")])

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(combined),
    ).handle_message(incoming(3, "Первый сделал. Что дальше?"))
    assert response == "Дальше: Сделать второй"
    async with session_factory() as session:
        first = await session.get(PlanItemProgress, item_ids[0])
        second = await session.get(PlanItemProgress, item_ids[1])
        assert first.status == PlanItemProgressStatus.DONE
        assert second.status == PlanItemProgressStatus.NOT_STARTED


@pytest.mark.parametrize(
    ("first_status", "dependent", "expected"),
    [
        (PlanItemProgressStatus.IN_PROGRESS, False, "Первый"),
        (PlanItemProgressStatus.WAITING, True, None),
        (PlanItemProgressStatus.BLOCKED, False, "Второй"),
        (PlanItemProgressStatus.SKIPPED, True, None),
    ],
)
async def test_next_action_preserves_existing_deterministic_semantics(
    session_factory, first_status, dependent, expected
) -> None:
    first_id, second_id = uuid4(), uuid4()
    dependencies = []
    if dependent:
        dependencies.append(
            {
                "prerequisite_item_id": str(first_id),
                "dependent_item_id": str(second_id),
            }
        )
    plan_id, _, _ = await seed_approved(
        session_factory,
        items=[
            {"id": str(first_id), "ordinal": 1, "title": "Первый"},
            {"id": str(second_id), "ordinal": 2, "title": "Второй"},
        ],
        dependencies=dependencies,
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_approved_plan_progress",
            {"plan_id": str(plan_id), "ordinal": 1, "status": first_status.value},
        ),
    ).handle_message(incoming(3, "Обновляю состояние первого."))

    def next_model(messages, info):
        returns = tool_returns(messages)
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_next_plan_action",
                        args={"plan_id": str(plan_id)},
                    )
                ]
            )
        value = returns[-1]
        title = value.get("title")
        return ModelResponse(parts=[TextPart(title or "Нет доступного следующего шага.")])

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(next_model),
    ).handle_message(incoming(4, "Что делать дальше?"))
    assert response == (expected or "Нет доступного следующего шага.")


async def test_next_action_ignores_candidate_and_requires_approved(session_factory) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(items=[{"ordinal": 1, "title": "Только Candidate"}]),
    ).handle_message(incoming(1, "Предложи план."))
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "get_next_plan_action",
            {},
            response="Пока есть только неутверждённый вариант.",
        ),
    ).handle_message(incoming(2, "Что делать дальше?"))
    assert "неутверждённый" in response
    async with session_factory() as session:
        version = (await session.execute(select(PlanVersion))).scalar_one()
        assert version.status == PlanVersionStatus.CANDIDATE


async def test_linked_task_is_unchanged_by_runtime_progress(session_factory) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("create_task", {"title": "Связанная задача"}),
    ).handle_message(incoming(1, "Создай задачу."))
    async with session_factory() as session:
        task = (await session.execute(select(Task))).scalar_one()
        task_id, task_status = task.entity_id, task.status
    plan_id, _, _ = await seed_approved(
        session_factory,
        start_number=2,
        items=[
            {
                "ordinal": 1,
                "title": "Связанный пункт",
                "linked_task_id": str(task_id),
            }
        ],
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_approved_plan_progress",
            {"plan_id": str(plan_id), "ordinal": 1, "status": "DONE"},
        ),
    ).handle_message(incoming(4, "Пункт сделал."))
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        assert task.status == task_status == TaskStatus.TODO


async def test_combined_turn_rolls_back_progress_when_acknowledgement_save_fails(
    session_factory, monkeypatch
) -> None:
    plan_id, _, item_ids = await seed_approved(
        session_factory,
        items=[
            {"ordinal": 1, "title": "Первый"},
            {"ordinal": 2, "title": "Второй"},
        ],
    )

    def combined(messages, info):
        if not has_tool_return(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="update_approved_plan_progress",
                        args={"plan_id": str(plan_id), "ordinal": 1, "status": "DONE"},
                    ),
                    ToolCallPart(
                        tool_name="get_next_plan_action",
                        args={"plan_id": str(plan_id)},
                    ),
                ]
            )
        return ModelResponse(parts=[TextPart("Дальше второй.")])

    async def fail_save(self, **kwargs):
        raise RuntimeError("synthetic combined acknowledgement failure")

    monkeypatch.setattr(
        runtime_module.ConversationService,
        "record_assistant_message",
        fail_save,
    )
    with pytest.raises(RuntimeError, match="combined acknowledgement"):
        await ElowynRuntime(
            session_factory=session_factory,
            model=FunctionModel(combined),
        ).handle_message(incoming(3, "Первый сделал. Что дальше?"))
    async with session_factory() as session:
        first = await session.get(PlanItemProgress, item_ids[0])
        assert first.status == PlanItemProgressStatus.NOT_STARTED
        user_count = (
            await session.execute(
                select(func.count()).select_from(Message).where(
                    Message.author == MessageAuthor.USER
                )
            )
        ).scalar_one()
        assert user_count == 3


async def test_read_current_plan_distinguishes_approved_candidate_and_progress(
    session_factory,
) -> None:
    plan_id, approved_id, _ = await seed_approved(
        session_factory,
        items=[{"ordinal": 1, "title": "Действующий шаг"}],
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_approved_plan_progress",
            {"plan_id": str(plan_id), "ordinal": 1, "status": "IN_PROGRESS"},
        ),
    ).handle_message(incoming(3, "Начал действующий шаг."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            plan_id=plan_id,
            based_on_version_id=approved_id,
            summary="Предложенный новый вариант",
            strategy="Новый ещё не утверждённый подход",
            items=[{"ordinal": 1, "title": "Предложенный шаг"}],
        ),
    ).handle_message(incoming(4, "Предложи другой вариант."))

    def explain_current(value):
        plan = value["plan"]
        assert plan["approved"]["status"] == "APPROVED"
        assert plan["approved"]["items"][0]["progress"] == "IN_PROGRESS"
        assert plan["candidate"]["status"] == "CANDIDATE"
        assert plan["strategy"]["approach"] == "Выполнять последовательно"
        return "Действуем по утверждённому плану; новый вариант пока только предложен."

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "read_current_plan",
            {"plan_id": str(plan_id)},
            explain_current,
        ),
    ).handle_message(incoming(5, "Какой у нас сейчас план?"))
    assert "только предложен" in response


async def test_history_rejected_provenance_and_structured_compare(session_factory) -> None:
    plan_id, first_id, _ = await seed_approved(
        session_factory,
        items=[
            {"ordinal": 1, "title": "Старый шаг"},
            {"ordinal": 2, "title": "Общий шаг"},
        ],
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            plan_id=plan_id,
            based_on_version_id=first_id,
            summary="Вариант после замечания",
            strategy="Изменённый подход",
            rationale="Учитывает отказ от старого шага",
            items=[
                {"ordinal": 1, "title": "Новый шаг"},
                {"ordinal": 2, "title": "Общий шаг", "description": "Уточнён"},
            ],
        ),
    ).handle_message(incoming(3, "Убери старый шаг и измени подход."))
    async with session_factory() as session:
        second = (
            await session.execute(
                select(PlanVersion).where(PlanVersion.status == PlanVersionStatus.CANDIDATE)
            )
        ).scalar_one()
        second_id = second.id
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("reject_presented_candidate", {}),
    ).handle_message(incoming(4, "Нет, этот новый вариант отклоняем."))

    def explain_history(value):
        versions = value["versions"]
        assert [version["status"] for version in versions] == ["REJECTED", "APPROVED"]
        assert "Убери старый шаг" in versions[0]["creation_evidence"][0]["text"]
        assert "отклоняем" in versions[0]["rejection_evidence"][0]["text"]
        return "Новый вариант появился после твоей просьбы, но затем был отклонён."

    history_response = await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "read_plan_history",
            {"plan_id": str(plan_id), "limit": 5},
            explain_history,
        ),
    ).handle_message(incoming(5, "Какой план был раньше и почему новый отклонили?"))
    assert "был отклонён" in history_response

    def explain_compare(value):
        comparison = value["comparison"]
        assert comparison["strategy_changed"] is True
        assert comparison["added_items"] == ["Новый шаг"]
        assert comparison["removed_items"] == ["Старый шаг"]
        assert comparison["changed_items"][0]["title"] == "Общий шаг"
        return "Подход изменился: старый шаг убран, новый добавлен, общий уточнён."

    compare_response = await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "compare_plan_versions",
            {
                "older_plan_version_id": str(first_id),
                "newer_plan_version_id": str(second_id),
            },
            explain_compare,
        ),
    ).handle_message(incoming(6, "Что поменялось?"))
    assert "старый шаг убран" in compare_response


async def test_restore_historical_version_creates_new_candidate(session_factory) -> None:
    plan_id, first_id, _ = await seed_approved(
        session_factory,
        items=[{"ordinal": 1, "title": "Исторический шаг"}],
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            plan_id=plan_id,
            based_on_version_id=first_id,
            summary="Второй план",
            strategy="Второй подход",
            items=[{"ordinal": 1, "title": "Нынешний шаг"}],
        ),
    ).handle_message(incoming(3, "Предложи второй план."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("approve_presented_candidate", {}),
    ).handle_message(incoming(4, "Да."))

    def restore_model(messages, info):
        returns = tool_returns(messages)
        presentation = next(
            (value for value in returns if "presentation_placeholder" in value),
            None,
        )
        if presentation is not None:
            return ModelResponse(parts=[TextPart(presentation["presentation_placeholder"])])
        historical = next(
            (value["version"] for value in returns if value.get("result") == "plan_version"),
            None,
        )
        if historical is None:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="read_plan_version",
                        args={"plan_version_id": str(first_id)},
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="present_candidate_plan",
                    args={
                        "plan_id": str(plan_id),
                        "summary": historical["summary"],
                        "rationale": historical["rationale"],
                        "proposed_strategy_snapshot": historical["proposed_strategy"],
                        "strategy_rationale_snapshot": historical["strategy_rationale"],
                        "based_on_version_id": str(first_id),
                        "items": [
                            {
                                "ordinal": item["ordinal"],
                                "title": item["title"],
                                "description": item["description"],
                                "expected_outcome": item["expected_outcome"],
                                "deadline_at": item["deadline_at"],
                                "estimated_duration_minutes": item[
                                    "estimated_duration_minutes"
                                ],
                            }
                            for item in historical["items"]
                        ],
                    },
                )
            ]
        )

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(restore_model),
    ).handle_message(incoming(5, "Давай вернёмся к предыдущему плану."))
    assert "Исторический шаг" in response
    async with session_factory() as session:
        versions = list(
            (
                await session.execute(
                    select(PlanVersion).order_by(PlanVersion.version_number)
                )
            ).scalars()
        )
        assert [version.status for version in versions] == [
            PlanVersionStatus.SUPERSEDED,
            PlanVersionStatus.APPROVED,
            PlanVersionStatus.CANDIDATE,
        ]
        assert versions[2].based_on_version_id == versions[0].id == first_id


async def test_staleness_read_is_basis_scoped_and_never_replans(session_factory) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("create_goal", {"title": "Основание плана"}),
    ).handle_message(incoming(1, "Создай цель-основание."))
    async with session_factory() as session:
        goal = (await session.execute(select(Goal))).scalar_one()
        goal_event = (
            await session.execute(
                select(Event).where(
                    Event.entity_id == goal.entity_id,
                    Event.event_type == EventType.GOAL_CREATED,
                )
            )
        ).scalar_one()
        goal_id, goal_event_id = goal.entity_id, goal_event.id
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            items=[{"ordinal": 1, "title": "Шаг по цели"}],
            basis=[
                {
                    "entity_id": str(goal_id),
                    "event_id": str(goal_event_id),
                    "role": "GOAL",
                }
            ],
        ),
    ).handle_message(incoming(2, "Предложи план по этой цели."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("approve_presented_candidate", {}),
    ).handle_message(incoming(3, "Да."))
    async with session_factory() as session:
        version = (await session.execute(select(PlanVersion))).scalar_one()
        version_id = version.id

    def fresh(value):
        assert value["is_basis_stale"] is False
        return "Нет известных изменений оснований, но это не гарантия абсолютной актуальности."

    await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "assess_plan_staleness_read",
            {"plan_version_id": str(version_id)},
            fresh,
        ),
    ).handle_message(incoming(4, "План актуален?"))
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("create_goal", {"title": "Посторонняя цель"}),
    ).handle_message(incoming(5, "Создай другую цель."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "assess_plan_staleness_read",
            {"plan_version_id": str(version_id)},
            fresh,
        ),
    ).handle_message(incoming(6, "А теперь актуален?"))
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_goal",
            {"entity_id": str(goal_id), "description": "Основание изменилось"},
        ),
    ).handle_message(incoming(7, "Измени цель-основание."))

    def stale(value):
        assert value["is_basis_stale"] is True
        assert value["changed_basis"] == [{"role": "GOAL", "label": "Основание плана"}]
        return "Основание изменилось; это сигнал пересмотреть план, а не доказательство ошибки."

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "assess_plan_staleness_read",
            {"plan_version_id": str(version_id)},
            stale,
        ),
    ).handle_message(incoming(8, "План всё ещё актуален?"))
    assert "не доказательство ошибки" in response
    async with session_factory() as session:
        version_count = (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one()
        assert version_count == 1


async def test_normal_planning_context_excludes_twenty_historical_versions(
    session_factory,
) -> None:
    plan_id, approved_id, _ = await seed_approved(
        session_factory,
        items=[{"ordinal": 1, "title": "Approved current"}],
    )
    based_on = approved_id
    historical_ids: list[UUID] = []
    for number in range(3, 23):
        await ElowynRuntime(
            session_factory=session_factory,
            model=candidate_model(
                plan_id=plan_id,
                based_on_version_id=based_on,
                summary=f"History {number}",
                strategy=f"Historical strategy {number}",
                items=[{"ordinal": 1, "title": f"Historical item {number}"}],
            ),
        ).handle_message(incoming(number, f"Предложи вариант {number}."))
        async with session_factory() as session:
            current = (
                await session.execute(
                    select(PlanVersion).where(
                        PlanVersion.status == PlanVersionStatus.CANDIDATE
                    )
                )
            ).scalar_one()
            based_on = current.id
            historical_ids.append(current.id)

    def inspect_prompt(messages, info):
        prompt = "\n".join(
            content
            for message in messages
            for part in message.parts
            if isinstance((content := getattr(part, "content", None)), str)
        )
        planning = prompt.split("ТЕКУЩЕЕ PLANNING STATE", 1)[1].split(
            "НОВОЕ СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ", 1
        )[0]
        assert planning.count("internal_version_id") == 2
        assert str(approved_id) in planning
        assert str(historical_ids[-1]) in planning
        assert str(historical_ids[0]) not in planning
        assert len(planning) < 5000
        return ModelResponse(parts=[TextPart("Текущий контекст остаётся компактным.")])

    await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(inspect_prompt),
    ).handle_message(incoming(23, "Обсудим текущий план."))

    def inspect_explicit_history(value):
        assert len(value["versions"]) == 20
        return "История загружена отдельно и ограничена двадцатью версиями."

    await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "read_plan_history",
            {"plan_id": str(plan_id), "limit": 20},
            inspect_explicit_history,
        ),
    ).handle_message(incoming(24, "Покажи историю версий."))


async def test_v03_deterministic_runtime_acceptance_cycle(
    session_factory, monkeypatch
) -> None:
    """Exercise the complete v0.3 user cycle through durable runtime boundaries."""

    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("create_goal", {"title": "Найти новую работу"}),
    ).handle_message(incoming(1, "Моя цель — найти новую работу."))
    async with session_factory() as session:
        goal = (await session.execute(select(Goal))).scalar_one()
        goal_event = (
            await session.execute(
                select(Event).where(
                    Event.entity_id == goal.entity_id,
                    Event.event_type == EventType.GOAL_CREATED,
                )
            )
        ).scalar_one()
        goal_id, goal_event_id = goal.entity_id, goal_event.id

    async def relevant_memory(self, **kwargs):
        return BoundedMemoryContext(
            text=(
                "MEMORY (DERIVED, NON-AUTHORITATIVE; current user statement and "
                "WORLD STATE win):\n- Пользователь предпочитает конкретные шаги."
            ),
            token_upper_bound=30,
            item_count=1,
        )

    monkeypatch.setattr(runtime_module.ContextComposer, "memory_context", relevant_memory)

    def initial_candidate(messages, info):
        if not has_tool_return(messages):
            prompt = "\n".join(
                content
                for message in messages
                for part in message.parts
                if isinstance((content := getattr(part, "content", None)), str)
            )
            assert "Найти новую работу" in prompt
            assert "DERIVED, NON-AUTHORITATIVE" in prompt
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="create_plan_with_candidate",
                        args={
                            "plan": {
                                "title": "Поиск новой работы",
                                "goals": [{"goal_id": str(goal_id), "role": "PRIMARY"}],
                            },
                            "candidate": {
                                "summary": "Первый вариант поиска",
                                "proposed_strategy_snapshot": "Широкий поиск через отклики",
                                "strategy_rationale_snapshot": "Больше охват рынка",
                                "basis": [
                                    {
                                        "entity_id": str(goal_id),
                                        "event_id": str(goal_event_id),
                                        "role": "GOAL",
                                    }
                                ],
                                "items": [
                                    {"ordinal": 1, "title": "Обновить резюме"},
                                    {"ordinal": 2, "title": "Выбрать вакансии"},
                                    {"ordinal": 3, "title": "Делать массовые отклики"},
                                ],
                            },
                        },
                    )
                ]
            )
        placeholder = next(
            value["presentation_placeholder"]
            for value in tool_returns(messages)
            if "presentation_placeholder" in value
        )
        return ModelResponse(parts=[TextPart(placeholder)])

    first_response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(initial_candidate),
        memory_service=object(),
    ).handle_message(incoming(2, "Я хочу найти новую работу. Что ты предлагаешь?"))
    assert "Делать массовые отклики" in first_response
    assert "ELOWYN_PLAN_PRESENTATION" not in first_response
    async with session_factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        first = (await session.execute(select(PlanVersion))).scalar_one()
        assert first.status == PlanVersionStatus.CANDIDATE
        assert (await session.execute(select(func.count()).select_from(Strategy))).scalar_one() == 0
        assert (
            await session.execute(select(func.count()).select_from(PlanGoalLink))
        ).scalar_one() == 1
        assert (await session.execute(select(func.count()).select_from(Task))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(Project))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(Goal))).scalar_one() == 1
        plan_id, first_id = plan.entity_id, first.id

    def revised_candidate(messages, info):
        if not has_tool_return(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="present_candidate_plan",
                        args={
                            "plan_id": str(plan_id),
                            "summary": "Исправленный вариант поиска",
                            "proposed_strategy_snapshot": "Точечный поиск подходящих ролей",
                            "strategy_rationale_snapshot": "Качество важнее количества",
                            "based_on_version_id": str(first_id),
                            "basis": [
                                {
                                    "entity_id": str(goal_id),
                                    "event_id": str(goal_event_id),
                                    "role": "GOAL",
                                }
                            ],
                            "items": [
                                {"ordinal": 1, "title": "Обновить резюме"},
                                {"ordinal": 2, "title": "Выбрать вакансии"},
                                {"ordinal": 3, "title": "Написать точечные обращения"},
                            ],
                        },
                    )
                ]
            )
        placeholder = next(
            value["presentation_placeholder"]
            for value in tool_returns(messages)
            if "presentation_placeholder" in value
        )
        return ModelResponse(parts=[TextPart(placeholder)])

    correction_text = (
        "Первые два пункта подходят, а третий я делать не хочу. Предложи другой вариант."
    )
    second_response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(revised_candidate),
    ).handle_message(incoming(3, correction_text))
    assert "Написать точечные обращения" in second_response
    async with session_factory() as session:
        versions = list(
            (
                await session.execute(
                    select(PlanVersion).order_by(PlanVersion.version_number)
                )
            ).scalars()
        )
        assert [version.status for version in versions] == [
            PlanVersionStatus.SUPERSEDED,
            PlanVersionStatus.CANDIDATE,
        ]
        assert versions[1].based_on_version_id == versions[0].id
        assert (
            await session.execute(select(func.count()).select_from(PlanVersionPresentation))
        ).scalar_one() == 2
        second_id = versions[1].id

    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("approve_presented_candidate", {}, "План утверждён."),
    ).handle_message(incoming(4, "Да."))
    async with session_factory() as session:
        approved = await session.get(PlanVersion, second_id)
        strategy = (await session.execute(select(Strategy))).scalar_one()
        approval_source = await session.get(Source, approved.approval_source_id)
        approval_message = await session.get(Message, approval_source.message_id)
        assert approved.status == PlanVersionStatus.APPROVED
        assert approval_message.text == "Да."
        assert strategy.accepted_from_plan_version_id == approved.id
        assert strategy.approach == approved.proposed_strategy_snapshot
        assert (await session.execute(select(func.count()).select_from(Task))).scalar_one() == 0

    def progress_and_next(messages, info):
        returns = tool_returns(messages)
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="update_approved_plan_progress",
                        args={"plan_id": str(plan_id), "ordinal": 1, "status": "DONE"},
                    )
                ]
            )
        if not any(value.get("result") == "next_action" for value in returns):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_next_plan_action",
                        args={"plan_id": str(plan_id)},
                    )
                ]
            )
        next_action = next(value for value in returns if value.get("result") == "next_action")
        assert next_action["ordinal"] == 2
        return ModelResponse(parts=[TextPart("Первый пункт готов. Дальше выбери вакансии.")])

    progress_response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(progress_and_next),
    ).handle_message(incoming(5, "Первый пункт сделал. Что дальше?"))
    assert "выбери вакансии" in progress_response
    async with session_factory() as session:
        version = await session.get(PlanVersion, second_id)
        progress = list(
            (
                await session.execute(
                    select(PlanItemProgress)
                    .join(
                        PlanVersionItem,
                        PlanVersionItem.id == PlanItemProgress.plan_version_item_id,
                    )
                    .where(PlanVersionItem.plan_version_id == second_id)
                    .order_by(PlanVersionItem.ordinal)
                )
            ).scalars()
        )
        assert version.status == PlanVersionStatus.APPROVED
        assert progress[0].status == PlanItemProgressStatus.DONE
        assert (await session.execute(select(func.count()).select_from(Task))).scalar_one() == 0

    def explain_change(value):
        versions = value["versions"]
        assert [version["status"] for version in versions] == ["APPROVED", "SUPERSEDED"]
        assert versions[0]["creation_evidence"][0]["text"] == correction_text
        return "Мы изменили вариант после твоего отказа от третьего пункта."

    reason_response = await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "read_plan_history",
            {"plan_id": str(plan_id), "limit": 5},
            explain_change,
        ),
    ).handle_message(incoming(6, "Почему мы вообще поменяли первый вариант?"))
    assert "после твоего отказа" in reason_response

    def explain_diff(value):
        comparison = value["comparison"]
        assert comparison["strategy_changed"] is True
        assert comparison["removed_items"] == ["Делать массовые отклики"]
        assert comparison["added_items"] == ["Написать точечные обращения"]
        return "Сменили подход и заменили массовые отклики на точечные обращения."

    diff_response = await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "compare_plan_versions",
            {
                "older_plan_version_id": str(first_id),
                "newer_plan_version_id": str(second_id),
            },
            explain_diff,
        ),
    ).handle_message(incoming(7, "Что изменилось между первым и текущим вариантом?"))
    assert "точечные обращения" in diff_response

    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_goal",
            {"entity_id": str(goal_id), "description": "Теперь нужна удалённая работа"},
        ),
    ).handle_message(incoming(8, "Теперь мне нужна только удалённая работа."))

    def explain_staleness(value):
        assert value["is_basis_stale"] is True
        assert value["changed_basis"] == [{"role": "GOAL", "label": "Найти новую работу"}]
        return "Основание плана изменилось; это не означает, что план автоматически заменён."

    stale_response = await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "assess_plan_staleness_read",
            {"plan_version_id": str(second_id)},
            explain_staleness,
        ),
    ).handle_message(incoming(9, "Наш план всё ещё актуален?"))
    assert "не означает" in stale_response

    def after_restart(value):
        snapshot = value["plan"]
        assert snapshot["approved"]["internal_version_id"] == str(second_id)
        assert snapshot["approved"]["items"][0]["progress"] == "DONE"
        assert snapshot["candidate"] is None
        assert snapshot["strategy"]["approach"] == "Точечный поиск подходящих ролей"
        return "Действует второй план; первый пункт выполнен."

    restarted_response = await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "read_current_plan",
            {"plan_id": str(plan_id)},
            after_restart,
        ),
    ).handle_message(incoming(10, "Что утверждено после перезапуска?"))
    assert "первый пункт выполнен" in restarted_response
    async with session_factory() as session:
        versions = list((await session.execute(select(PlanVersion))).scalars())
        assert len(versions) == 2
        assert not any(version.status == PlanVersionStatus.CANDIDATE for version in versions)
        assert (
            await session.execute(select(func.count()).select_from(PlanVersionPresentation))
        ).scalar_one() == 2
        (await ConsistencyVerifier(session).verify()).require_ok()
