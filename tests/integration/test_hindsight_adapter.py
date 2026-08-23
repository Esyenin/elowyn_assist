from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.base import Base
from elowyn.db.models import (
    Conversation,
    MemoryIngestionReceipt,
    MemoryObservation,
    MemoryPage,
    Message,
)
from elowyn.domain.enums import MemoryPageType, MessageAuthor, TransportType
from elowyn.memory.generation import ActiveGenerationMemoryService
from elowyn.memory.hindsight import (
    METADATA_SCHEMA_VERSION,
    HindsightAdapter,
    HindsightBackendFactory,
    HindsightConfig,
    document_id_for,
)
from elowyn.memory.observations import ObservationCandidate, ObservationEvidence
from elowyn.memory.service import (
    EpistemicStatus,
    MemoryProvenance,
    MemorySource,
    RecallQuery,
    ReflectQuery,
    RetainMessage,
    SemanticCategory,
)
from elowyn.services.memory_consolidation import (
    MemoryPageService,
    ObservationConsolidationService,
)
from elowyn.services.memory_pipeline import (
    MemoryIngestionProcessor,
    MemoryPipelineConfig,
    ingestion_operation_id,
)
from elowyn.services.memory_provenance import MemoryProvenanceService
from elowyn.services.memory_rebuild import MemoryGenerationManager, MemoryRebuildConfig

pytestmark = pytest.mark.hindsight


@pytest.mark.asyncio
async def test_real_hindsight_091_retain_retry_recall_reflect() -> None:
    base_url = os.getenv("ELOWYN_TEST_HINDSIGHT_URL")
    if not base_url:
        pytest.skip("ELOWYN_TEST_HINDSIGHT_URL is not configured")
    bank_id = f"elowyn-adapter-{uuid.uuid4()}"
    adapter = await HindsightBackendFactory(
        base_url=base_url,
        timeout_seconds=120.0,
    ).create_clean(
        bank_id,
    )
    conversation_id = uuid.uuid4()
    message = RetainMessage(
        source=MemorySource(
            conversation_id=conversation_id,
            message_id=uuid.uuid4(),
            role="USER",
            occurred_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
        ),
        text="Synthetic user prefers cedar tea during morning planning.",
        topic_tags=("preferences",),
    )

    try:
        health = await adapter.health()
        assert health.ready is True
        assert health.api_version == "0.9.1"

        first = await adapter.retain((message,))
        retry = await adapter.retain((message,))
        assert retry.operation_id == first.operation_id

        recalled = await adapter.recall(
            RecallQuery(text="Which tea is preferred for morning planning?", max_tokens=1024)
        )
        matching = [item for item in recalled.memories if item.source == message.source]
        assert matching
        assert matching[0].document_id == document_id_for(conversation_id)
        assert matching[0].metadata["extraction_schema_version"] == METADATA_SCHEMA_VERSION
        assert matching[0].kind == SemanticCategory.PREFERENCE
        assert matching[0].semantics.status == EpistemicStatus.PREFERRED
        assert matching[0].provenance is not None
        assert matching[0].provenance.source_ref == f"elowyn:message:{message.source.message_id}"
        assert matching[0].authoritative is False

        reflected = await adapter.reflect(
            ReflectQuery(text="What is the synthetic morning planning preference?", max_tokens=512)
        )
        assert reflected.text.strip()
        assert reflected.authoritative is False
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_real_hindsight_091_full_pipeline_rebuild_and_outage_recovery() -> None:
    base_url = os.getenv("ELOWYN_TEST_HINDSIGHT_URL")
    if not base_url:
        pytest.skip("ELOWYN_TEST_HINDSIGHT_URL is not configured")
    api_key = os.getenv("ELOWYN_TEST_HINDSIGHT_API_KEY")
    backend = f"hindsight-0.9.1:integration:{uuid.uuid4()}"
    factory = HindsightBackendFactory(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=180.0,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    initial_bank = f"elowyn-pipeline-{uuid.uuid4()}"
    initial = await factory.create_clean(initial_bank)
    await initial.close()
    manager = MemoryGenerationManager(
        session_factory,
        factory,
        MemoryRebuildConfig(
            backend=backend,
            bank_prefix="elowyn-integration-rebuild",
            batch_size=2,
            verification_attempts=30,
            verification_delay_seconds=1,
        ),
    )
    await manager.bootstrap_existing(initial_bank)
    active = ActiveGenerationMemoryService(
        session_factory,
        backend=backend,
        factory=factory,
    )
    processor = MemoryIngestionProcessor(
        session_factory,
        active,
        MemoryPipelineConfig(
            backend=backend,
            batch_size=2,
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(seconds=2),
        ),
    )
    messages: list[Message] = []
    try:
        async with session_factory() as session:
            conversations = [
                Conversation(transport=TransportType.INTERNAL),
                Conversation(transport=TransportType.INTERNAL),
            ]
            session.add_all(conversations)
            await session.flush()
            for index, (conversation, text) in enumerate(
                [
                    (conversations[0], "I prefer concise synthetic technical answers."),
                    (
                        conversations[0],
                        "I prefer concise synthetic technical answers.",
                    ),
                    (conversations[1], "Maybe Elowyn could evaluate Neo4j later."),
                    (conversations[1], "Elowyn canonical state uses PostgreSQL."),
                ]
            ):
                message = Message(
                    conversation_id=conversation.id,
                    author=MessageAuthor.USER,
                    text=text,
                    sent_at=datetime(2026, 8, 23, 8, index, tzinfo=UTC),
                )
                session.add(message)
                messages.append(message)
            await session.commit()

        while await processor.process_once():
            pass
        recalled = await _wait_for_conversations(
            active,
            {
                message.conversation_id: message.text or ""
                for message in (messages[1], messages[-1])
            },
        )
        assert all(item.authoritative is False for item in recalled.memories)

        async with session_factory() as session:
            consolidation = ObservationConsolidationService(session)
            evidence = tuple(
                ObservationEvidence(
                    backend_memory_id=(
                        "operation:"
                        f"{ingestion_operation_id(backend=backend, message_id=message.id)}"
                    ),
                    provenance=MemoryProvenance(
                        conversation_id=message.conversation_id,
                        message_id=message.id,
                        role=message.author.value,
                        occurred_at=message.sent_at,
                    ),
                )
                for message in messages[:2]
            )
            observation = await consolidation.consolidate(
                ObservationCandidate(
                    claim_key="integration.communication.concise",
                    statement="User prefers concise synthetic technical answers.",
                    category=SemanticCategory.PREFERENCE,
                    evidence=evidence,
                    page_type=MemoryPageType.COMMUNICATION_PREFERENCES,
                    page_scope_key="user",
                    page_title="Communication Preferences",
                )
            )
            page = await MemoryPageService(session).refresh(
                page_type=MemoryPageType.COMMUNICATION_PREFERENCES,
                scope_key="user",
                title="Communication Preferences",
            )
            await session.commit()
            assert observation.authoritative is False
            assert page is not None and page.authoritative is False

        rebuilt = await manager.rebuild(explicit=True)
        assert rebuilt.bank_id != initial_bank
        after_rebuild = await _wait_for_conversations(
            active,
            {
                message.conversation_id: message.text or ""
                for message in (messages[1], messages[-1])
            },
        )
        cleanup_candidates = await manager.cleanup_candidates()
        assert [item.bank_id for item in cleanup_candidates] == [initial_bank]
        cleaned = await manager.cleanup_orphans(
            tuple(item.generation_id for item in cleanup_candidates),
            explicit=True,
        )
        assert cleaned == tuple(item.generation_id for item in cleanup_candidates)
        async with session_factory() as session:
            for item in after_rebuild.memories:
                if item.provenance is not None:
                    raw = await MemoryProvenanceService(session).resolve_message(item.provenance)
                    assert raw.id == item.provenance.message_id
            observation_count = await session.scalar(
                select(func.count()).select_from(MemoryObservation)
            )
            assert int(observation_count or 0)
            assert int(await session.scalar(select(func.count()).select_from(MemoryPage)) or 0)

            outage_conversation = Conversation(transport=TransportType.INTERNAL)
            session.add(outage_conversation)
            await session.flush()
            outage_message = Message(
                conversation_id=outage_conversation.id,
                author=MessageAuthor.USER,
                text="The recovered backend must remember this synthetic backlog message.",
                sent_at=datetime(2026, 8, 23, 9, 0, tzinfo=UTC),
            )
            session.add(outage_message)
            await session.commit()

        unavailable = HindsightAdapter(
            HindsightConfig(
                base_url="http://127.0.0.1:1",
                bank_id=rebuilt.bank_id,
                timeout_seconds=0.2,
            )
        )
        outage_processor = MemoryIngestionProcessor(
            session_factory,
            unavailable,
            MemoryPipelineConfig(
                backend=backend,
                retry_base=timedelta(seconds=1),
                retry_max=timedelta(seconds=1),
            ),
        )
        outage_time = datetime.now(UTC)
        assert await outage_processor.process_once(now=outage_time) is True
        assert await processor.process_once(now=outage_time + timedelta(seconds=2)) is True
        caught_up = await _wait_for_sources(
            active,
            {
                outage_message.id: (
                    outage_message.text or "",
                    outage_message.conversation_id,
                )
            },
        )
        assert any(
            item.provenance is not None and item.provenance.message_id == outage_message.id
            for item in caught_up.memories
        )
        async with session_factory() as session:
            receipts = int(
                await session.scalar(select(func.count()).select_from(MemoryIngestionReceipt)) or 0
            )
            assert receipts == len(messages) + 1
    finally:
        await active.close()
        await engine.dispose()


async def _wait_for_sources(memory, expected: dict[uuid.UUID, tuple[str, uuid.UUID]]):
    for _ in range(30):
        combined = []
        found: set[uuid.UUID] = set()
        for message_id, (query, _conversation_id) in expected.items():
            recalled = await memory.recall(
                RecallQuery(
                    text=query,
                    max_tokens=1024,
                )
            )
            combined.extend(recalled.memories)
            if any(
                item.provenance is not None
                and item.provenance.message_id == message_id
                for item in recalled.memories
            ):
                found.add(message_id)
        if expected.keys() <= found:
            return type(recalled)(memories=tuple(combined))
        await asyncio.sleep(1)
    raise AssertionError("Hindsight recall did not expose all retained canonical sources")


async def _wait_for_conversations(memory, expected: dict[uuid.UUID, str]):
    for _ in range(30):
        combined = []
        found: set[uuid.UUID] = set()
        for conversation_id, query in expected.items():
            recalled = await memory.recall(RecallQuery(text=query, max_tokens=1024))
            combined.extend(recalled.memories)
            if any(
                item.provenance is not None
                and item.provenance.conversation_id == conversation_id
                for item in recalled.memories
            ):
                found.add(conversation_id)
        if expected.keys() <= found:
            return type(recalled)(memories=tuple(combined))
        await asyncio.sleep(1)
    raise AssertionError("Hindsight recall did not expose all retained conversation documents")
