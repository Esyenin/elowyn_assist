"""Conversation UX contract for acceptance scenario 10.

These evals exercise the real Pydantic AI tool wiring with deterministic FunctionModel
responses. They do not claim to measure a production provider's language quality; provider
specific evals can reuse the same cases later.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

pydantic_ai = pytest.importorskip("pydantic_ai")

from pydantic_ai import ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

from elowyn.assistant.context import build_turn_prompt  # noqa: E402
from elowyn.assistant.tools import build_agent  # noqa: E402
from elowyn.domain.enums import ActorType  # noqa: E402
from elowyn.services.world_state import ActionContext  # noqa: E402

pytestmark = pytest.mark.asyncio


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def create_task(self, command, ctx):
        self.calls.append(("create_task", command))
        return SimpleNamespace(entity_id=uuid4())

    async def update_task(self, command, ctx):
        self.calls.append(("update_task", command))
        return SimpleNamespace(entity_id=command.entity_id)


class StaticQueryService:
    async def render_for_llm(self, search_text=None) -> str:
        return '{"tasks": []}'


def model_with_first_response(parts, *, final_text: str):
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=parts)
        return ModelResponse(parts=[TextPart(final_text)])

    return FunctionModel(model_function)


def make_agent(model, service: RecordingService):
    return build_agent(
        model=model,
        service=service,
        query_service=StaticQueryService(),
        action_context=ActionContext(ActorType.USER),
    )


async def test_unambiguous_statement_can_create_without_crud_language() -> None:
    service = RecordingService()
    model = model_with_first_response(
        [ToolCallPart(tool_name="create_task", args={"title": "Отправить отчёт"})],
        final_text="Запомнила: нужно отправить отчёт.",
    )
    agent = make_agent(model, service)
    prompt = build_turn_prompt(
        user_text="Мне нужно отправить отчёт.", world_state='{"tasks": []}', history=[]
    )

    result = await agent.run(prompt)

    assert [name for name, _ in service.calls] == ["create_task"]
    response = str(result.output)
    assert response == "Запомнила: нужно отправить отчёт."
    assert "entity_id" not in response.lower()
    assert "crud" not in response.lower()
    assert "sql" not in response.lower()


async def test_ambiguous_statement_does_not_silently_write_canonical_state() -> None:
    service = RecordingService()

    def ambiguous_model(messages, info):
        return ModelResponse(
            parts=[TextPart("Ты хочешь перенести дедлайн или пока только рассматриваешь 28-е?")]
        )

    agent = make_agent(FunctionModel(ambiguous_model), service)
    prompt = build_turn_prompt(
        user_text="Может, всё-таки 28-го.", world_state='{"tasks": []}', history=[]
    )

    result = await agent.run(prompt)

    assert service.calls == []
    assert "28" in str(result.output)


async def test_unambiguous_correction_uses_domain_tool_and_answers_semantically() -> None:
    service = RecordingService()
    task_id = uuid4()
    model = model_with_first_response(
        [
            ToolCallPart(
                tool_name="update_task",
                args={"entity_id": str(task_id), "deadline_at": "2026-08-28T00:00:00Z"},
            )
        ],
        final_text="Исправила дедлайн на 28 августа.",
    )
    agent = make_agent(model, service)
    prompt = build_turn_prompt(
        user_text="Нет, я имел в виду 28-е.",
        world_state=(
            '{"tasks": [{"entity_id": "'
            + str(task_id)
            + '", "title": "Отправить отчёт", "deadline_at": "2026-08-30T00:00:00+00:00"}]}'
        ),
        history=[],
    )

    result = await agent.run(prompt)

    assert [name for name, _ in service.calls] == ["update_task"]
    assert service.calls[0][1].entity_id == task_id
    response = str(result.output)
    assert response == "Исправила дедлайн на 28 августа."
    assert str(task_id) not in response
