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
    MemoryIngestionReceipt,
    MemoryIngestionState,
    Message,
)
from elowyn.domain.enums import MemoryIngestionStatus, MessageAuthor, TransportType
from elowyn.memory.service import (
    MemoryHealth,
    RecallQuery,
    RecallResult,
    Reflection,
    ReflectQuery,
    RetainMessage,
    RetainResult,
)
from elowyn.services.memory_pipeline import (
    MemoryIngestionProcessor,
    MemoryIngestionWorker,
    MemoryPipelineConfig,
    ingestion_operation_id,
)


class RecordingMemory:
    def __init__(
        self,
        *,
        ready: bool = True,
        cancel_once: bool = False,
        fail_on_call: int | None = None,
    ) -> None:
        self.ready = ready
        self.cancel_once = cancel_once
        self.fail_on_call = fail_on_call
        self.calls: list[tuple[RetainMessage, uuid.UUID]] = []

    async def health(self) -> MemoryHealth:
        return MemoryHealth(backend="synthetic-memory", ready=self.ready)

    async def retain(
        self,
        messages: tuple[RetainMessage, ...],
        *,
        operation_id: uuid.UUID | None = None,
    ) -> RetainResult:
        assert len(messages) == 1
        assert operation_id is not None
        self.calls.append((messages[0], operation_id))
        if self.cancel_once:
            self.cancel_once = False
            raise asyncio.CancelledError
        if self.fail_on_call == len(self.calls):
            self.fail_on_call = None
            raise RuntimeError("synthetic retain failure")
        return RetainResult(operation_id=operation_id, accepted_items=1)

    async def recall(self, query: RecallQuery) -> RecallResult:
        raise AssertionError("recall is outside the ingestion pipeline")

    async def reflect(self, query: ReflectQuery) -> Reflection:
        raise AssertionError("reflect is outside the ingestion pipeline")

    async def close(self) -> None:
        return None


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_messages(session_factory, count: int) -> tuple[Conversation, list[Message]]:
    async with session_factory() as session:
        conversation = Conversation(transport=TransportType.INTERNAL)
        session.add(conversation)
        await session.flush()
        messages = []
        for index in range(count):
            message = Message(
                conversation_id=conversation.id,
                author=MessageAuthor.USER if index % 2 == 0 else MessageAuthor.ASSISTANT,
                text=f"synthetic canonical message {index}",
                sent_at=datetime(2026, 8, 22, 10, index, tzinfo=UTC),
            )
            session.add(message)
            messages.append(message)
        await session.commit()
        return conversation, messages


async def _state_snapshot(session_factory) -> tuple[MemoryIngestionState, int]:
    async with session_factory() as session:
        state = (await session.execute(select(MemoryIngestionState))).scalar_one()
        receipt_count = (
            await session.scalar(select(func.count()).select_from(MemoryIngestionReceipt))
        ) or 0
        return state, receipt_count


@pytest.mark.asyncio
async def test_bounded_batches_ingest_consecutive_raw_messages_and_align_receipts(
    session_factory,
) -> None:
    _, messages = await _seed_messages(session_factory, 3)
    memory = RecordingMemory()
    processor = MemoryIngestionProcessor(
        session_factory,
        memory,
        MemoryPipelineConfig(backend="synthetic-v1", batch_size=2),
    )

    assert await processor.process_once() is True
    state, receipt_count = await _state_snapshot(session_factory)
    assert receipt_count == 2
    assert state.last_succeeded_message_id == memory.calls[1][0].source.message_id
    assert state.status == MemoryIngestionStatus.IDLE

    assert await processor.process_once() is True
    assert await processor.process_once() is False
    state, receipt_count = await _state_snapshot(session_factory)
    assert receipt_count == 3
    assert state.last_succeeded_message_id == memory.calls[-1][0].source.message_id
    assert {call[0].source.message_id for call in memory.calls} == {
        message.id for message in messages
    }
    assert len(memory.calls) == 3


@pytest.mark.asyncio
async def test_backend_outage_preserves_cursor_then_recovery_catches_up(session_factory) -> None:
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    await _seed_messages(session_factory, 2)
    memory = RecordingMemory(ready=False)
    config = MemoryPipelineConfig(
        backend="synthetic-v1",
        retry_base=timedelta(seconds=5),
        retry_max=timedelta(seconds=20),
    )
    processor = MemoryIngestionProcessor(session_factory, memory, config)

    assert await processor.process_once(now=now) is True
    state, receipt_count = await _state_snapshot(session_factory)
    assert state.status == MemoryIngestionStatus.FAILED
    assert state.last_succeeded_message_id is None
    assert state.next_attempt_at.replace(tzinfo=UTC) == now + timedelta(seconds=5)
    assert receipt_count == 0
    assert memory.calls == []
    assert await processor.process_once(now=now + timedelta(seconds=4)) is False

    memory.ready = True
    assert await processor.process_once(now=now + timedelta(seconds=6)) is True
    state, receipt_count = await _state_snapshot(session_factory)
    assert state.status == MemoryIngestionStatus.IDLE
    assert state.last_succeeded_message_id == memory.calls[-1][0].source.message_id
    assert receipt_count == 2


@pytest.mark.asyncio
async def test_crash_restart_replays_safely_with_same_message_operation_id(
    session_factory,
) -> None:
    started = datetime.now(UTC)
    conversation, messages = await _seed_messages(session_factory, 1)
    memory = RecordingMemory(cancel_once=True)
    config = MemoryPipelineConfig(
        backend="synthetic-v1",
        batch_size=10,
        lease_for=timedelta(seconds=1),
    )
    crashed = MemoryIngestionProcessor(session_factory, memory, config)

    with pytest.raises(asyncio.CancelledError):
        await crashed.process_once(now=started)
    state, receipt_count = await _state_snapshot(session_factory)
    assert state.status == MemoryIngestionStatus.PROCESSING
    assert receipt_count == 0

    async with session_factory() as session:
        next_message = Message(
            conversation_id=conversation.id,
            author=MessageAuthor.ASSISTANT,
            text="synthetic message saved while worker was down",
            sent_at=started + timedelta(seconds=1),
        )
        session.add(next_message)
        await session.commit()

    restarted = MemoryIngestionProcessor(session_factory, memory, config)
    assert await restarted.process_once(now=started + timedelta(seconds=2)) is True
    state, receipt_count = await _state_snapshot(session_factory)
    assert state.status == MemoryIngestionStatus.IDLE
    assert receipt_count == 2
    replayed_calls = [
        call for call in memory.calls if call[0].source.message_id == messages[0].id
    ]
    assert len(replayed_calls) == 2
    assert replayed_calls[0][1] == replayed_calls[1][1]
    assert replayed_calls[0][1] == ingestion_operation_id(
        backend="synthetic-v1", message_id=messages[0].id
    )
    assert any(call[0].source.message_id == next_message.id for call in memory.calls)


@pytest.mark.asyncio
async def test_existing_receipt_prevents_duplicate_replay(session_factory) -> None:
    _, messages = await _seed_messages(session_factory, 1)
    memory = RecordingMemory()
    processor = MemoryIngestionProcessor(
        session_factory,
        memory,
        MemoryPipelineConfig(backend="synthetic-v1"),
    )

    assert await processor.process_once() is True
    assert await processor.process_once() is False
    assert len(memory.calls) == 1
    state, receipt_count = await _state_snapshot(session_factory)
    assert receipt_count == 1
    assert state.last_succeeded_message_id == messages[0].id


@pytest.mark.asyncio
async def test_partial_batch_failure_retries_only_unreceipted_messages(session_factory) -> None:
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    await _seed_messages(session_factory, 3)
    memory = RecordingMemory(fail_on_call=2)
    config = MemoryPipelineConfig(
        backend="synthetic-v1",
        retry_base=timedelta(seconds=1),
        retry_max=timedelta(seconds=5),
    )
    processor = MemoryIngestionProcessor(session_factory, memory, config)

    assert await processor.process_once(now=now) is True
    state, receipt_count = await _state_snapshot(session_factory)
    assert state.status == MemoryIngestionStatus.FAILED
    assert receipt_count == 1
    confirmed_message_id = memory.calls[0][0].source.message_id

    assert await processor.process_once(now=now + timedelta(seconds=2)) is True
    state, receipt_count = await _state_snapshot(session_factory)
    assert state.status == MemoryIngestionStatus.IDLE
    assert receipt_count == 3
    assert sum(
        call[0].source.message_id == confirmed_message_id for call in memory.calls
    ) == 1
    retried_message_id = memory.calls[1][0].source.message_id
    retried_operations = [
        call[1] for call in memory.calls if call[0].source.message_id == retried_message_id
    ]
    assert len(retried_operations) == 2
    assert retried_operations[0] == retried_operations[1]


@pytest.mark.asyncio
async def test_post_commit_wakeup_runs_ingestion_outside_the_turn(session_factory) -> None:
    memory = RecordingMemory()
    processor = MemoryIngestionProcessor(
        session_factory,
        memory,
        MemoryPipelineConfig(
            backend="synthetic-v1",
            idle_poll_seconds=60,
        ),
    )
    worker = MemoryIngestionWorker(processor)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0)

    _, messages = await _seed_messages(session_factory, 1)
    worker.wake()
    for _ in range(100):
        if memory.calls:
            break
        await asyncio.sleep(0.01)

    worker.stop()
    await asyncio.wait_for(task, timeout=1)
    assert [call[0].source.message_id for call in memory.calls] == [messages[0].id]
    _, receipt_count = await _state_snapshot(session_factory)
    assert receipt_count == 1
