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
    Message,
    Plan,
    PlanVersion,
    PlanVersionPresentation,
    Strategy,
    Task,
)
from elowyn.domain.enums import EventType, MessageAuthor, PlanVersionStatus, TransportType
from elowyn.domain.errors import DomainValidationError
from elowyn.domain.messages import IncomingMessage
from elowyn.runtime import ElowynRuntime
from elowyn.services.domain_mutation import DomainMutationService
from elowyn.support.database_safety import assert_test_database_url

pytestmark = pytest.mark.postgres


@pytest.fixture
async def session_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for Planning runtime tests")
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


def incoming(number: int, text: str) -> IncomingMessage:
    return IncomingMessage(
        transport=TransportType.INTERNAL,
        external_conversation_id="planning-runtime",
        external_message_id=str(number),
        text=text,
        sent_at=datetime.now(UTC),
    )


def returned_placeholder(messages) -> str:
    for message in reversed(messages):
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, dict) and "presentation_placeholder" in content:
                return content["presentation_placeholder"]
    raise AssertionError("planning tool did not return a placeholder")


def returned_placeholders(messages) -> list[str]:
    result: list[str] = []
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, dict) and "presentation_placeholder" in content:
                result.append(content["presentation_placeholder"])
    return result


def has_tool_return(messages) -> bool:
    return any(
        getattr(part, "part_kind", None) == "tool-return"
        for message in messages
        for part in message.parts
    )


def concrete_model(*, final_mode: str = "valid") -> FunctionModel:
    def model_function(messages, info):
        if not has_tool_return(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="create_plan_with_candidate",
                        args={
                            "plan": {"title": "План поиска работы"},
                            "candidate": {
                                "summary": "Конкретный поиск работы",
                                "proposed_strategy_snapshot": "Сначала проверить рынок",
                                "strategy_rationale_snapshot": "Так меньше риск",
                                "items": [
                                    {"ordinal": 1, "title": "Обновить резюме"},
                                    {"ordinal": 2, "title": "Выбрать вакансии"},
                                ],
                            },
                        },
                    )
                ]
            )
        if final_mode == "provider_failure":
            raise RuntimeError("synthetic provider failure after tool")
        if final_mode == "missing":
            return ModelResponse(parts=[TextPart("Предлагаю конкретный план.")])
        token = returned_placeholder(messages)
        return ModelResponse(parts=[TextPart(f"Предлагаю такой вариант:\n{token}\nОбсудим?")])

    return FunctionModel(model_function)


def revision_model(plan_id: UUID, version_id: UUID, *, include_placeholder: bool = True):
    def model_function(messages, info):
        if not has_tool_return(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="present_candidate_plan",
                        args={
                            "plan_id": str(plan_id),
                            "summary": "Исправленный план",
                            "proposed_strategy_snapshot": "Проверить рынок точечно",
                            "based_on_version_id": str(version_id),
                            "items": [{"ordinal": 1, "title": "Обновить резюме точечно"}],
                        },
                    )
                ]
            )
        if not include_placeholder:
            return ModelResponse(parts=[TextPart("Вот исправленный вариант.")])
        return ModelResponse(parts=[TextPart(returned_placeholder(messages))])

    return FunctionModel(model_function)


def show_model(plan_id: UUID) -> FunctionModel:
    def model_function(messages, info):
        if not has_tool_return(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="show_current_candidate",
                        args={"plan_id": str(plan_id)},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(f"Ещё раз:\n{returned_placeholder(messages)}")])

    return FunctionModel(model_function)


def decision_model(
    tool_name: str,
    *,
    plan_version_id: UUID | None = None,
    acknowledgement: str = "Договорились.",
) -> FunctionModel:
    def model_function(messages, info):
        if not has_tool_return(messages):
            args = {} if plan_version_id is None else {"plan_version_id": str(plan_version_id)}
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        return ModelResponse(parts=[TextPart(acknowledgement)])

    return FunctionModel(model_function)


def model_must_not_run() -> FunctionModel:
    def model_function(messages, info):
        raise AssertionError("canonical reject routing must bypass the model")

    return FunctionModel(model_function)


async def seed_approved_and_hidden_current_candidate(factory):
    await ElowynRuntime(
        session_factory=factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    await ElowynRuntime(
        session_factory=factory,
        model=decision_model("approve_presented_candidate"),
    ).handle_message(incoming(2, "Да."))
    async with factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        approved = (await session.execute(select(PlanVersion))).scalar_one()
        plan_id, approved_id = plan.entity_id, approved.id
    await ElowynRuntime(
        session_factory=factory,
        model=revision_model(plan_id, approved_id),
    ).handle_message(incoming(3, "Предложи новый вариант."))
    await ElowynRuntime(
        session_factory=factory,
        model=FunctionModel(
            lambda messages, info: ModelResponse(
                parts=[TextPart("Обсуждение продолжено без изменения Planning state.")]
            )
        ),
    ).handle_message(incoming(4, "Пока просто продолжим обсуждение."))
    async with factory() as session:
        candidate = (
            await session.execute(
                select(PlanVersion).where(PlanVersion.status == PlanVersionStatus.CANDIDATE)
            )
        ).scalar_one()
        return approved_id, candidate.id


def two_candidates_model() -> FunctionModel:
    def model_function(messages, info):
        placeholders = returned_placeholders(messages)
        if not placeholders:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="create_plan_with_candidate",
                        args={
                            "plan": {"title": "Вариант A"},
                            "candidate": {
                                "summary": "Первый вариант",
                                "proposed_strategy_snapshot": "Подход A",
                                "items": [{"ordinal": 1, "title": "Шаг A"}],
                            },
                        },
                    ),
                    ToolCallPart(
                        tool_name="create_plan_with_candidate",
                        args={
                            "plan": {"title": "Вариант B"},
                            "candidate": {
                                "summary": "Второй вариант",
                                "proposed_strategy_snapshot": "Подход B",
                                "items": [{"ordinal": 1, "title": "Шаг B"}],
                            },
                        },
                    ),
                ]
            )
        return ModelResponse(parts=[TextPart("\n\n".join(placeholders))])

    return FunctionModel(model_function)


async def counts(factory) -> dict[str, int]:
    async with factory() as session:
        return {
            "plans": (await session.execute(select(func.count()).select_from(Plan))).scalar_one(),
            "versions": (
                await session.execute(select(func.count()).select_from(PlanVersion))
            ).scalar_one(),
            "presentations": (
                await session.execute(select(func.count()).select_from(PlanVersionPresentation))
            ).scalar_one(),
            "assistant": (
                await session.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.author == MessageAuthor.ASSISTANT)
                )
            ).scalar_one(),
            "user": (
                await session.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.author == MessageAuthor.USER)
                )
            ).scalar_one(),
            "tasks": (await session.execute(select(func.count()).select_from(Task))).scalar_one(),
        }


async def test_brainstorming_does_not_create_candidate(session_factory) -> None:
    observed_tools: set[str] = set()

    def brainstorming(messages, info):
        observed_tools.update(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart("Можно рассмотреть несколько направлений.")])

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(brainstorming),
    ).handle_message(incoming(1, "Какие вообще варианты есть?"))
    assert response == "Можно рассмотреть несколько направлений."
    assert (await counts(session_factory))["versions"] == 0
    forbidden = {"update_plan_item_progress", "get_next_action"}
    assert forbidden.isdisjoint(observed_tools)


async def test_invalid_provider_basis_retries_without_orphan_plan(session_factory) -> None:
    invalid_entity_id, invalid_event_id = uuid4(), uuid4()

    def retry_model(messages, info):
        returns = [
            content
            for message in messages
            for part in message.parts
            if isinstance((content := getattr(part, "content", None)), dict)
        ]
        placeholders = [
            value["presentation_placeholder"]
            for value in returns
            if "presentation_placeholder" in value
        ]
        if placeholders:
            return ModelResponse(parts=[TextPart(placeholders[-1])])
        candidate = {
            "summary": "Provider retry candidate",
            "proposed_strategy_snapshot": "Use validated inputs",
            "items": [{"ordinal": 1, "title": "Validated step"}],
        }
        if not returns:
            candidate["basis"] = [
                {
                    "entity_id": str(invalid_entity_id),
                    "event_id": str(invalid_event_id),
                    "role": "GOAL",
                }
            ]
        else:
            assert returns[-1]["result"] == "candidate_not_created"
            assert returns[-1]["reason"] == "invalid_basis"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="create_plan_with_candidate",
                    args={"plan": {"title": "Provider retry plan"}, "candidate": candidate},
                )
            ]
        )

    response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(retry_model),
    ).handle_message(incoming(1, "Предложи синтетический план."))
    assert "Validated step" in response
    state = await counts(session_factory)
    assert state["plans"] == state["versions"] == state["presentations"] == 1


@pytest.mark.parametrize("words", ["Да.", "ок, делаем", "подходит", "давай так", "согласен"])
async def test_natural_approval_synonyms_approve_only_immediate_candidate(
    session_factory, words
) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model("approve_presented_candidate"),
    ).handle_message(incoming(2, words))
    assert response == "Договорились."
    async with session_factory() as session:
        version = (await session.execute(select(PlanVersion))).scalar_one()
        strategy = (await session.execute(select(Strategy))).scalar_one()
        assert version.status == PlanVersionStatus.APPROVED
        assert strategy.accepted_from_plan_version_id == version.id
        assert (await session.execute(select(func.count()).select_from(Task))).scalar_one() == 0


async def test_ambiguous_yes_and_no_presentation_do_not_approve(session_factory) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=two_candidates_model(),
    ).handle_message(incoming(1, "Покажи два цельных варианта."))
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model(
            "approve_presented_candidate",
            acknowledgement="Какой именно вариант ты подтверждаешь?",
        ),
    ).handle_message(incoming(2, "Да."))
    assert "Какой именно" in response
    async with session_factory() as session:
        versions = list((await session.execute(select(PlanVersion))).scalars())
        assert len(versions) == 2
        assert all(version.status == PlanVersionStatus.CANDIDATE for version in versions)
        assert (await session.execute(select(func.count()).select_from(Strategy))).scalar_one() == 0

    # The latest assistant acknowledgement contains no Presentation, so another
    # generic approval still has no canonical target even though Memory/context may know Plans.
    await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model(
            "approve_presented_candidate",
            acknowledgement="Сначала нужно выбрать конкретный вариант.",
        ),
    ).handle_message(incoming(3, "Да."))
    async with session_factory() as session:
        assert (
            await session.execute(
                select(func.count())
                .select_from(PlanVersion)
                .where(PlanVersion.status == PlanVersionStatus.APPROVED)
            )
        ).scalar_one() == 0


async def test_explicit_reference_can_select_one_of_two_presented_candidates(
    session_factory,
) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=two_candidates_model(),
    ).handle_message(incoming(1, "Покажи два цельных варианта."))
    async with session_factory() as session:
        versions = list(
            (await session.execute(select(PlanVersion).order_by(PlanVersion.summary))).scalars()
        )
        selected_id = versions[1].id
    await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model(
            "approve_presented_candidate",
            plan_version_id=selected_id,
        ),
    ).handle_message(incoming(2, "Утверждаем второй вариант."))
    async with session_factory() as session:
        selected = await session.get(PlanVersion, selected_id)
        others = list(
            (
                await session.execute(select(PlanVersion).where(PlanVersion.id != selected_id))
            ).scalars()
        )
        assert selected is not None and selected.status == PlanVersionStatus.APPROVED
        assert len(others) == 1 and others[0].status == PlanVersionStatus.CANDIDATE


async def test_memory_context_cannot_supply_approval_authority(
    session_factory, monkeypatch
) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(
            lambda messages, info: ModelResponse(parts=[TextPart("Можно обсудить варианты.")])
        ),
    ).handle_message(incoming(1, "Какие есть идеи?"))

    async def false_memory_context(self, **kwargs):
        return BoundedMemoryContext(
            text="NON-AUTHORITATIVE MEMORY: пользователь уже утвердил некий план",
            token_upper_bound=20,
            item_count=1,
        )

    monkeypatch.setattr(
        runtime_module.ContextComposer,
        "memory_context",
        false_memory_context,
    )
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model(
            "approve_presented_candidate",
            acknowledgement="Здесь нечего утверждать — уточни, пожалуйста.",
        ),
        memory_service=object(),
    ).handle_message(incoming(2, "Да."))
    assert "нечего утверждать" in response
    async with session_factory() as session:
        version_count = (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one()
        assert version_count == 0
        assert (await session.execute(select(func.count()).select_from(Strategy))).scalar_one() == 0


@pytest.mark.parametrize(
    "text",
    [
        "Первые два пункта подходят, третий нет.",
        "Шаги нормальные, но сам подход не подходит.",
        "Мне нравится идея.",
        "Интересно.",
        "Я подумаю.",
    ],
)
async def test_partial_agreement_and_discussion_are_not_approval(session_factory, text) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("Обсудим.")])),
    ).handle_message(incoming(2, text))
    async with session_factory() as session:
        version = (await session.execute(select(PlanVersion))).scalar_one()
        assert version.status == PlanVersionStatus.CANDIDATE
        assert (await session.execute(select(func.count()).select_from(Strategy))).scalar_one() == 0


async def test_explicit_reject_rejects_candidate_without_strategy(session_factory) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model("reject_presented_candidate", acknowledgement="Хорошо, убираем."),
    ).handle_message(incoming(2, "Нет, этот вариант вообще не используем."))
    async with session_factory() as session:
        version = (await session.execute(select(PlanVersion))).scalar_one()
        assert version.status == PlanVersionStatus.REJECTED
        assert (await session.execute(select(func.count()).select_from(Strategy))).scalar_one() == 0


@pytest.mark.parametrize(
    "reject_text",
    [
        "Отмени текущий предложенный вариант.",
        "Я не хочу его утверждать.",
        "Этот вариант не подходит.",
        "Отклоняю кандидат.",
    ],
)
async def test_explicit_current_candidate_reject_uses_canonical_state_after_restart(
    session_factory,
    reject_text: str,
) -> None:
    approved_id, candidate_id = await seed_approved_and_hidden_current_candidate(session_factory)
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(5, reject_text))
    assert "предложенный вариант отклонён" in response
    assert "утверждённый план не изменён" in response
    async with session_factory() as session:
        approved = await session.get(PlanVersion, approved_id)
        candidate = await session.get(PlanVersion, candidate_id)
        rejected_events = (
            await session.execute(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.event_type == EventType.PLAN_VERSION_REJECTED,
                    Event.entity_id == candidate.plan_id,
                )
            )
        ).scalar_one()
        current_candidate_count = (
            await session.execute(
                select(func.count())
                .select_from(PlanVersion)
                .where(PlanVersion.status == PlanVersionStatus.CANDIDATE)
            )
        ).scalar_one()
        assert approved.status == PlanVersionStatus.APPROVED
        assert candidate.status == PlanVersionStatus.REJECTED
        assert rejected_events == 1
        assert current_candidate_count == 0


async def test_explicit_current_candidate_reject_without_candidate_is_canonical_noop(
    session_factory,
) -> None:
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(1, "Отмени текущий предложенный вариант."))
    assert response == "Сейчас нет текущего предложенного варианта для отклонения."
    async with session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PlanVersion))
        ).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(Event))).scalar_one() == 0


@pytest.mark.parametrize("ambiguous_text", ["Отмени это.", "Не утверждай это."])
async def test_ambiguous_candidate_cancel_clarifies_without_mutation(
    session_factory,
    ambiguous_text: str,
) -> None:
    approved_id, candidate_id = await seed_approved_and_hidden_current_candidate(session_factory)
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=model_must_not_run(),
    ).handle_message(incoming(5, ambiguous_text))
    assert "отклонить" in response
    assert "только пока не утверждать" in response
    async with session_factory() as session:
        assert (await session.get(PlanVersion, approved_id)).status == PlanVersionStatus.APPROVED
        assert (await session.get(PlanVersion, candidate_id)).status == PlanVersionStatus.CANDIDATE
        assert (
            await session.execute(
                select(func.count())
                .select_from(Event)
                .where(Event.event_type == EventType.PLAN_VERSION_REJECTED)
            )
        ).scalar_one() == 0


async def test_keep_current_plan_language_does_not_reject_candidate(session_factory) -> None:
    approved_id, candidate_id = await seed_approved_and_hidden_current_candidate(session_factory)
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=FunctionModel(
            lambda messages, info: ModelResponse(
                parts=[TextPart("Оставляю Planning state без изменений.")]
            )
        ),
    ).handle_message(incoming(5, "Оставь пока текущий план."))
    assert "без изменений" in response
    async with session_factory() as session:
        assert (await session.get(PlanVersion, approved_id)).status == PlanVersionStatus.APPROVED
        assert (await session.get(PlanVersion, candidate_id)).status == PlanVersionStatus.CANDIDATE


async def test_concrete_revision_and_show_again_bind_exact_presentations(session_factory) -> None:
    first_response = await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    assert "Сохранённая стратегия этой версии:\nСначала проверить рынок" in first_response
    assert "1. Обновить резюме" in first_response
    assert "ELOWYN_PLAN_PRESENTATION" not in first_response
    assert await counts(session_factory) == {
        "plans": 1,
        "versions": 1,
        "presentations": 1,
        "assistant": 1,
        "user": 1,
        "tasks": 0,
    }
    async with session_factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        first = (await session.execute(select(PlanVersion))).scalar_one()
        presentation = (await session.execute(select(PlanVersionPresentation))).scalar_one()
        assistant = await session.get(Message, presentation.message_id)
        assert presentation.plan_version_id == first.id
        assert assistant.text == first_response
        assert "ELOWYN_PLAN_PRESENTATION" not in assistant.text
        plan_id, first_id = plan.entity_id, first.id

    await ElowynRuntime(
        session_factory=session_factory,
        model=revision_model(plan_id, first_id),
    ).handle_message(incoming(2, "Исправь предложенный вариант."))
    async with session_factory() as session:
        versions = list(
            (await session.execute(select(PlanVersion).order_by(PlanVersion.version_number)))
            .scalars()
            .all()
        )
        assert [version.status for version in versions] == [
            PlanVersionStatus.SUPERSEDED,
            PlanVersionStatus.CANDIDATE,
        ]
        assert versions[1].based_on_version_id == versions[0].id
    before_show = await counts(session_factory)
    show_response = await ElowynRuntime(
        session_factory=session_factory,
        model=show_model(plan_id),
    ).handle_message(incoming(3, "Покажи текущий план ещё раз."))
    after_show = await counts(session_factory)
    assert "Проверить рынок точечно" in show_response
    assert after_show["versions"] == before_show["versions"]
    assert after_show["presentations"] == before_show["presentations"] + 1


@pytest.mark.parametrize("failure_mode", ["provider_failure", "missing"])
async def test_failure_after_candidate_keeps_user_message_but_rolls_back_plan(
    session_factory, failure_mode
) -> None:
    runtime = ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(final_mode=failure_mode),
    )
    expected = RuntimeError if failure_mode == "provider_failure" else DomainValidationError
    with pytest.raises(expected):
        await runtime.handle_message(incoming(1, "Предложи конкретный план."))
    state = await counts(session_factory)
    assert state["user"] == 1
    assert state["plans"] == state["versions"] == state["presentations"] == 0
    assert state["assistant"] == 0


async def test_assistant_message_failure_rolls_back_candidate(session_factory, monkeypatch) -> None:
    async def fail_assistant_save(self, **kwargs):
        raise RuntimeError("synthetic assistant save failure")

    monkeypatch.setattr(
        runtime_module.ConversationService,
        "record_assistant_message",
        fail_assistant_save,
    )
    with pytest.raises(RuntimeError, match="assistant save"):
        await ElowynRuntime(
            session_factory=session_factory,
            model=concrete_model(),
        ).handle_message(incoming(1, "Предложи конкретный план."))
    state = await counts(session_factory)
    assert state["user"] == 1
    assert state["plans"] == state["versions"] == state["assistant"] == 0


async def test_presentation_failure_rolls_back_candidate_and_assistant(
    session_factory, monkeypatch
) -> None:
    original = DomainMutationService._append_event

    async def fail_presentation_event(self, **kwargs):
        if kwargs["event_type"] == EventType.PLAN_VERSION_PRESENTED:
            raise RuntimeError("synthetic presentation event failure")
        return await original(self, **kwargs)

    monkeypatch.setattr(
        runtime_module.PlanningService,
        "_append_event",
        fail_presentation_event,
    )
    with pytest.raises(RuntimeError, match="presentation event failure"):
        await ElowynRuntime(
            session_factory=session_factory,
            model=concrete_model(),
        ).handle_message(incoming(1, "Предложи конкретный план."))
    state = await counts(session_factory)
    assert state["user"] == 1
    assert state["plans"] == state["versions"] == state["assistant"] == 0


async def test_failed_revision_restores_previous_current_candidate(session_factory) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    async with session_factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        first = (await session.execute(select(PlanVersion))).scalar_one()
        plan_id, first_id = plan.entity_id, first.id
    with pytest.raises(DomainValidationError):
        await ElowynRuntime(
            session_factory=session_factory,
            model=revision_model(plan_id, first_id, include_placeholder=False),
        ).handle_message(incoming(2, "Исправь план."))
    async with session_factory() as session:
        versions = list((await session.execute(select(PlanVersion))).scalars().all())
        assert len(versions) == 1
        assert versions[0].id == first_id
        assert versions[0].status == PlanVersionStatus.CANDIDATE
        assert (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.author == MessageAuthor.USER)
            )
        ).scalar_one() == 2


async def test_new_candidate_approval_supersedes_old_approved_and_replaces_strategy(
    session_factory,
) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model("approve_presented_candidate"),
    ).handle_message(incoming(2, "Да."))
    async with session_factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        first = (await session.execute(select(PlanVersion))).scalar_one()
        plan_id, first_id = plan.entity_id, first.id

    await ElowynRuntime(
        session_factory=session_factory,
        model=revision_model(plan_id, first_id),
    ).handle_message(incoming(3, "Предложи новую версию."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model("approve_presented_candidate"),
    ).handle_message(incoming(4, "Давай так."))

    async with session_factory() as session:
        versions = list(
            (
                await session.execute(select(PlanVersion).order_by(PlanVersion.version_number))
            ).scalars()
        )
        strategy = (await session.execute(select(Strategy))).scalar_one()
        assert [version.status for version in versions] == [
            PlanVersionStatus.SUPERSEDED,
            PlanVersionStatus.APPROVED,
        ]
        assert strategy.accepted_from_plan_version_id == versions[1].id
        assert strategy.approach == versions[1].proposed_strategy_snapshot


async def test_reject_new_candidate_keeps_old_approved_and_strategy(session_factory) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model("approve_presented_candidate"),
    ).handle_message(incoming(2, "Да."))
    async with session_factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        first = (await session.execute(select(PlanVersion))).scalar_one()
        strategy = (await session.execute(select(Strategy))).scalar_one()
        plan_id, first_id = plan.entity_id, first.id
        strategy_version_id = strategy.accepted_from_plan_version_id

    await ElowynRuntime(
        session_factory=session_factory,
        model=revision_model(plan_id, first_id),
    ).handle_message(incoming(3, "Предложи новый вариант."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model("reject_presented_candidate"),
    ).handle_message(incoming(4, "Нет, этот вариант не подходит."))

    async with session_factory() as session:
        versions = list(
            (
                await session.execute(select(PlanVersion).order_by(PlanVersion.version_number))
            ).scalars()
        )
        strategy = (await session.execute(select(Strategy))).scalar_one()
        assert [version.status for version in versions] == [
            PlanVersionStatus.APPROVED,
            PlanVersionStatus.REJECTED,
        ]
        assert strategy.accepted_from_plan_version_id == strategy_version_id


async def test_stale_explicit_target_is_not_substituted_with_new_candidate(session_factory) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    async with session_factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        first = (await session.execute(select(PlanVersion))).scalar_one()
        plan_id, first_id = plan.entity_id, first.id
    await ElowynRuntime(
        session_factory=session_factory,
        model=revision_model(plan_id, first_id),
    ).handle_message(incoming(2, "Замени вариант."))
    response = await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model(
            "approve_presented_candidate",
            plan_version_id=first_id,
            acknowledgement="Этот старый вариант уже не является текущим.",
        ),
    ).handle_message(incoming(3, "Всё-таки утверждаем первый вариант."))
    assert "не является текущим" in response
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
        assert (await session.execute(select(func.count()).select_from(Strategy))).scalar_one() == 0


async def test_reprocessing_same_approval_message_is_idempotent(session_factory) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    approval = incoming(2, "Да.")
    runtime = ElowynRuntime(
        session_factory=session_factory,
        model=decision_model("approve_presented_candidate"),
    )
    assert await runtime.handle_message(approval) == "Договорились."
    assert await runtime.handle_message(approval) is None
    async with session_factory() as session:
        version = (await session.execute(select(PlanVersion))).scalar_one()
        approval_events = (
            await session.execute(
                select(func.count())
                .select_from(Event)
                .where(Event.event_type == EventType.PLAN_VERSION_APPROVED)
            )
        ).scalar_one()
        assert version.status == PlanVersionStatus.APPROVED
        assert approval_events == 1
        assert (await session.execute(select(func.count()).select_from(Strategy))).scalar_one() == 1


async def test_approval_acknowledgement_failure_rolls_back_approval(
    session_factory, monkeypatch
) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    original = runtime_module.ConversationService.record_assistant_message

    async def fail_acknowledgement(self, **kwargs):
        if kwargs["text"] == "Договорились.":
            raise RuntimeError("synthetic acknowledgement failure")
        return await original(self, **kwargs)

    monkeypatch.setattr(
        runtime_module.ConversationService,
        "record_assistant_message",
        fail_acknowledgement,
    )
    with pytest.raises(RuntimeError, match="acknowledgement failure"):
        await ElowynRuntime(
            session_factory=session_factory,
            model=decision_model("approve_presented_candidate"),
        ).handle_message(incoming(2, "Да."))
    async with session_factory() as session:
        version = (await session.execute(select(PlanVersion))).scalar_one()
        assert version.status == PlanVersionStatus.CANDIDATE
        assert version.approval_source_id is None
        assert (await session.execute(select(func.count()).select_from(Strategy))).scalar_one() == 0
        user_count = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.author == MessageAuthor.USER)
            )
        ).scalar_one()
        assert user_count == 2


async def test_strategy_acceptance_failure_restores_old_approved(
    session_factory, monkeypatch
) -> None:
    await ElowynRuntime(
        session_factory=session_factory,
        model=concrete_model(),
    ).handle_message(incoming(1, "Предложи конкретный план."))
    await ElowynRuntime(
        session_factory=session_factory,
        model=decision_model("approve_presented_candidate"),
    ).handle_message(incoming(2, "Да."))
    async with session_factory() as session:
        plan = (await session.execute(select(Plan))).scalar_one()
        first = (await session.execute(select(PlanVersion))).scalar_one()
        plan_id, first_id = plan.entity_id, first.id
    await ElowynRuntime(
        session_factory=session_factory,
        model=revision_model(plan_id, first_id),
    ).handle_message(incoming(3, "Предложи новую версию."))

    async def fail_strategy(self, **kwargs):
        raise RuntimeError("synthetic Strategy acceptance failure")

    monkeypatch.setattr(runtime_module.PlanningService, "_accept_strategy", fail_strategy)
    with pytest.raises(RuntimeError, match="Strategy acceptance failure"):
        await ElowynRuntime(
            session_factory=session_factory,
            model=decision_model("approve_presented_candidate"),
        ).handle_message(incoming(4, "Да."))
    async with session_factory() as session:
        versions = list(
            (
                await session.execute(select(PlanVersion).order_by(PlanVersion.version_number))
            ).scalars()
        )
        strategy = (await session.execute(select(Strategy))).scalar_one()
        assert [version.status for version in versions] == [
            PlanVersionStatus.APPROVED,
            PlanVersionStatus.CANDIDATE,
        ]
        assert strategy.accepted_from_plan_version_id == versions[0].id
