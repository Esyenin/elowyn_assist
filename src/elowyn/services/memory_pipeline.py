from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from elowyn.memory.service import MemoryService, MemorySource, RetainMessage
from elowyn.services.memory_ingestion import CatchUpBatch, MemoryIngestionStateService

logger = logging.getLogger(__name__)

_INGESTION_OPERATION_NAMESPACE = uuid.UUID("0638258d-a320-42d9-940e-805f37bb2dc9")


@dataclass(frozen=True)
class MemoryPipelineConfig:
    backend: str
    batch_size: int = 25
    lease_for: timedelta = timedelta(minutes=15)
    retry_base: timedelta = timedelta(seconds=5)
    retry_max: timedelta = timedelta(minutes=5)
    idle_poll_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.backend.strip():
            raise ValueError("memory pipeline backend must not be blank")
        if self.batch_size < 1:
            raise ValueError("memory pipeline batch size must be positive")
        if self.lease_for <= timedelta(0):
            raise ValueError("memory pipeline lease must be positive")
        if self.retry_base <= timedelta(0) or self.retry_max < self.retry_base:
            raise ValueError("memory pipeline retry bounds are invalid")
        if self.idle_poll_seconds <= 0:
            raise ValueError("memory pipeline poll interval must be positive")


class MemoryIngestionProcessor:
    """Move bounded raw-message batches through MemoryService outside the turn path."""

    def __init__(self, session_factory, memory: MemoryService, config: MemoryPipelineConfig):
        self.session_factory = session_factory
        self.memory = memory
        self.config = config

    async def process_once(self, *, now: datetime | None = None) -> bool:
        current_time = now or datetime.now(UTC)
        batch = await self._claim(current_time)
        if batch is None:
            return False

        try:
            health = await self.memory.health()
            if not health.ready:
                await self._mark_failed(
                    batch,
                    current_time=current_time,
                    error=f"{health.backend} is not ready",
                )
                return True

            for message in batch.messages:
                await self._renew_lease(batch.state_id)
                retain_message = RetainMessage(
                    source=MemorySource(
                        conversation_id=message.conversation_id,
                        message_id=message.id,
                        role=message.author.value,
                        occurred_at=_aware(message.sent_at),
                    ),
                    text=message.text or "",
                )
                await self.memory.retain(
                    (retain_message,),
                    operation_id=ingestion_operation_id(
                        backend=batch.backend,
                        message_id=message.id,
                    ),
                )
                await self._record_receipt(batch.state_id, message.id)

            await self._finish(batch)
            return True
        except asyncio.CancelledError:
            # Leave the committed PROCESSING lease in place. Restart recovery
            # reclaims it after expiry and reuses the same operation identity.
            raise
        except Exception as exc:
            await self._mark_failed(
                batch,
                current_time=current_time,
                error=f"{type(exc).__name__}: memory ingestion failed",
            )
            return True

    async def _claim(self, now: datetime) -> CatchUpBatch | None:
        async with self.session_factory() as session:
            state = MemoryIngestionStateService(session, backend=self.config.backend)
            batch = await state.claim_next_batch(
                limit=self.config.batch_size,
                lease_for=self.config.lease_for,
                now=now,
            )
            await session.commit()
            return batch

    async def _renew_lease(self, state_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            state = MemoryIngestionStateService(session, backend=self.config.backend)
            await state.renew_lease(state_id=state_id, lease_for=self.config.lease_for)
            await session.commit()

    async def _record_receipt(self, state_id: uuid.UUID, message_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            state = MemoryIngestionStateService(session, backend=self.config.backend)
            await state.record_message_succeeded(state_id=state_id, message_id=message_id)
            await session.commit()

    async def _finish(self, batch: CatchUpBatch) -> None:
        async with self.session_factory() as session:
            state = MemoryIngestionStateService(session, backend=self.config.backend)
            await state.mark_succeeded(
                state_id=batch.state_id,
                message_ids=tuple(message.id for message in batch.messages),
            )
            await session.commit()

    async def _mark_failed(
        self,
        batch: CatchUpBatch,
        *,
        current_time: datetime,
        error: str,
    ) -> None:
        async with self.session_factory() as session:
            state = MemoryIngestionStateService(session, backend=self.config.backend)
            await state.mark_failed(
                state_id=batch.state_id,
                error=error,
                retry_at=current_time + retry_delay(self.config, batch.attempt),
            )
            await session.commit()


class MemoryIngestionWorker:
    """Lifecycle-owned wakeable catch-up loop; raw messages remain the durable queue."""

    def __init__(self, processor: MemoryIngestionProcessor):
        self.processor = processor
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                while not self._stop.is_set() and await self.processor.process_once():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Memory catch-up iteration failed; periodic retry will continue")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self.processor.config.idle_poll_seconds,
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def ingestion_operation_id(*, backend: str, message_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_INGESTION_OPERATION_NAMESPACE, f"{backend}:{message_id}")


def retry_delay(config: MemoryPipelineConfig, attempt: int) -> timedelta:
    maximum_multiplier = max(int(config.retry_max / config.retry_base), 1)
    exponent = min(max(attempt - 1, 0), maximum_multiplier.bit_length())
    multiplier = 2**exponent
    delay = config.retry_base * multiplier
    return min(delay, config.retry_max)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
