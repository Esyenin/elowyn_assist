from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.assistant.tools import build_agent
from elowyn.db.base import Base
from elowyn.db.models import Conversation, ConversationSummary, Entity, Message
from elowyn.domain.enums import ActorType, MessageAuthor, SemanticCategory, TransportType
from elowyn.memory.deep import DeepMemoryRoute
from elowyn.memory.service import (
    EpistemicStatus,
    MemoryBackendError,
    MemoryProvenance,
    MemorySemantics,
    MemoryTemporal,
    RecalledMemory,
    RecallResult,
    Reflection,
)
from elowyn.services.deep_memory import (
    DeepMemoryConfig,
    DeepMemoryService,
    route_deep_memory,
)
from elowyn.services.world_state import ActionContext


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class FakeDeepBackend:
    def __init__(
        self,
        memories: tuple[RecalledMemory, ...] = (),
        *,
        reflection: str = "A repeated synthetic pattern.",
        unavailable: bool = False,
    ) -> None:
        self.memories = memories
        self.reflection_text = reflection
        self.unavailable = unavailable
        self.recall_calls = 0
        self.reflect_calls = 0
        self.last_recall_query = None
        self.last_reflect_query = None

    async def recall(self, query) -> RecallResult:
        self.recall_calls += 1
        self.last_recall_query = query
        if self.unavailable:
            raise MemoryBackendError("synthetic backend outage")
        return RecallResult(memories=self.memories)

    async def reflect(self, query) -> Reflection:
        self.reflect_calls += 1
        self.last_reflect_query = query
        if self.unavailable:
            raise MemoryBackendError("synthetic backend outage")
        return Reflection(
            text=self.reflection_text,
            evidence_backend_ids=("evidence-1", "evidence-2"),
        )


def _memory(
    message: Message,
    *,
    text: str,
    backend_id: str,
    category: SemanticCategory = SemanticCategory.FACT,
    status: EpistemicStatus = EpistemicStatus.MENTIONED,
) -> RecalledMemory:
    provenance = MemoryProvenance(
        conversation_id=message.conversation_id,
        message_id=message.id,
        role=message.author.value,
        occurred_at=message.sent_at,
    )
    return RecalledMemory(
        backend_id=backend_id,
        text=text,
        semantics=MemorySemantics(category=category, status=status),
        backend_kind="world",
        document_id=provenance.document_id,
        source=provenance,
        temporal=MemoryTemporal(mentioned_at=message.sent_at),
    )


async def _raw_message(session, text: str, *, minute: int = 0) -> Message:
    conversation = Conversation(transport=TransportType.INTERNAL)
    session.add(conversation)
    await session.flush()
    message = Message(
        conversation_id=conversation.id,
        author=MessageAuthor.USER,
        text=text,
        sent_at=datetime(2026, 8, 22, 10, minute, tzinfo=UTC),
    )
    session.add(message)
    await session.flush()
    return message


def test_deep_route_is_explicit_and_normal_turn_stays_fast() -> None:
    assert route_deep_memory("Show my current tasks") == DeepMemoryRoute.NONE
    assert route_deep_memory("What tea did I prefer last summer?") == DeepMemoryRoute.RECALL
    assert (
        route_deep_memory("What recurring pattern appears across our conversations?")
        == DeepMemoryRoute.REFLECT
    )
    assert (
        route_deep_memory("Что именно я тогда сказал?") == DeepMemoryRoute.EXACT_SOURCE
    )


@pytest.mark.asyncio
async def test_specific_fact_recall_is_relevant_bounded_and_provenance_preserving(
    session_factory,
) -> None:
    async with session_factory() as session:
        relevant = await _raw_message(session, "I preferred jasmine tea last summer.")
        irrelevant = await _raw_message(session, "I visited Lisbon once.", minute=1)
        backend = FakeDeepBackend(
            (
                _memory(
                    relevant,
                    text="User preferred jasmine tea last summer.",
                    backend_id="tea-memory",
                    category=SemanticCategory.PREFERENCE,
                    status=EpistemicStatus.PREFERRED,
                ),
                _memory(
                    irrelevant,
                    text="User visited Lisbon.",
                    backend_id="travel-memory",
                ),
            )
        )
        service = DeepMemoryService(
            session,
            backend,
            DeepMemoryConfig(recall_budget=600, backend_token_limit=300),
        )

        result = await service.recall("What tea did I prefer last summer?")

        assert result.available is True
        assert result.authoritative is False
        assert [item.backend_id for item in result.items] == ["tea-memory"]
        assert result.items[0].provenance.message_id == relevant.id
        assert result.items[0].authoritative is False
        assert "travel-memory" not in result.context
        assert "WORLD STATE win" in result.context
        assert result.token_upper_bound <= 600
        assert backend.last_recall_query.max_tokens == 300


@pytest.mark.asyncio
async def test_conflicting_history_is_preserved_without_becoming_authoritative(
    session_factory,
) -> None:
    async with session_factory() as session:
        old = await _raw_message(session, "I preferred tea.")
        new = await _raw_message(session, "I now prefer coffee.", minute=1)
        backend = FakeDeepBackend(
            (
                _memory(
                    new,
                    text="User now prefers coffee.",
                    backend_id="new",
                    category=SemanticCategory.PREFERENCE,
                    status=EpistemicStatus.PREFERRED,
                ),
                _memory(
                    old,
                    text="User preferred tea.",
                    backend_id="old",
                    category=SemanticCategory.PREFERENCE,
                    status=EpistemicStatus.PREFERRED,
                ),
            )
        )

        result = await DeepMemoryService(session, backend).recall(
            "How did my drink preference change from tea to coffee?"
        )

        assert [item.backend_id for item in result.items] == ["new", "old"]
        assert all(item.authoritative is False for item in result.items)
        assert "preserve contradictions/history" in result.context


@pytest.mark.asyncio
async def test_reflect_is_bounded_derived_synthesis_not_an_exact_source(session_factory) -> None:
    async with session_factory() as session:
        backend = FakeDeepBackend(reflection="pattern " * 1000)
        service = DeepMemoryService(
            session,
            backend,
            DeepMemoryConfig(reflect_budget=420, backend_token_limit=350),
        )

        result = await service.reflect("What recurring planning pattern appears over time?")
        exact = await service.exact_source("evidence-1")

        assert result.available is True
        assert result.authoritative is False
        assert result.truncated is True
        assert result.token_upper_bound <= 420
        assert result.evidence_backend_ids == ("evidence-1", "evidence-2")
        assert "not an exact quote" in result.synthesis
        assert backend.last_reflect_query.max_tokens == 350
        assert exact.found is False


@pytest.mark.asyncio
async def test_exact_quote_comes_only_from_canonical_raw_message(session_factory) -> None:
    async with session_factory() as session:
        raw = await _raw_message(
            session,
            'I said exactly: "Use the blue notebook, not the summary."',
        )
        before = Message(
            conversation_id=raw.conversation_id,
            author=MessageAuthor.ASSISTANT,
            text="Which notebook should we use?",
            sent_at=datetime(2026, 8, 22, 9, 59, tzinfo=UTC),
        )
        after = Message(
            conversation_id=raw.conversation_id,
            author=MessageAuthor.ASSISTANT,
            text="Understood; the blue notebook.",
            sent_at=datetime(2026, 8, 22, 10, 1, tzinfo=UTC),
        )
        session.add_all((before, after))
        await session.flush()
        backend = FakeDeepBackend(
            (
                _memory(
                    raw,
                    text="The user preferred a blue notebook.",
                    backend_id="paraphrased-memory",
                ),
            )
        )
        session.add(
            ConversationSummary(
                conversation_id=raw.conversation_id,
                short_summary="A lossy summary says only that a notebook was discussed.",
                topics=["notebook"],
                related_entity_ids=[],
                last_processed_message_id=raw.id,
                derivation_version="synthetic-summary-v1",
            )
        )
        await session.flush()
        service = DeepMemoryService(session, backend)

        recalled = await service.recall("What exactly did I say about the blue notebook?")
        source = await service.exact_source(recalled.items[0].provenance.source_ref)

        assert recalled.items[0].text != raw.text
        assert source.found is True
        assert source.canonical_raw_source is True
        assert source.world_state_authority is False
        assert source.raw_text == raw.text
        assert "lossy summary" not in source.raw_text
        assert source.message_id == raw.id
        assert source.sent_at == raw.sent_at
        assert [item.raw_text for item in source.surrounding_context] == [
            before.text,
            after.text,
        ]
        assert source.context_complete is True
        assert source.token_upper_bound <= 2048


@pytest.mark.asyncio
async def test_deep_result_count_and_size_remain_bounded(session_factory) -> None:
    async with session_factory() as session:
        memories = []
        for index in range(12):
            raw = await _raw_message(session, f"Python history evidence {index}.", minute=index)
            memories.append(
                _memory(
                    raw,
                    text=f"Python historical detail {index}: " + ("bounded " * 100),
                    backend_id=f"memory-{index}",
                )
            )
        service = DeepMemoryService(
            session,
            FakeDeepBackend(tuple(memories)),
            DeepMemoryConfig(recall_budget=520, result_limit=2),
        )

        result = await service.recall("Recall old Python history details")

        assert len(result.items) <= 2
        assert result.token_upper_bound <= 520
        assert result.truncated is True


@pytest.mark.asyncio
async def test_deep_backend_failure_degrades_safely_and_does_not_write_world_state(
    session_factory,
) -> None:
    async with session_factory() as session:
        service = DeepMemoryService(session, FakeDeepBackend(unavailable=True))

        recalled = await service.recall("What did I prefer last time?")
        reflected = await service.reflect("What pattern appeared over time?")
        entity_count = await session.scalar(select(func.count()).select_from(Entity))

        assert recalled.available is False
        assert recalled.items == ()
        assert "do not infer" in recalled.context
        assert reflected.available is False
        assert "do not invent" in reflected.synthesis
        assert entity_count == 0


class _StaticQueryService:
    async def render_for_llm(self, search_text=None) -> str:
        return '{"tasks": []}'


@pytest.mark.asyncio
async def test_explicit_recall_route_exposes_read_only_tool_only_when_needed(
    session_factory,
) -> None:
    async with session_factory() as session:
        raw = await _raw_message(session, "I preferred jasmine tea last summer.")
        backend = FakeDeepBackend(
            (
                _memory(
                    raw,
                    text="User preferred jasmine tea last summer.",
                    backend_id="tea-memory",
                ),
            )
        )
        deep = DeepMemoryService(session, backend)
        calls = 0
        exposed_tools: set[str] = set()

        def model_function(messages, info):
            nonlocal calls
            calls += 1
            exposed_tools.update(tool.name for tool in info.function_tools)
            if calls == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="recall_long_term_memory",
                            args={"query": "What tea did I prefer last summer?"},
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart("You preferred jasmine tea last summer.")])

        agent = build_agent(
            model=FunctionModel(model_function),
            service=SimpleNamespace(),
            query_service=_StaticQueryService(),
            action_context=ActionContext(ActorType.USER),
            deep_memory_service=deep,
            deep_memory_route=DeepMemoryRoute.RECALL,
        )

        result = await agent.run("Explicit old-memory question")

        assert str(result.output) == "You preferred jasmine tea last summer."
        assert backend.recall_calls == 1
        assert exposed_tools == {
            "lookup_exact_memory_source",
            "query_world_state",
            "recall_long_term_memory",
        }
        assert "create_task" not in exposed_tools
