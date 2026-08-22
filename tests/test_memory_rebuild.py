from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.base import Base
from elowyn.db.models import (
    Conversation,
    ConversationSummary,
    Entity,
    Event,
    MemoryBackendRegistry,
    MemoryGeneration,
    MemoryObservation,
    MemoryObservationEvidence,
    MemoryPage,
    Message,
    Operation,
    Task,
)
from elowyn.domain.commands import TaskCreate
from elowyn.domain.enums import (
    ActorType,
    MemoryGenerationStatus,
    MessageAuthor,
    SemanticCategory,
    TransportType,
)
from elowyn.memory.generation import ActiveGenerationMemoryService
from elowyn.memory.rebuild import MemoryRebuildError
from elowyn.memory.semantics import classify_semantics
from elowyn.memory.service import (
    MemoryHealth,
    MemoryTemporal,
    RecalledMemory,
    RecallQuery,
    RecallResult,
    Reflection,
    ReflectQuery,
    RetainMessage,
    RetainResult,
)
from elowyn.services.memory_consolidation import ObservationConsolidationService
from elowyn.services.memory_pipeline import (
    MemoryIngestionProcessor,
    MemoryPipelineConfig,
)
from elowyn.services.memory_provenance import MemoryProvenanceService
from elowyn.services.memory_rebuild import (
    MemoryDiagnosticsService,
    MemoryGenerationManager,
    MemoryRebuildConfig,
)
from elowyn.services.world_state import ActionContext, WorldStateService

BACKEND = "synthetic-memory:generation-test"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class GenerationMemory:
    def __init__(self, bank_id: str, *, ready: bool = True) -> None:
        self.bank_id = bank_id
        self.ready = ready
        self.items: dict[uuid.UUID, RetainMessage] = {}
        self.operations: dict[uuid.UUID, uuid.UUID] = {}
        self.fail_at: int | None = None
        self.cancel_at: int | None = None
        self.retain_calls = 0

    async def health(self) -> MemoryHealth:
        return MemoryHealth(backend=self.bank_id, ready=self.ready)

    async def retain(
        self,
        messages: tuple[RetainMessage, ...],
        *,
        operation_id: uuid.UUID | None = None,
    ) -> RetainResult:
        self.retain_calls += 1
        if self.cancel_at == self.retain_calls:
            raise asyncio.CancelledError
        if self.fail_at == self.retain_calls:
            raise RuntimeError("synthetic mid-rebuild failure")
        assert operation_id is not None
        for message in messages:
            self.items[message.source.message_id] = message
            self.operations[message.source.message_id] = operation_id
        return RetainResult(operation_id=operation_id, accepted_items=len(messages))

    async def recall(self, query: RecallQuery) -> RecallResult:
        memories = tuple(
            RecalledMemory(
                backend_id=f"{self.bank_id}:{message.source.message_id}",
                text=message.text,
                semantics=message.semantics or classify_semantics(message.text),
                backend_kind="world",
                document_id=message.source.document_id,
                source=message.source,
                temporal=MemoryTemporal(mentioned_at=message.source.occurred_at),
            )
            for message in self.items.values()
        )
        return RecallResult(memories=memories)

    async def reflect(self, query: ReflectQuery) -> Reflection:
        return Reflection(text="synthetic derived reflection")

    async def close(self) -> None:
        return None


class GenerationFactory:
    def __init__(self) -> None:
        self.banks: dict[str, GenerationMemory] = {}
        self.created: list[str] = []
        self.next_fail_at: int | None = None
        self.next_cancel_at: int | None = None

    def open(self, bank_id: str) -> GenerationMemory:
        return self.banks.setdefault(bank_id, GenerationMemory(bank_id))

    async def create_clean(self, bank_id: str) -> GenerationMemory:
        assert bank_id not in self.banks
        memory = GenerationMemory(bank_id)
        memory.fail_at = self.next_fail_at
        memory.cancel_at = self.next_cancel_at
        self.next_fail_at = None
        self.next_cancel_at = None
        self.banks[bank_id] = memory
        self.created.append(bank_id)
        return memory


def _manager(session_factory, factory: GenerationFactory, **kwargs) -> MemoryGenerationManager:
    return MemoryGenerationManager(
        session_factory,
        factory,
        MemoryRebuildConfig(
            backend=BACKEND,
            bank_prefix="synthetic-bank",
            verification_attempts=1,
            verification_delay_seconds=0,
            **kwargs,
        ),
    )


async def _seed_archive_and_world_state(session_factory) -> tuple[list[Message], tuple[int, ...]]:
    async with session_factory() as session:
        await WorldStateService(session).create_task(
            TaskCreate(title="Canonical task survives rebuild"),
            ActionContext(ActorType.USER),
        )
        conversation_one = Conversation(transport=TransportType.INTERNAL)
        conversation_two = Conversation(transport=TransportType.INTERNAL)
        session.add_all((conversation_one, conversation_two))
        await session.flush()
        texts = [
            "I prefer concise replies.",
            "I prefer concise replies.",
            "Maybe we could try Neo4j.",
            "The service uses PostgreSQL.",
        ]
        messages = []
        for index, text in enumerate(texts):
            message = Message(
                conversation_id=conversation_one.id if index < 2 else conversation_two.id,
                author=MessageAuthor.USER,
                text=text,
                sent_at=datetime(2026, 8, 23, 10, index, tzinfo=UTC),
            )
            session.add(message)
            messages.append(message)
        await session.commit()
        counts = await _world_counts(session)
        return messages, counts


async def _world_counts(session) -> tuple[int, ...]:
    counts = []
    for model in (Entity, Task, Operation, Event):
        counts.append(int(await session.scalar(select(func.count()).select_from(model)) or 0))
    return tuple(counts)


async def _active_generation(session_factory) -> MemoryGeneration:
    async with session_factory() as session:
        registry = await session.get(MemoryBackendRegistry, BACKEND)
        assert registry is not None and registry.active_generation_id is not None
        generation = await session.get(MemoryGeneration, registry.active_generation_id)
        assert generation is not None
        return generation


@pytest.mark.asyncio
async def test_clean_full_rebuild_switches_atomically_and_rebuilds_derived_state(
    session_factory,
) -> None:
    messages, world_counts = await _seed_archive_and_world_state(session_factory)
    raw_snapshot = [(message.id, message.text, _utc(message.sent_at)) for message in messages]
    factory = GenerationFactory()
    factory.open("stable-bank")
    manager = _manager(session_factory, factory, batch_size=2)
    stable_id = await manager.bootstrap_existing("stable-bank")
    proxy = ActiveGenerationMemoryService(
        session_factory,
        backend=BACKEND,
        factory=factory,
    )
    assert (await proxy.health()).backend == "stable-bank"

    result = await manager.rebuild(explicit=True)

    assert result.generation_id != stable_id
    assert factory.created == [result.bank_id]
    rebuilt_bank = factory.banks[result.bank_id]
    assert set(rebuilt_bank.items) == {message.id for message in messages}
    assert len(rebuilt_bank.items) == len(messages)
    assert result.messages_replayed == len(messages)
    assert result.messages_verified == 2
    assert (await proxy.health()).backend == result.bank_id
    assert rebuilt_bank.items[messages[2].id].semantics.category == SemanticCategory.IDEA
    assert rebuilt_bank.items[messages[3].id].semantics.category == SemanticCategory.FACT

    async with session_factory() as session:
        registry = await session.get(MemoryBackendRegistry, BACKEND)
        assert registry is not None and registry.active_generation_id == result.generation_id
        stable = await session.get(MemoryGeneration, stable_id)
        assert stable is not None and stable.status == MemoryGenerationStatus.SUPERSEDED
        summaries = int(
            await session.scalar(select(func.count()).select_from(ConversationSummary)) or 0
        )
        observations = (await session.execute(select(MemoryObservation))).scalars().all()
        pages = int(await session.scalar(select(func.count()).select_from(MemoryPage)) or 0)
        assert summaries == 2
        assert {item.category for item in observations} >= {
            SemanticCategory.IDEA,
            SemanticCategory.FACT,
            SemanticCategory.PREFERENCE,
        }
        assert pages == 1
        evidence = (
            await session.execute(
                select(MemoryObservationEvidence).where(
                    MemoryObservationEvidence.message_id == messages[0].id
                )
            )
        ).scalar_one()
        preference = next(
            item for item in observations if item.category == SemanticCategory.PREFERENCE
        )
        provenance = await MemoryProvenanceService(session).resolve_message(
            next(
                item.provenance
                for item in (
                    await ObservationConsolidationService(session).view(preference.id)
                ).evidence
                if item.provenance.message_id == evidence.message_id
            )
        )
        assert provenance.id == messages[0].id
        current_raw = (
            (
                await session.execute(select(Message).order_by(Message.sent_at, Message.id))
            )
            .scalars()
            .all()
        )
        assert [(item.id, item.text, _utc(item.sent_at)) for item in current_raw] == raw_snapshot
        assert await _world_counts(session) == world_counts
    await proxy.close()


@pytest.mark.asyncio
async def test_second_rebuild_is_safe_and_does_not_create_logical_duplicates(
    session_factory,
) -> None:
    messages, _ = await _seed_archive_and_world_state(session_factory)
    factory = GenerationFactory()
    factory.open("stable-bank")
    manager = _manager(session_factory, factory)
    await manager.bootstrap_existing("stable-bank")

    first = await manager.rebuild(explicit=True)
    first_counts = None
    async with session_factory() as session:
        first_counts = (
            int(await session.scalar(select(func.count()).select_from(MemoryObservation)) or 0),
            int(await session.scalar(select(func.count()).select_from(MemoryPage)) or 0),
        )
    second = await manager.rebuild(explicit=True)

    assert second.generation_id != first.generation_id
    assert len(factory.banks[second.bank_id].items) == len(messages)
    async with session_factory() as session:
        second_counts = (
            int(await session.scalar(select(func.count()).select_from(MemoryObservation)) or 0),
            int(await session.scalar(select(func.count()).select_from(MemoryPage)) or 0),
        )
        first_generation = await session.get(MemoryGeneration, first.generation_id)
        assert first_generation is not None
        assert first_generation.status == MemoryGenerationStatus.SUPERSEDED
    assert second_counts == first_counts


@pytest.mark.asyncio
async def test_mid_rebuild_failure_preserves_old_active_generation_and_derivatives(
    session_factory,
) -> None:
    await _seed_archive_and_world_state(session_factory)
    factory = GenerationFactory()
    factory.open("stable-bank")
    manager = _manager(session_factory, factory)
    await manager.bootstrap_existing("stable-bank")
    working = await manager.rebuild(explicit=True)
    proxy = ActiveGenerationMemoryService(
        session_factory,
        backend=BACKEND,
        factory=factory,
    )
    assert (await proxy.health()).backend == working.bank_id
    factory.next_fail_at = 2

    with pytest.raises(MemoryRebuildError, match="memory rebuild failed"):
        await manager.rebuild(explicit=True)

    active = await _active_generation(session_factory)
    assert active.id == working.generation_id
    assert (await proxy.health()).backend == working.bank_id
    assert len(factory.banks[working.bank_id].items) == 4
    async with session_factory() as session:
        failed = (
            await session.execute(
                select(MemoryGeneration).where(
                    MemoryGeneration.status == MemoryGenerationStatus.FAILED
                )
            )
        ).scalar_one()
        assert failed.id != active.id
        assert int(await session.scalar(select(func.count()).select_from(MemoryPage)) or 0) == 1
    await proxy.close()


@pytest.mark.asyncio
async def test_interrupted_rebuild_is_detected_then_restart_builds_a_new_bank(
    session_factory,
) -> None:
    await _seed_archive_and_world_state(session_factory)
    factory = GenerationFactory()
    factory.open("stable-bank")
    manager = _manager(session_factory, factory, lease_for=timedelta(seconds=1))
    stable_id = await manager.bootstrap_existing("stable-bank")
    factory.next_cancel_at = 2

    with pytest.raises(asyncio.CancelledError):
        await manager.rebuild(explicit=True)
    assert (await _active_generation(session_factory)).id == stable_id

    recovered = await manager.recover_interrupted(now=datetime.now(UTC) + timedelta(seconds=2))
    result = await manager.rebuild(explicit=True)

    assert recovered == 1
    assert result.generation_id != stable_id
    async with session_factory() as session:
        failed_count = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryGeneration)
                .where(MemoryGeneration.status == MemoryGenerationStatus.FAILED)
            )
            or 0
        )
        assert failed_count == 1


@pytest.mark.asyncio
async def test_outage_backlog_recovery_catchup_and_diagnostics(session_factory) -> None:
    messages, _ = await _seed_archive_and_world_state(session_factory)
    factory = GenerationFactory()
    stable = factory.open("stable-bank")
    manager = _manager(session_factory, factory)
    await manager.bootstrap_existing("stable-bank")
    proxy = ActiveGenerationMemoryService(
        session_factory,
        backend=BACKEND,
        factory=factory,
    )
    diagnostics = MemoryDiagnosticsService(session_factory, proxy, backend=BACKEND)
    processor = MemoryIngestionProcessor(
        session_factory,
        proxy,
        MemoryPipelineConfig(
            backend=BACKEND,
            batch_size=10,
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(seconds=2),
        ),
    )
    stable.ready = False
    before = await diagnostics.snapshot()

    assert await processor.process_once(now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC)) is True
    failed = await diagnostics.snapshot()
    stable.ready = True
    assert await processor.process_once(now=datetime(2026, 8, 23, 12, 2, tzinfo=UTC)) is True
    assert await processor.process_once(now=datetime(2026, 8, 23, 12, 3, tzinfo=UTC)) is True
    assert await processor.process_once(now=datetime(2026, 8, 23, 12, 4, tzinfo=UTC)) is False
    recovered = await diagnostics.snapshot()
    recalled = await proxy.recall(RecallQuery(text="Neo4j idea", max_tokens=256))

    assert before.pending_message_count == len(messages)
    assert before.indexing_verified_by_readiness is False
    assert failed.failed_ingestion_count == 1
    assert recovered.pending_message_count == 0
    assert recovered.ingested_message_count == len(messages)
    assert any(item.provenance.message_id == messages[2].id for item in recalled.memories)
    assert recovered.active_bank_id == "stable-bank"
    await proxy.close()


@pytest.mark.asyncio
async def test_rebuild_requires_explicit_opt_in(session_factory) -> None:
    factory = GenerationFactory()
    manager = _manager(session_factory, factory)
    with pytest.raises(PermissionError, match="explicit"):
        await manager.rebuild()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
