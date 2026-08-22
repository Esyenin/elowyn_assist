from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.base import Base
from elowyn.db.models import Conversation, MemoryIngestionState, Message
from elowyn.domain.enums import MessageAuthor, TransportType
from elowyn.services.conversation_summary import ConversationSummaryService
from elowyn.services.memory_ingestion import MemoryIngestionStateService


def test_memory_ingestion_service_rejects_blank_backend() -> None:
    with pytest.raises(ValueError, match="backend"):
        MemoryIngestionStateService(object(), backend="  ")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_memory_ingestion_service_rejects_invalid_claim_parameters() -> None:
    service = MemoryIngestionStateService(object(), backend="hindsight")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        await service.claim_next_batch(limit=0)
    with pytest.raises(ValueError, match="lease"):
        await service.claim_next_batch(
            lease_for=timedelta(0), now=datetime(2026, 8, 22, tzinfo=UTC)
        )


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_messages(session, *, count: int = 2):
    conversation = Conversation(transport=TransportType.INTERNAL)
    session.add(conversation)
    await session.flush()
    messages = []
    for index in range(count):
        message = Message(
            conversation_id=conversation.id,
            author=MessageAuthor.USER if index % 2 == 0 else MessageAuthor.ASSISTANT,
            text=f"synthetic message {index}",
            sent_at=datetime(2026, 8, 22, 10, index),
        )
        session.add(message)
        messages.append(message)
    await session.commit()
    return conversation, messages


async def test_catch_up_replays_after_expired_lease_and_advances_only_on_success(
    session_factory,
) -> None:
    now = datetime(2026, 8, 22, 12, 0)
    async with session_factory() as session:
        conversation, messages = await _seed_messages(session)
        service = MemoryIngestionStateService(session, backend="hindsight")
        first = await service.claim_next_batch(now=now, lease_for=timedelta(seconds=5))
        assert first is not None
        assert {item.id for item in first.messages} == {item.id for item in messages}
        await session.commit()

    # Simulated crash: no success was recorded. An expired lease makes the raw
    # archive batch claimable again after restart, with no state reconstruction hook.
    async with session_factory() as restarted:
        service = MemoryIngestionStateService(restarted, backend="hindsight")
        replay = await service.claim_next_batch(
            now=now + timedelta(seconds=6), lease_for=timedelta(seconds=5)
        )
        assert replay is not None
        assert {item.id for item in replay.messages} == {item.id for item in messages}
        await service.mark_succeeded(
            state_id=replay.state_id,
            message_ids=tuple(item.id for item in replay.messages),
        )
        await restarted.commit()

    async with session_factory() as session:
        new_message = Message(
            conversation_id=conversation.id,
            author=MessageAuthor.USER,
            text="message saved after the previous cursor",
            sent_at=datetime(2026, 8, 22, 12, 30),
        )
        session.add(new_message)
        await session.commit()
        service = MemoryIngestionStateService(session, backend="hindsight")
        catch_up = await service.claim_next_batch(now=now + timedelta(hours=1))
        assert catch_up is not None
        assert [item.id for item in catch_up.messages] == [new_message.id]


async def test_conversation_without_state_is_discovered_from_raw_archive(session_factory) -> None:
    async with session_factory() as session:
        conversation, messages = await _seed_messages(session, count=1)
        assert await session.get(MemoryIngestionState, uuid4()) is None

        batch = await MemoryIngestionStateService(session, backend="hindsight").claim_next_batch(
            now=datetime(2026, 8, 22, 12, 0)
        )

        assert batch is not None
        assert batch.conversation_id == conversation.id
        assert [item.id for item in batch.messages] == [messages[0].id]


async def test_failed_batch_waits_for_retry_and_preserves_cursor(session_factory) -> None:
    now = datetime(2026, 8, 22, 12, 0)
    async with session_factory() as session:
        _, messages = await _seed_messages(session, count=1)
        service = MemoryIngestionStateService(session, backend="hindsight")
        batch = await service.claim_next_batch(now=now)
        assert batch is not None
        await service.mark_failed(
            state_id=batch.state_id,
            error="synthetic backend outage",
            retry_at=now + timedelta(minutes=5),
        )
        await session.commit()

    async with session_factory() as session:
        service = MemoryIngestionStateService(session, backend="hindsight")
        assert await service.claim_next_batch(now=now + timedelta(minutes=4)) is None
        retry = await service.claim_next_batch(now=now + timedelta(minutes=6))
        assert retry is not None
        assert [item.id for item in retry.messages] == [messages[0].id]


async def test_conversation_summary_is_derived_and_cursor_validated(session_factory) -> None:
    async with session_factory() as session:
        conversation, messages = await _seed_messages(session, count=1)
        service = ConversationSummaryService(session)
        summary = await service.save(
            conversation_id=conversation.id,
            short_summary=" Synthetic conversation summary ",
            topics=["memory", "memory", ""],
            related_entity_ids=[uuid4()],
            last_processed_message_id=messages[0].id,
            derivation_version="summary-v1",
        )
        assert summary.short_summary == "Synthetic conversation summary"
        assert summary.topics == ["memory"]
        assert summary.last_processed_message_id == messages[0].id

        other_conversation, other_messages = await _seed_messages(session, count=1)
        with pytest.raises(ValueError, match="cursor"):
            await service.save(
                conversation_id=other_conversation.id,
                short_summary="Wrong cursor",
                topics=[],
                related_entity_ids=[],
                last_processed_message_id=messages[0].id,
                derivation_version="summary-v1",
            )
        assert other_messages[0].conversation_id == other_conversation.id
