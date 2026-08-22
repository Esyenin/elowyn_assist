from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import elowyn.runtime as runtime_module
from elowyn.domain.enums import TransportType
from elowyn.domain.messages import IncomingMessage
from elowyn.runtime import ElowynRuntime


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeConversationService:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def ingest_user_message(self, incoming: IncomingMessage) -> object:
        return SimpleNamespace(
            conversation=SimpleNamespace(id=uuid.uuid4()),
            message=SimpleNamespace(id=uuid.uuid4()),
            source=SimpleNamespace(id=uuid.uuid4()),
            is_new=True,
        )

    async def recent_messages(self, conversation_id: uuid.UUID, *, limit: int) -> list[object]:
        return []

    async def has_assistant_reply(
        self, *, conversation_id: uuid.UUID, user_message_id: uuid.UUID
    ) -> bool:
        return False

    async def record_assistant_message(self, **kwargs: object) -> object:
        return SimpleNamespace(id=uuid.uuid4())


class FakeQueryService:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def render_for_llm(self) -> str:
        return "synthetic world state"


class FakeAgent:
    async def run(self, prompt: str) -> object:
        return SimpleNamespace(output="synthetic assistant response")


@pytest.mark.asyncio
async def test_turn_commits_and_returns_when_memory_wakeup_fails(monkeypatch) -> None:
    session = FakeSession()
    wake_attempts = 0

    def failed_wakeup() -> None:
        nonlocal wake_attempts
        wake_attempts += 1
        raise RuntimeError("synthetic memory outage")

    monkeypatch.setattr(runtime_module, "ConversationService", FakeConversationService)
    monkeypatch.setattr(runtime_module, "WorldStateQueryService", FakeQueryService)
    monkeypatch.setattr(runtime_module, "WorldStateService", lambda session: object())
    monkeypatch.setattr(runtime_module, "build_agent", lambda **kwargs: FakeAgent())
    monkeypatch.setattr(runtime_module, "build_turn_prompt", lambda **kwargs: "synthetic prompt")
    runtime = ElowynRuntime(
        session_factory=lambda: session,
        model=object(),
        memory_ingestion_wakeup=failed_wakeup,
    )
    incoming = IncomingMessage(
        transport=TransportType.INTERNAL,
        external_conversation_id="synthetic-conversation",
        external_message_id="synthetic-message",
        text="synthetic user message",
        sent_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
    )

    response = await runtime.handle_message(incoming)

    assert response == "synthetic assistant response"
    assert session.commits == 2
    assert session.rollbacks == 0
    assert wake_attempts == 2
