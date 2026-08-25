from __future__ import annotations

import json
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
from elowyn.assistant.planning_tools import has_explicit_replanning_intent
from elowyn.db.base import Base
from elowyn.db.models import (
    Event,
    Goal,
    Message,
    Plan,
    PlanGoalLink,
    PlanItemProgress,
    PlanVersion,
    PlanVersionBasis,
    PlanVersionItem,
    PlanVersionPresentation,
    Project,
    Source,
    Strategy,
    Task,
)
from elowyn.domain.enums import (
    DeadlineType,
    EventType,
    MessageAuthor,
    PlanItemProgressStatus,
    PlanVersionStatus,
    TaskStatus,
    TransportType,
)
from elowyn.domain.messages import IncomingMessage
from elowyn.runtime import ElowynRuntime
from elowyn.services.planning_query import PlanningQueryService
from elowyn.support.consistency import ConsistencyVerifier
from elowyn.support.database_safety import assert_test_database_url

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "text_value",
    [
        "Убери письменное саммари. В последний день оставь только устную репетицию.",
        "Давай всё-таки читать не за две недели, а за три.",
        "Предложи обновлённый план на 5 дней.",
        "Книгу надо закончить за 5 дней, перестрой план.",
    ],
)
def test_explicit_replanning_intent_examples(text_value: str) -> None:
    assert has_explicit_replanning_intent(text_value) is True


@pytest.mark.parametrize(
    "text_value",
    [
        "Книгу теперь надо закончить за 5 дней.",
        "На самом деле книгу надо закончить уже через пять дней.",
        "Наш текущий план всё ещё построен на актуальных данных?",
        "Покажи текущий вариант.",
        "Давай обсудим план, но пока ничего не меняй.",
    ],
)
def test_basis_or_discussion_is_not_explicit_replanning_intent(text_value: str) -> None:
    assert has_explicit_replanning_intent(text_value) is False


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


def model_must_not_run() -> FunctionModel:
    def model_function(messages, info):
        raise AssertionError("canonical Planning route must bypass model inference")

    return FunctionModel(model_function)


def inspected_tool_model(name: str, args: dict, response_builder) -> FunctionModel:
    def model_function(messages, info):
        returns = tool_returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
        return ModelResponse(parts=[TextPart(response_builder(returns[-1]))])

    return FunctionModel(model_function)


def basis_update_then_candidate_model(
    *,
    goal_id: UUID,
    plan_id: UUID,
    based_on_version_id: UUID,
    fail_after_candidate: bool = False,
    create_new_plan: bool = False,
) -> FunctionModel:
    def model_function(messages, info):
        returns = tool_returns(messages)
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="update_goal",
                        args={
                            "entity_id": str(goal_id),
                            "description": "Книгу надо закончить за пять дней",
                        },
                    )
                ]
            )
        if len(returns) == 1:
            candidate = {
                "summary": "Пятидневный вариант",
                "proposed_strategy_snapshot": "Сжать чтение до пяти дней",
                "items": [{"ordinal": day, "title": f"Чтение: день {day}"} for day in range(1, 6)],
            }
            if create_new_plan:
                tool_name = "create_plan_with_candidate"
                args = {"plan": {"title": "Автоматический план"}, "candidate": candidate}
            else:
                tool_name = "present_candidate_plan"
                args = {
                    "plan_id": str(plan_id),
                    "based_on_version_id": str(based_on_version_id),
                    **candidate,
                }
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=tool_name,
                        args=args,
                    )
                ]
            )
        if fail_after_candidate:
            raise RuntimeError("synthetic failure after replanning")
        if returns[-1].get("result") == "candidate_not_created":
            assert returns[-1]["reason"] == "replanning_intent_not_explicit"
            return ModelResponse(
                parts=[
                    TextPart(
                        "Текущий план теперь устарел относительно нового срока. "
                        "Хочешь, я предложу обновлённый вариант на 5 дней?"
                    )
                ]
            )
        placeholder = returns[-1]["presentation_placeholder"]
        return ModelResponse(parts=[TextPart(placeholder)])

    return FunctionModel(model_function)


async def seed_approved(factory, *, items: list[dict], dependencies=None, start_number: int = 1):
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
                await session.execute(select(PlanVersionItem).order_by(PlanVersionItem.ordinal))
            ).scalars()
        )
        return plan.entity_id, version.id, [item.id for item in persisted_items]


async def seed_basis_approved(factory, *, start_number: int = 1):
    await ElowynRuntime(
        session_factory=factory,
        model=one_tool_model("create_goal", {"title": "Прочитать книгу"}),
    ).handle_message(incoming(start_number, "Создай цель прочитать книгу."))
    async with factory() as session:
        goal = (await session.execute(select(Goal))).scalar_one()
        event = (
            await session.execute(
                select(Event).where(
                    Event.entity_id == goal.entity_id,
                    Event.event_type == EventType.GOAL_CREATED,
                )
            )
        ).scalar_one()
        goal_id, event_id = goal.entity_id, event.id
    await ElowynRuntime(
        session_factory=factory,
        model=candidate_model(
            items=[{"ordinal": day, "title": f"Чтение: день {day}"} for day in range(1, 15)],
            basis=[
                {
                    "entity_id": str(goal_id),
                    "event_id": str(event_id),
                    "role": "GOAL",
                }
            ],
        ),
    ).handle_message(incoming(start_number + 1, "Предложи план чтения на 14 дней."))
    await ElowynRuntime(
        session_factory=factory,
        model=one_tool_model("approve_presented_candidate", {}),
    ).handle_message(incoming(start_number + 2, "Да."))
    async with factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        version = (await session.execute(select(PlanVersion))).scalar_one()
        return goal_id, plan.entity_id, version.id


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
                select(PlanVersion).where(PlanVersion.status == PlanVersionStatus.CANDIDATE)
            )
        ).scalar_one()
        candidate_item = (
            await session.execute(
                select(PlanVersionItem).where(PlanVersionItem.plan_version_id == candidate.id)
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
                select(func.count())
                .select_from(Message)
                .where(Message.author == MessageAuthor.USER)
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


async def test_historical_approved_return_reactivates_identity_then_edit_creates_candidate(
    session_factory,
) -> None:
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
    async with session_factory() as session:
        versions = list(
            (
                await session.execute(select(PlanVersion).order_by(PlanVersion.version_number))
            ).scalars()
        )
        second_id = versions[1].id
        first_item = (
            await session.execute(
                select(PlanVersionItem).where(PlanVersionItem.plan_version_id == first_id)
            )
        ).scalar_one()
        first_progress = await session.get(PlanItemProgress, first_item.id)
        first_snapshot = (first_item.id, first_item.title, first_progress.status)

    def show_historical(messages, info):
        if not has_tool_return(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="show_historical_plan_for_return",
                        args={"plan_version_id": str(first_id)},
                    )
                ]
            )
        presentation = next(
            value for value in tool_returns(messages) if "presentation_placeholder" in value
        )
        assert presentation["result"] == "historical_approved_presented_for_confirmation"
        return ModelResponse(parts=[TextPart(presentation["presentation_placeholder"])])

    shown = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(show_historical),
    ).handle_message(incoming(5, "Покажи прежний утверждённый вариант."))
    assert "Исторический шаг" in shown
    async with session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one() == 2

    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("reactivate_presented_historical_plan", {}),
    ).handle_message(incoming(6, "Вернёмся к нему."))
    async with session_factory() as session:
        versions = list(
            (
                await session.execute(select(PlanVersion).order_by(PlanVersion.version_number))
            ).scalars()
        )
        assert [version.status for version in versions] == [
            PlanVersionStatus.APPROVED,
            PlanVersionStatus.SUPERSEDED,
        ]
        assert [version.id for version in versions] == [first_id, second_id]
        first_item = (
            await session.execute(
                select(PlanVersionItem).where(PlanVersionItem.plan_version_id == first_id)
            )
        ).scalar_one()
        first_progress = await session.get(PlanItemProgress, first_item.id)
        assert (first_item.id, first_item.title, first_progress.status) == first_snapshot
        strategy = (await session.execute(select(Strategy))).scalar_one()
        assert strategy.accepted_from_plan_version_id == first_id
        assert (await session.execute(select(func.count()).select_from(Task))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(Project))).scalar_one() == 0
        approval_events = list(
            (
                await session.execute(
                    select(Event)
                    .where(Event.event_type == EventType.PLAN_VERSION_APPROVED)
                    .order_by(Event.created_at)
                )
            ).scalars()
        )
        activated_versions = [
            next(change["new"] for change in event.changes if change["field"] == "version_id")
            for event in approval_events
        ]
        assert activated_versions == [str(first_id), str(second_id), str(first_id)]

    def inspect_activation_history(value):
        assert [item["version_number"] for item in value["approval_activity"]] == [1, 2, 1]
        assert [item["reactivated"] for item in value["approval_activity"]] == [
            False,
            False,
            True,
        ]
        return "История активности: версия 1, версия 2, снова версия 1."

    await ElowynRuntime(
        session_factory=session_factory,
        model=inspected_tool_model(
            "read_plan_history",
            {"plan_id": str(plan_id), "limit": 5},
            inspect_activation_history,
        ),
    ).handle_message(incoming(7, "Покажи историю активности плана."))

    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            plan_id=plan_id,
            based_on_version_id=first_id,
            summary="Edited after return",
            strategy="Historical approach with one edit",
            items=[{"ordinal": 1, "title": "Исторический шаг уточнён"}],
        ),
    ).handle_message(incoming(8, "Теперь немного измени этот возвращённый план."))
    async with session_factory() as session:
        versions = list(
            (
                await session.execute(select(PlanVersion).order_by(PlanVersion.version_number))
            ).scalars()
        )
        assert [version.status for version in versions] == [
            PlanVersionStatus.APPROVED,
            PlanVersionStatus.SUPERSEDED,
            PlanVersionStatus.CANDIDATE,
        ]
        assert versions[2].based_on_version_id == first_id


async def test_historical_reactivation_failure_rolls_back_atomically(
    session_factory, monkeypatch
) -> None:
    plan_id, first_id, _ = await seed_approved(
        session_factory,
        items=[{"ordinal": 1, "title": "Исторический шаг"}],
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            plan_id=plan_id,
            based_on_version_id=first_id,
            summary="Current plan",
            strategy="Current strategy",
            items=[{"ordinal": 1, "title": "Текущий шаг"}],
        ),
    ).handle_message(incoming(3, "Предложи новый план."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("approve_presented_candidate", {}),
    ).handle_message(incoming(4, "Да."))

    def show_historical(messages, info):
        if not has_tool_return(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="show_historical_plan_for_return",
                        args={"plan_version_id": str(first_id)},
                    )
                ]
            )
        placeholder = next(
            value["presentation_placeholder"]
            for value in tool_returns(messages)
            if "presentation_placeholder" in value
        )
        return ModelResponse(parts=[TextPart(placeholder)])

    await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(show_historical),
    ).handle_message(incoming(5, "Покажи старый план."))
    async with session_factory() as session:
        before_events = (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one()
        current = (
            await session.execute(
                select(PlanVersion).where(PlanVersion.status == PlanVersionStatus.APPROVED)
            )
        ).scalar_one()
        current_id = current.id

    async def fail_strategy(self, **kwargs):
        raise RuntimeError("synthetic reactivation Strategy failure")

    monkeypatch.setattr(runtime_module.PlanningService, "_accept_strategy", fail_strategy)
    with pytest.raises(RuntimeError, match="reactivation Strategy failure"):
        await ElowynRuntime(
            session_factory=session_factory,
            model=one_tool_model("reactivate_presented_historical_plan", {}),
        ).handle_message(incoming(6, "Вернёмся к нему."))
    async with session_factory() as session:
        historical = await session.get(PlanVersion, first_id)
        current = await session.get(PlanVersion, current_id)
        strategy = (await session.execute(select(Strategy))).scalar_one()
        assert historical.status == PlanVersionStatus.SUPERSEDED
        assert current.status == PlanVersionStatus.APPROVED
        assert strategy.accepted_from_plan_version_id == current_id
        assert (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one() == 2
        assert (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one() == before_events
        (await ConsistencyVerifier(session).verify()).require_ok()


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
    assert "Canonical Planning assessment" in response
    assert "основание" in response
    async with session_factory() as session:
        details = await PlanningQueryService(session).get_staleness_details(version_id)
        assert details == {
            "is_basis_stale": True,
            "changed_basis": [{"role": "GOAL", "label": "Основание плана"}],
        }
        version_count = (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one()
        assert version_count == 1


async def test_plain_basis_change_is_stale_without_candidate_until_explicit_replan(
    session_factory,
) -> None:
    goal_id, plan_id, approved_id = await seed_basis_approved(session_factory)
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=basis_update_then_candidate_model(
            goal_id=goal_id,
            plan_id=plan_id,
            based_on_version_id=approved_id,
        ),
    ).handle_message(incoming(4, "Книгу теперь надо закончить за 5 дней."))
    assert "устарел" in response
    assert "Хочешь" in response
    async with session_factory() as session:
        goal = await session.get(Goal, goal_id)
        versions = list((await session.execute(select(PlanVersion))).scalars())
        assert goal.target_date is not None
        assert goal.target_date_type == DeadlineType.HARD
        assert (
            await session.execute(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.entity_id == goal_id,
                    Event.event_type == EventType.GOAL_UPDATED,
                )
            )
        ).scalar_one() == 1
        assert (
            await session.execute(
                select(func.count())
                .select_from(PlanGoalLink)
                .where(
                    PlanGoalLink.plan_id == plan_id,
                    PlanGoalLink.goal_id == goal_id,
                )
            )
        ).scalar_one() == 1
        assert [(version.id, version.status) for version in versions] == [
            (approved_id, PlanVersionStatus.APPROVED)
        ]

    assessment = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(5, "Наш текущий план всё ещё построен на актуальных данных?"))
    assert "Canonical Planning assessment" in assessment
    assert "основание" in assessment
    async with session_factory() as session:
        stale = await PlanningQueryService(session).assess_plan_staleness(approved_id)
        assert stale.is_stale is True
        assert [changed.entity_id for changed in stale.changed_basis] == [goal_id]
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            plan_id=plan_id,
            based_on_version_id=approved_id,
            summary="Пятидневный вариант",
            items=[{"ordinal": day, "title": f"Чтение: день {day}"} for day in range(1, 6)],
        ),
    ).handle_message(incoming(6, "Предложи обновлённый план на 5 дней."))
    async with session_factory() as session:
        versions = list(
            (
                await session.execute(select(PlanVersion).order_by(PlanVersion.version_number))
            ).scalars()
        )
        assert [version.status for version in versions] == [
            PlanVersionStatus.APPROVED,
            PlanVersionStatus.CANDIDATE,
        ]
        assert versions[0].id == approved_id
        assert (
            await PlanningQueryService(session).assess_plan_staleness(approved_id)
        ).is_stale is True
        candidate_basis = list(
            (
                await session.execute(
                    select(PlanVersionBasis).where(
                        PlanVersionBasis.plan_version_id == versions[1].id
                    )
                )
            ).scalars()
        )
        assert [(basis.entity_id, basis.role.value) for basis in candidate_basis] == [
            (goal_id, "GOAL")
        ]


async def test_deadline_basis_change_persists_for_legacy_approved_without_basis(
    session_factory,
) -> None:
    plan_id, approved_id, _ = await seed_approved(
        session_factory,
        items=[{"ordinal": day, "title": f"Чтение: день {day}"} for day in range(1, 15)],
    )
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(3, "Уточняю: книгу нужно закончить через пять дней."))
    assert "canonical основание" in response
    assert "устарел" in response
    async with session_factory() as session:
        versions = list((await session.execute(select(PlanVersion))).scalars())
        goal = (await session.execute(select(Goal))).scalar_one()
        link = (await session.execute(select(PlanGoalLink))).scalar_one()
        event_types = set((await session.execute(select(Event.event_type))).scalars())
        assert [(version.id, version.status) for version in versions] == [
            (approved_id, PlanVersionStatus.APPROVED)
        ]
        assert goal.target_date is not None
        assert goal.target_date_type == DeadlineType.HARD
        assert link.plan_id == plan_id
        assert link.goal_id == goal.entity_id
        assert EventType.GOAL_CREATED in event_types
        assert EventType.PLAN_GOAL_LINKED in event_types
        assessment = await PlanningQueryService(session).assess_plan_staleness(approved_id)
        assert assessment.is_stale is True
        assert [changed.entity_id for changed in assessment.changed_basis] == [goal.entity_id]
    after_restart = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(4, "Наш текущий план всё ещё построен на актуальных данных?"))
    assert "Canonical Planning assessment" in after_restart
    assert "основание" in after_restart


async def test_same_message_basis_change_and_explicit_replan_creates_candidate(
    session_factory,
) -> None:
    goal_id, plan_id, approved_id = await seed_basis_approved(session_factory)
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=basis_update_then_candidate_model(
            goal_id=goal_id,
            plan_id=plan_id,
            based_on_version_id=approved_id,
        ),
    ).handle_message(incoming(4, "Книгу надо закончить за 5 дней, перестрой план."))
    assert "Чтение: день 5" in response
    async with session_factory() as session:
        versions = list(
            (
                await session.execute(select(PlanVersion).order_by(PlanVersion.version_number))
            ).scalars()
        )
        assert [version.status for version in versions] == [
            PlanVersionStatus.APPROVED,
            PlanVersionStatus.CANDIDATE,
        ]


async def test_planning_history_and_current_candidate_do_not_grant_replanning_intent(
    session_factory,
) -> None:
    goal_id, plan_id, approved_id = await seed_basis_approved(session_factory)
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            plan_id=plan_id,
            based_on_version_id=approved_id,
            summary="Недавний вариант",
            items=[{"ordinal": 1, "title": "Недавний шаг"}],
        ),
    ).handle_message(incoming(4, "Предложи другой вариант плана."))
    async with session_factory() as session:
        current_candidate = (
            await session.execute(
                select(PlanVersion).where(PlanVersion.status == PlanVersionStatus.CANDIDATE)
            )
        ).scalar_one()
        candidate_id = current_candidate.id
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=basis_update_then_candidate_model(
            goal_id=goal_id,
            plan_id=plan_id,
            based_on_version_id=candidate_id,
            create_new_plan=True,
        ),
    ).handle_message(incoming(5, "На самом деле книгу надо закончить уже через пять дней."))
    assert "Хочешь" in response
    async with session_factory() as session:
        versions = list((await session.execute(select(PlanVersion))).scalars())
        assert (await session.execute(select(func.count()).select_from(Plan))).scalar_one() == 1
        assert len(versions) == 2
        assert next(v for v in versions if v.id == approved_id).status == PlanVersionStatus.APPROVED
        assert (
            next(v for v in versions if v.id == candidate_id).status == PlanVersionStatus.CANDIDATE
        )


async def test_explicit_basis_update_and_replan_failure_rolls_back_atomically(
    session_factory,
) -> None:
    goal_id, plan_id, approved_id = await seed_basis_approved(session_factory)
    async with session_factory() as session:
        before_events = (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one()
    with pytest.raises(RuntimeError, match="failure after replanning"):
        await ElowynRuntime(
            session_factory=session_factory,
            model=basis_update_then_candidate_model(
                goal_id=goal_id,
                plan_id=plan_id,
                based_on_version_id=approved_id,
                fail_after_candidate=True,
            ),
        ).handle_message(incoming(4, "Книгу надо закончить за 5 дней, перестрой план."))
    async with session_factory() as session:
        goal = await session.get(Goal, goal_id)
        versions = list((await session.execute(select(PlanVersion))).scalars())
        assert goal.description is None
        assert [(version.id, version.status) for version in versions] == [
            (approved_id, PlanVersionStatus.APPROVED)
        ]
        assert (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one() == before_events
        (await ConsistencyVerifier(session).verify()).require_ok()


@pytest.mark.asyncio
async def test_same_plan_shorter_is_compact_presentation_without_new_version(
    session_factory,
) -> None:
    runtime = ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            summary="План чтения на 21 день",
            strategy="Читать небольшими блоками каждый день",
            rationale="Подробное обоснование, которое не нужно повторять в compact view",
            items=[
                {
                    "ordinal": 1,
                    "title": "Дни 1–7: первая часть",
                    "description": "Очень подробное описание первого этапа",
                },
                {"ordinal": 2, "title": "Дни 8–21: завершение"},
            ],
        ),
    )
    await runtime.handle_message(incoming(1, "Предложи план чтения на 21 день."))
    async with session_factory() as session:
        before_versions = (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one()
        before_presentations = (
            await session.execute(select(func.count()).select_from(PlanVersionPresentation))
        ).scalar_one()
        candidate = (await session.execute(select(PlanVersion))).scalar_one()

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(2, "Можешь этот же план написать короче? Очень длинно получилось"))

    assert response is not None
    assert "Пункты (2):" in response
    assert "Дни 1–7: первая часть" in response
    assert "Дни 8–21: завершение" in response
    assert "Очень подробное описание" not in response
    assert "Подробное обоснование" not in response
    async with session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one() == before_versions
        assert (
            await session.execute(select(func.count()).select_from(PlanVersionPresentation))
        ).scalar_one() == before_presentations + 1
        unchanged = await session.get(PlanVersion, candidate.id)
        assert unchanged is not None
        assert unchanged.status == PlanVersionStatus.CANDIDATE


@pytest.mark.asyncio
async def test_work_on_first_item_together_does_not_approve_or_mutate_progress(
    session_factory,
) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(
            items=[
                {
                    "ordinal": 1,
                    "title": "Сформулировать ключевую идею",
                    "description": "Запиши идею одним предложением.",
                }
            ]
        ),
    ).handle_message(incoming(1, "Предложи рабочий план."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model("approve_presented_candidate", {}, "План утверждён."),
    ).handle_message(incoming(2, "Да."))
    async with session_factory() as session:
        version = (await session.execute(select(PlanVersion))).scalar_one()
        progress = (await session.execute(select(PlanItemProgress))).scalar_one()
        progress_id = progress.plan_version_item_id
        progress_status = progress.status

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(3, "Сделай пока первый пункт вместе со мной"))

    assert response is not None
    assert response.startswith("Давай начнём вместе.")
    assert "Запиши идею одним предложением." in response
    async with session_factory() as session:
        unchanged_version = await session.get(PlanVersion, version.id)
        unchanged_progress = await session.get(PlanItemProgress, progress_id)
        assert unchanged_version is not None
        assert unchanged_version.status == PlanVersionStatus.APPROVED
        assert unchanged_progress is not None
        assert unchanged_progress.status == progress_status == PlanItemProgressStatus.NOT_STARTED
        assert (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_agent_context_canonically_records_rejected_candidate_history(
    session_factory,
) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=candidate_model(items=[{"ordinal": 1, "title": "Первый пункт"}]),
    ).handle_message(incoming(1, "Предложи план."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(2, "Отмени текущий предложенный вариант."))

    async with session_factory() as session:
        payload = json.loads(await PlanningQueryService(session).render_for_agent())
        plan = payload["plans"][0]
        assert plan["current_candidate"] is None
        assert plan["recent_version_history"] == [
            {"version_number": 1, "status": "REJECTED"}
        ]
        assert plan["title_semantics"] == "LINEAGE_LABEL_NOT_VERSION_DURATION"


@pytest.mark.asyncio
async def test_collaborative_next_action_skips_done_item_without_progress_mutation(
    session_factory,
) -> None:
    plan_id, version_id, item_ids = await seed_approved(
        session_factory,
        items=[
            {"ordinal": 1, "title": "День 1", "description": "Уже завершён."},
            {
                "ordinal": 2,
                "title": "Дни 2–8",
                "description": "Разберём следующий блок чтения.",
            },
        ],
    )
    await ElowynRuntime(
        session_factory=session_factory,
        model=one_tool_model(
            "update_approved_plan_progress",
            {"plan_id": str(plan_id), "ordinal": 1, "status": "DONE"},
        ),
    ).handle_message(incoming(3, "Первый пункт выполнен."))
    async with session_factory() as session:
        before = {
            item_id: (await session.get(PlanItemProgress, item_id)).status
            for item_id in item_ids
        }

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(4, "Сделай следующий пункт вместе со мной."))

    assert response is not None
    assert "Пункт 2 — «Дни 2–8»" in response
    assert "День 1" not in response
    assert "Разберём следующий блок чтения." in response
    async with session_factory() as session:
        version = await session.get(PlanVersion, version_id)
        after = {
            item_id: (await session.get(PlanItemProgress, item_id)).status
            for item_id in item_ids
        }
        assert version is not None
        assert version.status == PlanVersionStatus.APPROVED
        assert after == before == {
            item_ids[0]: PlanItemProgressStatus.DONE,
            item_ids[1]: PlanItemProgressStatus.NOT_STARTED,
        }


@pytest.mark.asyncio
async def test_exact_rejected_history_question_reads_v7_not_current_candidate(
    session_factory,
) -> None:
    plan_id, approved_id, _ = await seed_approved(
        session_factory,
        items=[{"ordinal": 1, "title": "Утверждённый пункт"}],
    )
    based_on = approved_id
    for number in range(2, 8):
        await ElowynRuntime(
            session_factory=session_factory,
            model=candidate_model(
                plan_id=plan_id,
                based_on_version_id=based_on,
                summary=("Вариант на 5 дней" if number == 7 else f"Вариант {number}"),
                items=[{"ordinal": 1, "title": f"Пункт версии {number}"}],
            ),
        ).handle_message(incoming(number + 1, f"Предложи вариант {number}."))
        async with session_factory() as session:
            current = (
                await session.execute(
                    select(PlanVersion).where(
                        PlanVersion.status == PlanVersionStatus.CANDIDATE
                    )
                )
            ).scalar_one()
            based_on = current.id
    await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(9, "Отмени текущий предложенный вариант."))
    async with session_factory() as session:
        before_events = (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one()

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(
        incoming(10, "Что стало с предыдущим отклонённым вариантом на 5 дней?")
    )

    assert response is not None
    assert "Версия v7 существовала и была отклонена" in response
    assert "нет текущего предложенного варианта для отклонения" not in response
    async with session_factory() as session:
        versions = list((await session.execute(select(PlanVersion))).scalars())
        assert len(versions) == 7
        assert next(version for version in versions if version.version_number == 7).status == (
            PlanVersionStatus.REJECTED
        )
        assert not any(version.status == PlanVersionStatus.CANDIDATE for version in versions)
        assert (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one() == before_events


@pytest.mark.asyncio
async def test_presence_small_talk_bypasses_planning_and_model_output(
    session_factory,
) -> None:
    _, version_id, item_ids = await seed_approved(
        session_factory,
        items=[{"ordinal": 1, "title": "Не упоминать этот пункт"}],
    )
    async with session_factory() as session:
        before_status = (await session.get(PlanItemProgress, item_ids[0])).status
        before_events = (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one()

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
        memory_service=object(),
    ).handle_message(incoming(3, "Ты тут?"))

    assert response == "Да, я здесь."
    assert "план" not in response.casefold()
    assert "stale" not in response.casefold()
    async with session_factory() as session:
        version = await session.get(PlanVersion, version_id)
        progress = await session.get(PlanItemProgress, item_ids[0])
        assert version is not None
        assert version.status == PlanVersionStatus.APPROVED
        assert progress is not None
        assert progress.status == before_status
        assert (
            await session.execute(select(func.count()).select_from(Event))
        ).scalar_one() == before_events


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
                    select(PlanVersion).where(PlanVersion.status == PlanVersionStatus.CANDIDATE)
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


async def test_v03_deterministic_runtime_acceptance_cycle(session_factory, monkeypatch) -> None:
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
                await session.execute(select(PlanVersion).order_by(PlanVersion.version_number))
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
        assert versions[0]["change_reason"]["user_trigger"]["evidence"][0]["text"] == (
            correction_text
        )
        assert (
            versions[0]["change_reason"]["assistant_rationale"]["classification"]
            == "ASSISTANT_RATIONALE_NOT_USER_MOTIVE"
        )
        contract = value["explainability_answer_contract"]
        assert "user_trigger evidence first" in contract
        assert "never as the user's motive" in contract
        assert "do not infer one" in contract
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
    assert "Canonical Planning assessment" in stale_response
    assert "основание" in stale_response
    async with session_factory() as session:
        details = await PlanningQueryService(session).get_staleness_details(second_id)
        assert details == {
            "is_basis_stale": True,
            "changed_basis": [{"role": "GOAL", "label": "Найти новую работу"}],
        }

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
