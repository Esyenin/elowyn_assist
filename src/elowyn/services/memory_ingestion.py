from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import (
    Conversation,
    MemoryIngestionReceipt,
    MemoryIngestionState,
    Message,
)
from elowyn.domain.enums import MemoryIngestionOutcome, MemoryIngestionStatus


@dataclass(frozen=True)
class CatchUpBatch:
    state_id: uuid.UUID
    conversation_id: uuid.UUID
    backend: str
    attempt: int
    messages: tuple[Message, ...]

    @property
    def through_message_id(self) -> uuid.UUID:
        return self.messages[-1].id


@dataclass(frozen=True)
class DerivedRefreshClaim:
    state_id: uuid.UUID
    conversation_id: uuid.UUID
    through_message_id: uuid.UUID
    attempt: int


class MemoryIngestionStateService:
    """Lease batches from the canonical archive and advance only after backend success."""

    def __init__(self, session: AsyncSession, *, backend: str):
        backend = backend.strip()
        if not backend:
            raise ValueError("memory backend must not be blank")
        self.session = session
        self.backend = backend

    async def claim_next_batch(
        self,
        *,
        limit: int = 100,
        lease_for: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> CatchUpBatch | None:
        if limit < 1:
            raise ValueError("batch limit must be positive")
        if lease_for <= timedelta(0):
            raise ValueError("lease duration must be positive")
        current_time = now or datetime.now(UTC)

        conversation_ids = (
            (
                await self.session.execute(
                    select(Conversation.id)
                    .where(
                        select(Message.id)
                        .where(Message.conversation_id == Conversation.id)
                        .exists()
                    )
                    .order_by(Conversation.created_at, Conversation.id)
                )
            )
            .scalars()
            .all()
        )
        for conversation_id in conversation_ids:
            state = (
                await self.session.execute(
                    select(MemoryIngestionState)
                    .where(
                        MemoryIngestionState.conversation_id == conversation_id,
                        MemoryIngestionState.backend == self.backend,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if state is None:
                state = MemoryIngestionState(
                    conversation_id=conversation_id,
                    backend=self.backend,
                )
                self.session.add(state)
                await self.session.flush()
            elif state.status == MemoryIngestionStatus.PROCESSING and (
                state.lease_expires_at is not None
                and _as_utc(state.lease_expires_at) > _as_utc(current_time)
            ):
                continue
            elif state.next_attempt_at is not None and _as_utc(
                state.next_attempt_at
            ) > _as_utc(current_time):
                continue

            already_succeeded = (
                select(MemoryIngestionReceipt.message_id)
                .where(
                    MemoryIngestionReceipt.state_id == state.id,
                    MemoryIngestionReceipt.message_id == Message.id,
                )
                .exists()
            )
            statement = select(Message).where(
                Message.conversation_id == conversation_id,
                ~already_succeeded,
            )
            messages = (
                (
                    await self.session.execute(
                        statement.order_by(Message.created_at, Message.id).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if not messages:
                state.status = MemoryIngestionStatus.IDLE
                state.attempts = 0
                state.next_attempt_at = None
                state.lease_expires_at = None
                state.last_error = None
                continue

            state.status = MemoryIngestionStatus.PROCESSING
            state.attempts += 1
            state.lease_expires_at = current_time + lease_for
            state.next_attempt_at = None
            state.last_error = None
            await self.session.flush()
            return CatchUpBatch(
                state_id=state.id,
                conversation_id=conversation_id,
                backend=self.backend,
                attempt=state.attempts,
                messages=tuple(messages),
            )
        return None

    async def mark_succeeded(
        self, *, state_id: uuid.UUID, message_ids: tuple[uuid.UUID, ...]
    ) -> None:
        if not message_ids:
            raise ValueError("successful ingestion must contain at least one message")
        state = await self._locked_state(state_id)
        messages = await self._record_receipts(state, message_ids)
        state.last_succeeded_message_id = messages[-1].id
        state.status = MemoryIngestionStatus.IDLE
        state.attempts = 0
        state.next_attempt_at = None
        state.lease_expires_at = None
        state.last_error = None
        await self.session.flush()

    async def record_message_succeeded(
        self, *, state_id: uuid.UUID, message_id: uuid.UUID
    ) -> None:
        """Persist one backend-confirmed receipt while retaining the batch lease."""
        state = await self._locked_state(state_id)
        messages = await self._record_receipts(
            state, (message_id,), outcome=MemoryIngestionOutcome.INGESTED
        )
        state.last_succeeded_message_id = messages[-1].id
        state.derived_dirty_through_message_id = messages[-1].id
        state.derived_next_attempt_at = None
        await self.session.flush()

    async def record_message_ignored(
        self, *, state_id: uuid.UUID, message_id: uuid.UUID
    ) -> None:
        """Advance the durable ledger for content that cannot produce semantic memory."""
        state = await self._locked_state(state_id)
        messages = await self._record_receipts(
            state, (message_id,), outcome=MemoryIngestionOutcome.IGNORED_BLANK
        )
        state.last_succeeded_message_id = messages[-1].id
        await self.session.flush()

    async def claim_derived_refresh(
        self, *, now: datetime | None = None
    ) -> DerivedRefreshClaim | None:
        current_time = now or datetime.now(UTC)
        state = (
            await self.session.execute(
                select(MemoryIngestionState)
                .where(
                    MemoryIngestionState.backend == self.backend,
                    MemoryIngestionState.derived_dirty_through_message_id.is_not(None),
                    (
                        MemoryIngestionState.derived_next_attempt_at.is_(None)
                        | (MemoryIngestionState.derived_next_attempt_at <= current_time)
                    ),
                )
                .order_by(MemoryIngestionState.updated_at, MemoryIngestionState.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if state is None or state.derived_dirty_through_message_id is None:
            return None
        state.derived_attempts += 1
        await self.session.flush()
        return DerivedRefreshClaim(
            state_id=state.id,
            conversation_id=state.conversation_id,
            through_message_id=state.derived_dirty_through_message_id,
            attempt=state.derived_attempts,
        )

    async def mark_derived_succeeded(
        self, *, state_id: uuid.UUID, through_message_id: uuid.UUID
    ) -> None:
        state = await self._locked_state(state_id)
        if state.derived_dirty_through_message_id == through_message_id:
            state.derived_dirty_through_message_id = None
        state.derived_attempts = 0
        state.derived_next_attempt_at = None
        state.derived_last_error = None
        await self.session.flush()

    async def mark_derived_failed(
        self, *, state_id: uuid.UUID, error: str, retry_at: datetime
    ) -> None:
        state = await self._locked_state(state_id)
        state.derived_next_attempt_at = retry_at
        state.derived_last_error = error.strip()[:2000] or "memory derived refresh failed"
        await self.session.flush()

    async def renew_lease(
        self,
        *,
        state_id: uuid.UUID,
        lease_for: timedelta,
        now: datetime | None = None,
    ) -> None:
        if lease_for <= timedelta(0):
            raise ValueError("lease duration must be positive")
        state = await self._locked_state(state_id)
        if state.status != MemoryIngestionStatus.PROCESSING:
            raise ValueError("only a processing ingestion batch can renew its lease")
        state.lease_expires_at = (now or datetime.now(UTC)) + lease_for
        await self.session.flush()

    async def mark_failed(
        self,
        *,
        state_id: uuid.UUID,
        error: str,
        retry_at: datetime,
    ) -> None:
        state = await self._locked_state(state_id)
        state.status = MemoryIngestionStatus.FAILED
        state.next_attempt_at = retry_at
        state.lease_expires_at = None
        state.last_error = error.strip()[:2000] or "memory ingestion failed"
        await self.session.flush()

    async def _locked_state(self, state_id: uuid.UUID) -> MemoryIngestionState:
        state = (
            await self.session.execute(
                select(MemoryIngestionState)
                .where(
                    MemoryIngestionState.id == state_id,
                    MemoryIngestionState.backend == self.backend,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state is None:
            raise ValueError("memory ingestion state was not found for backend")
        return state

    async def _record_receipts(
        self,
        state: MemoryIngestionState,
        message_ids: tuple[uuid.UUID, ...],
        *,
        outcome: MemoryIngestionOutcome | None = None,
    ) -> list[Message]:
        messages: list[Message] = []
        for message_id in dict.fromkeys(message_ids):
            message = await self.session.get(Message, message_id)
            if message is None or message.conversation_id != state.conversation_id:
                raise ValueError("ingested messages must belong to the ingestion conversation")
            messages.append(message)
            receipt = await self.session.get(MemoryIngestionReceipt, (state.id, message.id))
            if receipt is None:
                self.session.add(
                    MemoryIngestionReceipt(
                        state_id=state.id,
                        message_id=message.id,
                        outcome=outcome or MemoryIngestionOutcome.INGESTED,
                    )
                )
            elif outcome is not None and receipt.outcome != outcome:
                raise ValueError("memory ingestion receipt outcome cannot change")
        return messages


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
