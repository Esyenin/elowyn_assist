from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.base import Base
from elowyn.db.models import (
    Event,
    Goal,
    Message,
    Plan,
    PlanItemProgress,
    PlanVersion,
    Project,
    Task,
)
from elowyn.domain.enums import MessageAuthor
from elowyn.provider import MODEL_HTTP_MAX_RETRIES, build_runtime_model
from elowyn.runtime import ElowynRuntime
from elowyn.support.database_safety import assert_test_database_url
from elowyn.support.model_errors import classify_transient_model_error
from elowyn.transport.telegram import (
    TEMPORARY_MODEL_ERROR_MESSAGE,
    TelegramAdapter,
    build_router,
)

pytestmark = pytest.mark.postgres
MODEL_NAME = "nvidia/nemotron-3-ultra-550b-a55b"


@pytest.fixture
async def session_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for provider resilience tests")
    assert_test_database_url(url)
    engine = create_async_engine(url)
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
    await engine.dispose()


class TelegramMessageDouble:
    def __init__(self, number: int, text_value: str, *, fail_delivery: bool = False) -> None:
        self.from_user = SimpleNamespace(id=777)
        self.chat = SimpleNamespace(id=987654)
        self.message_id = number
        self.text = text_value
        self.date = datetime(2026, 8, 25, 10, number, tzinfo=UTC)
        self.answers: list[str] = []
        self.fail_delivery = fail_delivery

    def model_dump(self, **kwargs) -> dict[str, object]:
        return {"message_id": self.message_id, "text": self.text}

    async def answer(self, text_value: str, *, parse_mode=None) -> None:
        assert parse_mode is None
        if self.fail_delivery:
            raise RuntimeError("synthetic Telegram outage")
        self.answers.append(text_value)


def _router(factory, model):
    runtime = ElowynRuntime(session_factory=factory, model=model)
    return runtime, build_router(
        runtime.handle_message,
        adapter=TelegramAdapter(allowed_user_id=777),
    )


def _http_error(status_code: int) -> ModelHTTPError:
    return ModelHTTPError(
        status_code=status_code,
        model_name=MODEL_NAME,
        body={"error": "synthetic provider failure"},
    )


def _failing_model(status_code: int = 500) -> FunctionModel:
    def model_function(messages, info):
        raise _http_error(status_code)

    return FunctionModel(model_function)


def _candidate_model(*, fail_after_tool: bool) -> FunctionModel:
    def model_function(messages, info):
        tool_returns = [
            part
            for message in messages
            for part in message.parts
            if getattr(part, "part_kind", None) == "tool-return"
        ]
        if not tool_returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="create_plan_with_candidate",
                        args={
                            "plan": {"title": "Synthetic recovery plan"},
                            "candidate": {
                                "summary": "Synthetic candidate",
                                "proposed_strategy_snapshot": "One safe step",
                                "items": [{"ordinal": 1, "title": "Safe step"}],
                            },
                        },
                    )
                ]
            )
        if fail_after_tool:
            raise _http_error(500)
        placeholder = tool_returns[-1].content["presentation_placeholder"]
        return ModelResponse(parts=[TextPart(placeholder)])

    return FunctionModel(model_function)


async def _canonical_counts(factory) -> dict[str, int]:
    models = (Event, Plan, PlanVersion, PlanItemProgress, Goal, Task, Project)
    async with factory() as session:
        counts = {
            model.__tablename__: (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            for model in models
        }
        counts["user_messages"] = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.author == MessageAuthor.USER)
            )
        ).scalar_one()
        counts["assistant_messages"] = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.author == MessageAuthor.ASSISTANT)
            )
        ).scalar_one()
        return counts


@pytest.mark.asyncio
async def test_500_before_tool_preserves_only_committed_raw_user_message(session_factory) -> None:
    _, router = _router(session_factory, _failing_model())
    message = TelegramMessageDouble(1, "Synthetic failed turn")

    await router.message.handlers[0].callback(message)

    assert message.answers == [TEMPORARY_MODEL_ERROR_MESSAGE]
    assert await _canonical_counts(session_factory) == {
        "events": 0,
        "plans": 0,
        "plan_versions": 0,
        "plan_item_progress": 0,
        "goals": 0,
        "tasks": 0,
        "projects": 0,
        "user_messages": 1,
        "assistant_messages": 0,
    }


@pytest.mark.asyncio
async def test_polling_path_accepts_a_normal_turn_after_500(session_factory) -> None:
    calls = 0

    def recovering_model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _http_error(500)
        return ModelResponse(parts=[TextPart("Снова работаю.")])

    _, router = _router(session_factory, FunctionModel(recovering_model))
    failed = TelegramMessageDouble(1, "First turn")
    recovered = TelegramMessageDouble(2, "Second turn")

    await router.message.handlers[0].callback(failed)
    await router.message.handlers[0].callback(recovered)

    assert failed.answers == [TEMPORARY_MODEL_ERROR_MESSAGE]
    assert recovered.answers == ["Снова работаю."]
    assert calls == 2


@pytest.mark.asyncio
async def test_timeout_gets_the_same_safe_temporary_response(session_factory) -> None:
    def timeout_model(messages, info):
        request = httpx.Request("POST", "https://synthetic.invalid/v1/chat")
        timeout = httpx.ReadTimeout("synthetic timeout", request=request)
        provider_error = ModelAPIError(model_name=MODEL_NAME, message="Request timed out")
        provider_error.__cause__ = timeout
        raise provider_error

    _, router = _router(session_factory, FunctionModel(timeout_model))
    message = TelegramMessageDouble(1, "Timeout turn")

    await router.message.handlers[0].callback(message)

    assert message.answers == [TEMPORARY_MODEL_ERROR_MESSAGE]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_auth_and_config_4xx_are_not_masked_as_transient(
    session_factory, status_code
) -> None:
    _, router = _router(session_factory, _failing_model(status_code))
    message = TelegramMessageDouble(1, "Misconfigured turn")

    with pytest.raises(ModelHTTPError) as raised:
        await router.message.handlers[0].callback(message)

    assert raised.value.status_code == status_code
    assert message.answers == []


@pytest.mark.asyncio
async def test_failed_post_tool_request_rolls_back_and_same_message_replay_is_safe(
    session_factory,
) -> None:
    runtime, router = _router(session_factory, _candidate_model(fail_after_tool=True))
    message = TelegramMessageDouble(1, "Create one synthetic candidate")

    await router.message.handlers[0].callback(message)
    failed_counts = await _canonical_counts(session_factory)
    assert failed_counts["user_messages"] == 1
    assert failed_counts["plans"] == failed_counts["plan_versions"] == 0

    runtime.model = _candidate_model(fail_after_tool=False)
    replay = TelegramMessageDouble(1, "Create one synthetic candidate")
    await router.message.handlers[0].callback(replay)

    recovered_counts = await _canonical_counts(session_factory)
    assert recovered_counts["user_messages"] == 1
    assert recovered_counts["plans"] == recovered_counts["plan_versions"] == 1
    assert recovered_counts["events"] > 0


@pytest.mark.asyncio
async def test_runtime_does_not_retry_whole_agent_turn(session_factory) -> None:
    calls = 0

    def counted_failure(messages, info):
        nonlocal calls
        calls += 1
        raise _http_error(503)

    _, router = _router(session_factory, FunctionModel(counted_failure))

    await router.message.handlers[0].callback(TelegramMessageDouble(1, "One attempt"))

    assert calls == 1


@pytest.mark.asyncio
async def test_failed_temporary_notice_is_logged_without_state_corruption(
    session_factory, caplog
) -> None:
    _, router = _router(session_factory, _failing_model())
    message = TelegramMessageDouble(1, "Failed delivery", fail_delivery=True)

    with caplog.at_level(logging.ERROR, logger="elowyn.transport.telegram"):
        await router.message.handlers[0].callback(message)

    assert "Failed to deliver temporary model-error response" in caplog.text
    counts = await _canonical_counts(session_factory)
    assert counts["user_messages"] == 1
    assert counts["events"] == counts["plan_versions"] == 0


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_only_supported_5xx_statuses_are_classified_transient(status_code) -> None:
    details = classify_transient_model_error(_http_error(status_code))

    assert details is not None
    assert details.status_code == status_code
    assert details.model_name == MODEL_NAME


def test_runtime_provider_has_explicit_bounded_http_retry(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "synthetic-key")
    monkeypatch.setenv("NVIDIA_MODEL", MODEL_NAME)

    model = build_runtime_model()

    assert MODEL_HTTP_MAX_RETRIES == 2
    assert model._provider.client.max_retries == MODEL_HTTP_MAX_RETRIES
