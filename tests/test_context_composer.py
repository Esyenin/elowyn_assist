from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import elowyn.runtime as runtime_module
from elowyn.assistant.context import build_turn_prompt
from elowyn.db.base import Base
from elowyn.db.models import Conversation, MemoryPage, Message
from elowyn.domain.enums import (
    MemoryPageType,
    MessageAuthor,
    ObservationStatus,
    SemanticCategory,
    TransportType,
)
from elowyn.domain.messages import IncomingMessage
from elowyn.memory.observations import PAGE_DERIVATION_VERSION
from elowyn.memory.service import (
    EpistemicStatus,
    MemoryBackendError,
    MemorySemantics,
    MemoryTemporal,
    RecalledMemory,
    RecallResult,
)
from elowyn.runtime import ElowynRuntime
from elowyn.services.context_composer import (
    ContextComposer,
    ContextComposerConfig,
)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class FakeMemory:
    def __init__(self, memories: tuple[RecalledMemory, ...] = (), *, unavailable: bool = False):
        self.memories = memories
        self.unavailable = unavailable
        self.recall_calls = 0
        self.last_query = None

    async def recall(self, query) -> RecallResult:
        self.recall_calls += 1
        self.last_query = query
        if self.unavailable:
            raise MemoryBackendError("synthetic outage")
        return RecallResult(memories=self.memories)


def _entry(
    statement: str,
    *,
    claim_key: str = "preference.communication.style",
    status: ObservationStatus = ObservationStatus.ACTIVE,
    confidence: float = 1.0,
) -> dict[str, object]:
    return {
        "observation_id": str(uuid.uuid4()),
        "claim_key": claim_key,
        "statement": statement,
        "category": SemanticCategory.PREFERENCE.value,
        "status": status.value,
        "confidence": confidence,
        "evidence_count": 2,
    }


async def _page(session, title: str, entries: list[dict[str, object]]) -> None:
    session.add(
        MemoryPage(
            page_type=MemoryPageType.COMMUNICATION_PREFERENCES,
            scope_key=title.casefold().replace(" ", "-"),
            title=title,
            entries=entries,
            max_entries=6,
            derivation_version=PAGE_DERIVATION_VERSION,
            refreshed_at=datetime.now(UTC),
        )
    )
    await session.flush()


def _recalled(text: str) -> RecalledMemory:
    return RecalledMemory(
        backend_id=f"memory-{uuid.uuid4()}",
        text=text,
        semantics=MemorySemantics(
            category=SemanticCategory.CONTEXT,
            status=EpistemicStatus.MENTIONED,
        ),
        backend_kind="world",
        document_id=None,
        source=None,
        temporal=MemoryTemporal(mentioned_at=datetime.now(UTC)),
    )


@pytest.mark.asyncio
async def test_relevant_previous_conversation_page_enters_context(session_factory) -> None:
    async with session_factory() as session:
        previous = Conversation(transport=TransportType.INTERNAL)
        current = Conversation(transport=TransportType.INTERNAL)
        session.add_all((previous, current))
        await session.flush()
        evidence = Message(
            conversation_id=previous.id,
            author=MessageAuthor.USER,
            text="Concise replies are best.",
            sent_at=datetime.now(UTC) - timedelta(days=1),
        )
        session.add(evidence)
        await _page(session, "Communication Preferences", [_entry("User prefers concise replies.")])

        memory = FakeMemory((_recalled("unneeded fallback"),))
        context = await ContextComposer(session).memory_context(
            user_text="Please give a concise project reply.",
            world_state='{"projects": []}',
            history=[],
        )

        assert context is not None
        assert "User prefers concise replies." in context.text
        assert "NON-AUTHORITATIVE" in context.text
        assert memory.recall_calls == 0


@pytest.mark.asyncio
async def test_irrelevant_page_does_not_trigger_backend_recall(session_factory) -> None:
    async with session_factory() as session:
        await _page(session, "Tea Preferences", [_entry("User prefers green tea.")])
        memory = FakeMemory((_recalled("User once visited Lisbon."),))

        context = await ContextComposer(session).memory_context(
            user_text="Help debug the Python parser.",
            world_state="{}",
            history=[],
        )

        assert context is None
        assert memory.recall_calls == 0


@pytest.mark.asyncio
async def test_memory_budget_and_item_limit_bound_repeated_history(session_factory) -> None:
    async with session_factory() as session:
        entries = [
            _entry(
                f"Python preference detail {index} remains intentionally compact.",
                claim_key=f"preference.python.{index}",
            )
            for index in range(20)
        ]
        await _page(session, "Python Preferences", entries)
        config = ContextComposerConfig(memory_token_budget=260, memory_item_limit=3)
        composer = ContextComposer(session, config)

        first = await composer.memory_context(
            user_text="Which Python preference matters?", world_state="{}", history=[]
        )
        second = await composer.memory_context(
            user_text="Which Python preference matters?", world_state="{}", history=[]
        )

        assert first is not None and second is not None
        assert first.text == second.text
        assert first.token_upper_bound <= 260
        assert first.item_count <= 3


@pytest.mark.asyncio
async def test_current_explicit_update_suppresses_old_memory(session_factory) -> None:
    async with session_factory() as session:
        await _page(session, "Drink Preference", [_entry("User prefers tea in the morning.")])

        context = await ContextComposer(session).memory_context(
            user_text="Actually, I now prefer coffee, not tea in the morning.",
            world_state="{}",
            history=[],
        )

        assert context is None or "prefers tea" not in context.text


@pytest.mark.asyncio
async def test_world_state_and_recent_context_are_not_duplicated(session_factory) -> None:
    async with session_factory() as session:
        statement = "Project Aurora uses PostgreSQL."
        await _page(
            session,
            "Project Aurora",
            [_entry(statement, claim_key="project.aurora.storage")],
        )
        conversation = Conversation(transport=TransportType.INTERNAL)
        session.add(conversation)
        await session.flush()
        recent = Message(
            conversation_id=conversation.id,
            author=MessageAuthor.USER,
            text=statement,
            sent_at=datetime.now(UTC),
        )

        from_world = await ContextComposer(session).memory_context(
            user_text="What storage does Aurora use?",
            world_state=f'{{"projects": [{{"description": "{statement}"}}]}}',
            history=[],
        )
        from_recent = await ContextComposer(session).memory_context(
            user_text="What storage does Aurora use?", world_state="{}", history=[recent]
        )

        assert from_world is None
        assert from_recent is None


@pytest.mark.asyncio
async def test_contested_observation_is_labeled_uncertain(session_factory) -> None:
    async with session_factory() as session:
        await _page(
            session,
            "Editor Preference",
            [
                _entry(
                    "User may prefer Vim for editing.",
                    status=ObservationStatus.CONTESTED,
                    confidence=0.55,
                )
            ],
        )

        context = await ContextComposer(session).memory_context(
            user_text="Which editor preference should we consider?",
            world_state="{}",
            history=[],
        )

        assert context is not None
        assert "CONTESTED/UNCERTAIN confidence=0.55" in context.text
        assert context.authoritative is False


@pytest.mark.asyncio
async def test_empty_memory_preserves_recent_message_prompt_behavior(session_factory) -> None:
    async with session_factory() as session:
        conversation = Conversation(transport=TransportType.INTERNAL)
        session.add(conversation)
        await session.flush()
        history = [
            Message(
                conversation_id=conversation.id,
                author=MessageAuthor.USER,
                text=f"history-{index}",
                sent_at=datetime.now(UTC) + timedelta(minutes=index),
            )
            for index in range(20)
        ]
        context = await ContextComposer(session).memory_context(
            user_text="unrelated current request", world_state="{}", history=history
        )
        prompt = build_turn_prompt(
            user_text="current", world_state="{}", history=history, memory_context=context
        )

        assert context is None
        assert "history-7" not in prompt
        assert "history-8" in prompt
        assert "history-19" in prompt
        assert "MEMORY (DERIVED" not in prompt


class _FakeAgent:
    def __init__(self) -> None:
        self.prompt = ""

    async def run(self, prompt: str) -> object:
        self.prompt = prompt
        return SimpleNamespace(output="normal response")


@pytest.mark.asyncio
async def test_hindsight_outage_does_not_break_real_turn_or_world_state(
    session_factory, monkeypatch
) -> None:
    agent = _FakeAgent()
    memory = FakeMemory(unavailable=True)
    monkeypatch.setattr(runtime_module, "build_agent", lambda **kwargs: agent)
    runtime = ElowynRuntime(
        session_factory=session_factory,
        model=object(),
        memory_service=memory,
    )
    incoming = IncomingMessage(
        transport=TransportType.INTERNAL,
        external_conversation_id="outage-conversation",
        external_message_id="outage-message",
        text="Show the current tasks.",
        sent_at=datetime.now(UTC),
    )

    response = await runtime.handle_message(incoming)

    assert response == "normal response"
    assert memory.recall_calls == 0
    assert "ТЕКУЩИЙ WORLD STATE (authoritative" in agent.prompt
    assert "MEMORY (DERIVED" not in agent.prompt
    async with session_factory() as session:
        messages = (await session.execute(select(Message))).scalars().all()
        assert [message.author for message in messages] == [
            MessageAuthor.USER,
            MessageAuthor.ASSISTANT,
        ]
