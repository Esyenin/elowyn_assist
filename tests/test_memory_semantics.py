from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.base import Base
from elowyn.db.models import Conversation, Message
from elowyn.domain.enums import MessageAuthor, TransportType
from elowyn.memory.hindsight import HindsightAdapter, HindsightConfig, document_id_for
from elowyn.memory.semantics import classify_semantics
from elowyn.memory.service import (
    EpistemicStatus,
    MemoryBackendError,
    RecallQuery,
    SemanticCategory,
)
from elowyn.services.memory_provenance import MemoryProvenanceService


@pytest.mark.parametrize(
    ("text", "category", "status"),
    [
        ("The deployment uses PostgreSQL.", SemanticCategory.FACT, EpistemicStatus.MENTIONED),
        (
            "I prefer concise morning updates.",
            SemanticCategory.PREFERENCE,
            EpistemicStatus.PREFERRED,
        ),
        (
            "Maybe we could try Neo4j.",
            SemanticCategory.IDEA,
            EpistemicStatus.CONSIDERED,
        ),
        (
            "We decided to keep PostgreSQL.",
            SemanticCategory.CONTEXT,
            EpistemicStatus.DECIDED,
        ),
        (
            "I am currently working on Elowyn memory.",
            SemanticCategory.CONTEXT,
            EpistemicStatus.CURRENTLY_TRUE,
        ),
        (
            "Production data must not leave the local environment.",
            SemanticCategory.CONSTRAINT,
            EpistemicStatus.CURRENTLY_TRUE,
        ),
        (
            "Yesterday we visited the archive.",
            SemanticCategory.EPISODE,
            EpistemicStatus.MENTIONED,
        ),
    ],
)
def test_semantic_categories_preserve_claim_modality(
    text: str,
    category: SemanticCategory,
    status: EpistemicStatus,
) -> None:
    semantics = classify_semantics(text)

    assert semantics.category == category
    assert semantics.status == status


def test_mixed_message_stays_conservative_instead_of_promoting_possible_idea() -> None:
    semantics = classify_semantics(
        "Мне обычно нравятся короткие ответы, но для Elowyn давай подробно. "
        "И возможно потом попробуем Neo4j."
    )

    assert semantics.category == SemanticCategory.IDEA
    assert semantics.status == EpistemicStatus.CONSIDERED
    assert semantics.status not in {EpistemicStatus.PREFERRED, EpistemicStatus.CURRENTLY_TRUE}


def test_uncertain_statement_is_not_current_truth() -> None:
    semantics = classify_semantics("Rust might be useful for one component.")

    assert semantics.category == SemanticCategory.IDEA
    assert semantics.status == EpistemicStatus.CONSIDERED


def test_adversarial_dataset_preserves_modality_and_temporal_distinctions() -> None:
    dataset = json.loads(
        (Path(__file__).parent / "fixtures" / "memory_v02_golden.json").read_text(
            encoding="utf-8"
        )
    )
    fragments = {item["id"]: item["text"] for item in dataset["fragments"]}

    assert classify_semantics(fragments["idea-neo4j"]).status == EpistemicStatus.CONSIDERED
    assert classify_semantics(fragments["decision-postgres"]).status == EpistemicStatus.DECIDED
    assert classify_semantics(fragments["discussion-kafka"]).status != EpistemicStatus.DECIDED
    assert classify_semantics(fragments["fact-storage-old"]).status != (
        EpistemicStatus.CURRENTLY_TRUE
    )
    assert (
        classify_semantics(fragments["fact-storage-current"]).status
        == EpistemicStatus.CURRENTLY_TRUE
    )
    assert classify_semantics(fragments["temporary-coffee"]).status != (
        EpistemicStatus.PREFERRED
    )
    assert classify_semantics(fragments["uncertain-rust"]).status != (
        EpistemicStatus.CURRENTLY_TRUE
    )


class RecallClient:
    def __init__(self, results: list[object]) -> None:
        self.results = results

    async def aget_version(self) -> object:
        return SimpleNamespace(api_version="0.9.1")

    async def arecall(self, **kwargs: object) -> object:
        return SimpleNamespace(results=self.results)


def _metadata(
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    timestamp: datetime,
    category: SemanticCategory = SemanticCategory.FACT,
    status: EpistemicStatus = EpistemicStatus.MENTIONED,
) -> dict[str, str]:
    return {
        "conversation_id": str(conversation_id),
        "message_id": str(message_id),
        "source_ref": f"elowyn:message:{message_id}",
        "document_id": document_id_for(conversation_id),
        "role": "USER",
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "semantic_category": category.value,
        "epistemic_status": status.value,
    }


def _recall_item(
    *,
    text: str,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    mentioned_at: datetime,
    category: SemanticCategory = SemanticCategory.FACT,
    status: EpistemicStatus = EpistemicStatus.MENTIONED,
) -> object:
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        text=text,
        type="world",
        document_id=document_id_for(conversation_id),
        metadata=_metadata(
            conversation_id=conversation_id,
            message_id=message_id,
            timestamp=mentioned_at,
            category=category,
            status=status,
        ),
        tags=[],
        occurred_start=None,
        occurred_end=None,
        mentioned_at=mentioned_at.isoformat(),
    )


@pytest.mark.asyncio
async def test_recall_provenance_resolves_to_canonical_raw_message() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    timestamp = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
    async with session_factory() as session:
        conversation = Conversation(transport=TransportType.INTERNAL)
        session.add(conversation)
        await session.flush()
        message = Message(
            conversation_id=conversation.id,
            author=MessageAuthor.USER,
            text="The canonical synthetic source message.",
            sent_at=timestamp,
        )
        session.add(message)
        await session.commit()

    adapter = HindsightAdapter(
        HindsightConfig(base_url="http://memory.invalid", bank_id="elowyn-test"),
        client=RecallClient(
            [
                _recall_item(
                    text="Canonical synthetic fact",
                    conversation_id=conversation.id,
                    message_id=message.id,
                    mentioned_at=timestamp,
                )
            ]
        ),
    )
    recalled = await adapter.recall(RecallQuery(text="canonical source"))
    provenance = recalled.memories[0].provenance
    assert provenance is not None
    assert provenance.source_ref == f"elowyn:message:{message.id}"
    assert provenance.document_id == document_id_for(conversation.id)

    async with session_factory() as session:
        raw_message = await MemoryProvenanceService(session).resolve_message(provenance)
        assert raw_message.id == message.id
        assert raw_message.text == "The canonical synthetic source message."
    await engine.dispose()


@pytest.mark.asyncio
async def test_newer_conflicting_memory_is_first_and_old_history_is_preserved() -> None:
    conversation_id = uuid.uuid4()
    old_id = uuid.uuid4()
    new_id = uuid.uuid4()
    old_time = datetime(2026, 7, 1, tzinfo=UTC)
    new_time = datetime(2026, 8, 1, tzinfo=UTC)
    client = RecallClient(
        [
            _recall_item(
                text="The user now prefers coffee.",
                conversation_id=conversation_id,
                message_id=new_id,
                mentioned_at=new_time,
                category=SemanticCategory.PREFERENCE,
                status=EpistemicStatus.PREFERRED,
            ),
            _recall_item(
                text="The user prefers tea.",
                conversation_id=conversation_id,
                message_id=old_id,
                mentioned_at=old_time,
                category=SemanticCategory.PREFERENCE,
                status=EpistemicStatus.PREFERRED,
            ),
        ]
    )
    adapter = HindsightAdapter(
        HindsightConfig(base_url="http://memory.invalid", bank_id="elowyn-test"),
        client=client,
    )

    recalled = await adapter.recall(RecallQuery(text="drink preference"))

    assert [memory.source.message_id for memory in recalled.memories if memory.source] == [
        new_id,
        old_id,
    ]
    assert recalled.memories[0].temporal.mentioned_at > recalled.memories[1].temporal.mentioned_at
    assert all(memory.authoritative is False for memory in recalled.memories)


@pytest.mark.asyncio
async def test_raw_recall_with_mismatched_document_provenance_fails_closed() -> None:
    conversation_id = uuid.uuid4()
    item = _recall_item(
        text="Synthetic fact",
        conversation_id=conversation_id,
        message_id=uuid.uuid4(),
        mentioned_at=datetime(2026, 8, 22, tzinfo=UTC),
    )
    item.document_id = document_id_for(uuid.uuid4())
    adapter = HindsightAdapter(
        HindsightConfig(base_url="http://memory.invalid", bank_id="elowyn-test"),
        client=RecallClient([item]),
    )

    with pytest.raises(MemoryBackendError, match="provenance"):
        await adapter.recall(RecallQuery(text="synthetic"))
