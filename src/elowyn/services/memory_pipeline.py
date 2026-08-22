from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from elowyn.db.models import Message
from elowyn.domain.enums import MemoryPageType, MessageAuthor, SemanticCategory
from elowyn.memory.observations import ObservationCandidate, ObservationEvidence
from elowyn.memory.semantics import classify_semantics
from elowyn.memory.service import (
    MemoryProvenance,
    MemoryService,
    MemorySource,
    RetainMessage,
)
from elowyn.services.conversation_summary import ConversationSummaryService
from elowyn.services.memory_consolidation import (
    MemoryPageService,
    ObservationConsolidationService,
)
from elowyn.services.memory_ingestion import CatchUpBatch, MemoryIngestionStateService

logger = logging.getLogger(__name__)

_INGESTION_OPERATION_NAMESPACE = uuid.UUID("0638258d-a320-42d9-940e-805f37bb2dc9")
_SUMMARY_VERSION = "elowyn-archive-summary-v1"
_COMMUNICATION_TERMS = {
    "answer",
    "brief",
    "concise",
    "communication",
    "reply",
    "response",
    "кратк",
    "общен",
    "ответ",
}
_TOPIC_STOP_WORDS = {
    "about",
    "and",
    "для",
    "или",
    "мне",
    "the",
    "this",
    "user",
    "что",
    "это",
}


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
            try:
                await self._refresh_derived(batch)
            except Exception:
                # Atomic retention/cursor success remains valid. Derived summaries,
                # observations, and pages are disposable and full rebuild remains
                # the recovery path if an opportunistic refresh fails.
                logger.warning("Memory derived refresh failed; raw archive remains rebuildable")
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

    async def _refresh_derived(self, batch: CatchUpBatch) -> None:
        conversation_ids = {message.conversation_id for message in batch.messages}
        user_texts = {
            " ".join((message.text or "").split())
            for message in batch.messages
            if message.author == MessageAuthor.USER and (message.text or "").strip()
        }
        async with self.session_factory() as session:
            for conversation_id in conversation_ids:
                messages = list(
                    (
                        await session.execute(
                            select(Message)
                            .where(
                                Message.conversation_id == conversation_id,
                                Message.text.is_not(None),
                                func.length(func.trim(Message.text)) > 0,
                            )
                            .order_by(Message.sent_at, Message.created_at, Message.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not messages:
                    continue
                await ConversationSummaryService(session).save(
                    conversation_id=conversation_id,
                    short_summary=_clip(
                        " | ".join(
                            f"{message.author.value}: {message.text}" for message in messages
                        ),
                        600,
                    ),
                    topics=list(
                        dict.fromkeys(
                            classify_semantics(message.text or "").category.value.casefold()
                            for message in messages
                        )
                    ),
                    related_entity_ids=[],
                    last_processed_message_id=messages[-1].id,
                    derivation_version=_SUMMARY_VERSION,
                )

            pages: set[tuple[MemoryPageType, str, str]] = set()
            consolidation = ObservationConsolidationService(session)
            for text in sorted(user_texts):
                evidence_messages = list(
                    (
                        await session.execute(
                            select(Message)
                            .where(
                                Message.author == MessageAuthor.USER,
                                Message.text == text,
                            )
                            .order_by(Message.sent_at, Message.created_at, Message.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not evidence_messages:
                    continue
                semantics = classify_semantics(text)
                page_type, scope_key, title = _page_for(semantics.category, text)
                claim_hash = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:24]
                await consolidation.consolidate(
                    ObservationCandidate(
                        claim_key=f"archive:{claim_hash}",
                        statement=_clip(text, 400),
                        category=semantics.category,
                        evidence=tuple(
                            ObservationEvidence(
                                backend_memory_id=(
                                    "operation:"
                                    + str(
                                        ingestion_operation_id(
                                            backend=batch.backend,
                                            message_id=message.id,
                                        )
                                    )
                                ),
                                provenance=MemoryProvenance(
                                    conversation_id=message.conversation_id,
                                    message_id=message.id,
                                    role=message.author.value,
                                    occurred_at=_aware(message.sent_at),
                                ),
                            )
                            for message in evidence_messages
                        ),
                        page_type=page_type,
                        page_scope_key=scope_key,
                        page_title=title,
                    )
                )
                pages.add((page_type, scope_key, title))
            for page_type, scope_key, title in pages:
                await MemoryPageService(session).refresh(
                    page_type=page_type,
                    scope_key=scope_key,
                    title=title,
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


def _page_for(category: SemanticCategory, text: str) -> tuple[MemoryPageType, str, str]:
    lowered = text.casefold()
    if category == SemanticCategory.PREFERENCE:
        if any(term in lowered for term in _COMMUNICATION_TERMS):
            return (
                MemoryPageType.COMMUNICATION_PREFERENCES,
                "user",
                "Communication Preferences",
            )
        return MemoryPageType.USER_PROFILE, "user", "User Profile"
    topic = next(
        (
            token
            for token in _words(lowered)
            if len(token) >= 3 and token not in _TOPIC_STOP_WORDS
        ),
        category.value.casefold(),
    )
    return MemoryPageType.TOPIC, topic, f"Topic: {topic}"


def _words(value: str) -> list[str]:
    return [
        "".join(character for character in token if character.isalnum())
        for token in value.split()
    ]


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
