from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, distinct, func, select

from elowyn.db.models import (
    Conversation,
    ConversationSummary,
    MemoryBackendRegistry,
    MemoryGeneration,
    MemoryIngestionReceipt,
    MemoryIngestionState,
    Message,
)
from elowyn.domain.enums import (
    MemoryGenerationStatus,
    MemoryIngestionStatus,
    MemoryPageType,
    MessageAuthor,
    SemanticCategory,
)
from elowyn.memory.generation import MemoryBackendFactory
from elowyn.memory.observations import ObservationCandidate, ObservationEvidence
from elowyn.memory.rebuild import MemoryDiagnostics, MemoryRebuildError, MemoryRebuildResult
from elowyn.memory.semantics import classify_semantics
from elowyn.memory.service import MemoryProvenance, MemoryService, RecallQuery, RetainMessage
from elowyn.services.conversation_summary import ConversationSummaryService
from elowyn.services.memory_consolidation import MemoryDerivedRebuilder
from elowyn.services.memory_pipeline import ingestion_operation_id

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
class MemoryRebuildConfig:
    backend: str
    bank_prefix: str
    batch_size: int = 25
    lease_for: timedelta = timedelta(minutes=30)
    verification_attempts: int = 3
    verification_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not self.backend.strip() or not self.bank_prefix.strip():
            raise ValueError("rebuild backend and bank prefix must not be blank")
        if len(self.backend) > 100 or len(self.bank_prefix) > 200:
            raise ValueError("rebuild backend or bank prefix is too long")
        if self.batch_size < 1 or self.lease_for <= timedelta(0):
            raise ValueError("rebuild batch and lease must be positive")
        if self.verification_attempts < 1 or self.verification_delay_seconds < 0:
            raise ValueError("rebuild verification configuration is invalid")


class MemoryGenerationManager:
    """Explicit clean-bank rebuild with an atomic active-generation switch."""

    def __init__(self, session_factory, factory: MemoryBackendFactory, config: MemoryRebuildConfig):
        self.session_factory = session_factory
        self.factory = factory
        self.config = config

    async def bootstrap_existing(self, bank_id: str) -> uuid.UUID:
        """Register the configured pre-generation bank without creating or deleting it."""
        bank_id = bank_id.strip()
        if not bank_id:
            raise ValueError("bootstrap bank ID must not be blank")
        async with self.session_factory() as session:
            registry = (
                await session.execute(
                    select(MemoryBackendRegistry)
                    .where(MemoryBackendRegistry.backend == self.config.backend)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if registry is not None and registry.active_generation_id is not None:
                return registry.active_generation_id
            if registry is None:
                registry = MemoryBackendRegistry(backend=self.config.backend)
                session.add(registry)
                await session.flush()
            generation = (
                await session.execute(
                    select(MemoryGeneration).where(MemoryGeneration.bank_id == bank_id)
                )
            ).scalar_one_or_none()
            if generation is None:
                generation = MemoryGeneration(
                    backend=self.config.backend,
                    bank_id=bank_id,
                    status=MemoryGenerationStatus.ACTIVE,
                    messages_total=0,
                    messages_replayed=0,
                    messages_verified=0,
                )
                session.add(generation)
                await session.flush()
            elif generation.backend != self.config.backend:
                raise ValueError("bootstrap bank belongs to another backend")
            generation.status = MemoryGenerationStatus.ACTIVE
            generation.lease_expires_at = None
            registry.active_generation_id = generation.id
            await session.commit()
            return generation.id

    async def recover_interrupted(self, *, now: datetime | None = None) -> int:
        current_time = now or datetime.now(UTC)
        async with self.session_factory() as session:
            generations = (
                (
                    await session.execute(
                        select(MemoryGeneration)
                        .where(
                            MemoryGeneration.backend == self.config.backend,
                            MemoryGeneration.status == MemoryGenerationStatus.BUILDING,
                            MemoryGeneration.lease_expires_at <= current_time,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for generation in generations:
                generation.status = MemoryGenerationStatus.FAILED
                generation.lease_expires_at = None
                generation.last_error = "rebuild interrupted before atomic switch"
                generation.completed_at = current_time
            await session.commit()
            return len(generations)

    async def rebuild(self, *, explicit: bool = False) -> MemoryRebuildResult:
        if not explicit:
            raise PermissionError("memory rebuild requires explicit opt-in")
        generation = await self._start()
        memory: MemoryService | None = None
        try:
            memory = await self.factory.create_clean(generation.bank_id)
            health = await memory.health()
            if not health.ready:
                raise MemoryRebuildError("clean memory generation is not ready")
            await self._replay(generation.id, memory)
            return await self._verify_derive_and_switch(generation.id, memory)
        except asyncio.CancelledError:
            # Keep BUILDING+lease. A restarted maintainer marks it failed after expiry;
            # the previous registry pointer remains untouched throughout.
            raise
        except Exception as exc:
            await self._mark_failed(generation.id, exc)
            if isinstance(exc, MemoryRebuildError):
                raise
            raise MemoryRebuildError(f"{type(exc).__name__}: memory rebuild failed") from exc
        finally:
            if memory is not None:
                await memory.close()

    async def _start(self) -> MemoryGeneration:
        now = datetime.now(UTC)
        await self.recover_interrupted(now=now)
        async with self.session_factory() as session:
            registry = (
                await session.execute(
                    select(MemoryBackendRegistry)
                    .where(MemoryBackendRegistry.backend == self.config.backend)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if registry is None or registry.active_generation_id is None:
                raise MemoryRebuildError("active generation must be bootstrapped before rebuild")
            running = await session.scalar(
                select(func.count())
                .select_from(MemoryGeneration)
                .where(
                    MemoryGeneration.backend == self.config.backend,
                    MemoryGeneration.status == MemoryGenerationStatus.BUILDING,
                )
            )
            if running:
                raise MemoryRebuildError("another memory rebuild is already running")
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.text.is_not(None), func.length(func.trim(Message.text)) > 0)
                )
                or 0
            )
            bank_id = f"{self.config.bank_prefix}-g-{uuid.uuid4().hex}"
            generation = MemoryGeneration(
                backend=self.config.backend,
                bank_id=bank_id,
                status=MemoryGenerationStatus.BUILDING,
                messages_total=total,
                messages_replayed=0,
                messages_verified=0,
                lease_expires_at=now + self.config.lease_for,
            )
            session.add(generation)
            await session.commit()
            return generation

    async def _replay(self, generation_id: uuid.UUID, memory: MemoryService) -> None:
        offset = 0
        while True:
            messages = await self._message_batch(offset)
            if not messages:
                return
            for message in messages:
                await self._retain_message(memory, message)
            offset += len(messages)
            await self._record_progress(generation_id, offset)

    async def _verify_derive_and_switch(
        self, generation_id: uuid.UUID, memory: MemoryService
    ) -> MemoryRebuildResult:
        async with self.session_factory() as session:
            registry = (
                await session.execute(
                    select(MemoryBackendRegistry)
                    .where(MemoryBackendRegistry.backend == self.config.backend)
                    .with_for_update()
                )
            ).scalar_one()
            generation = await session.get(MemoryGeneration, generation_id)
            if generation is None or generation.status != MemoryGenerationStatus.BUILDING:
                raise MemoryRebuildError("rebuild generation is no longer switchable")

            # The registry lock also blocks active-proxy retain. Catch up raw rows
            # committed after the initial replay before changing the pointer.
            while True:
                messages = await self._message_batch(generation.messages_replayed, session=session)
                if not messages:
                    break
                generation.messages_total += len(messages)
                for message in messages:
                    await self._retain_message(memory, message)
                generation.messages_replayed += len(messages)
                generation.lease_expires_at = datetime.now(UTC) + self.config.lease_for
                await session.flush()

            generation.messages_verified = await self._verify_index(memory, session)
            await self._rebuild_derived(session)
            previous_id = registry.active_generation_id
            if previous_id is not None and previous_id != generation.id:
                previous = await session.get(MemoryGeneration, previous_id)
                if previous is not None:
                    previous.status = MemoryGenerationStatus.SUPERSEDED
                    previous.lease_expires_at = None
            generation.status = MemoryGenerationStatus.ACTIVE
            generation.lease_expires_at = None
            generation.completed_at = datetime.now(UTC)
            generation.last_error = None
            registry.active_generation_id = generation.id
            await session.commit()
            return MemoryRebuildResult(
                generation_id=generation.id,
                bank_id=generation.bank_id,
                messages_replayed=generation.messages_replayed,
                messages_verified=generation.messages_verified,
            )

    async def _message_batch(self, offset: int, *, session=None) -> list[Message]:
        if session is not None:
            return await self._query_message_batch(session, offset)
        async with self.session_factory() as owned_session:
            return await self._query_message_batch(owned_session, offset)

    async def _query_message_batch(self, session, offset: int) -> list[Message]:
        return list(
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.text.is_not(None),
                        func.length(func.trim(Message.text)) > 0,
                    )
                    .order_by(Message.created_at, Message.id)
                    .offset(offset)
                    .limit(self.config.batch_size)
                )
            )
            .scalars()
            .all()
        )

    async def _retain_message(self, memory: MemoryService, message: Message) -> None:
        semantics = classify_semantics(message.text or "")
        source = MemoryProvenance(
            conversation_id=message.conversation_id,
            message_id=message.id,
            role=message.author.value,
            occurred_at=_aware(message.sent_at),
        )
        await memory.retain(
            (
                RetainMessage(
                    source=source,
                    text=message.text or "",
                    topic_tags=(semantics.category.value.lower(),),
                    semantics=semantics,
                ),
            ),
            operation_id=ingestion_operation_id(
                backend=self.config.backend,
                message_id=message.id,
            ),
        )

    async def _record_progress(self, generation_id: uuid.UUID, replayed: int) -> None:
        async with self.session_factory() as session:
            generation = await session.get(MemoryGeneration, generation_id)
            if generation is None or generation.status != MemoryGenerationStatus.BUILDING:
                raise MemoryRebuildError("rebuild generation disappeared during replay")
            generation.messages_replayed = replayed
            generation.messages_total = max(generation.messages_total, replayed)
            generation.lease_expires_at = datetime.now(UTC) + self.config.lease_for
            await session.commit()

    async def _verify_index(self, memory: MemoryService, session) -> int:
        samples: list[Message] = []
        for descending in (False, True):
            order = Message.created_at.desc() if descending else Message.created_at
            message = (
                await session.execute(
                    select(Message)
                    .where(Message.text.is_not(None), func.length(func.trim(Message.text)) > 0)
                    .order_by(order, Message.id.desc() if descending else Message.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if message is not None and all(item.id != message.id for item in samples):
                samples.append(message)
        for attempt in range(self.config.verification_attempts):
            verified = 0
            for message in samples:
                result = await memory.recall(RecallQuery(text=message.text or "", max_tokens=256))
                if any(
                    item.provenance is not None
                    and item.provenance.message_id == message.id
                    for item in result.memories
                ):
                    verified += 1
            if verified == len(samples):
                return verified
            if attempt + 1 < self.config.verification_attempts:
                await asyncio.sleep(self.config.verification_delay_seconds)
        if samples:
            raise MemoryRebuildError("rebuild retain succeeded but index verification failed")
        return 0

    async def _rebuild_derived(self, session) -> None:
        await session.execute(delete(ConversationSummary))
        conversations = (
            (await session.execute(select(Conversation).order_by(Conversation.created_at)))
            .scalars()
            .all()
        )
        candidates: list[ObservationCandidate] = []
        for conversation in conversations:
            messages = (
                (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.conversation_id == conversation.id,
                            Message.text.is_not(None),
                            func.length(func.trim(Message.text)) > 0,
                        )
                        .order_by(Message.created_at, Message.id)
                    )
                )
                .scalars()
                .all()
            )
            if not messages:
                continue
            topics = list(
                dict.fromkeys(
                    classify_semantics(message.text or "").category.value
                    for message in messages
                )
            )
            summary_text = _clip(
                " | ".join(f"{message.author.value}: {message.text}" for message in messages),
                600,
            )
            await ConversationSummaryService(session).save(
                conversation_id=conversation.id,
                short_summary=summary_text,
                topics=[topic.lower() for topic in topics],
                related_entity_ids=[],
                last_processed_message_id=messages[-1].id,
                derivation_version=_SUMMARY_VERSION,
            )
            candidates.extend(_archive_candidates(messages))
        await MemoryDerivedRebuilder(session).rebuild(tuple(candidates))

    async def _mark_failed(self, generation_id: uuid.UUID, exc: Exception) -> None:
        async with self.session_factory() as session:
            generation = await session.get(MemoryGeneration, generation_id)
            if generation is not None and generation.status == MemoryGenerationStatus.BUILDING:
                generation.status = MemoryGenerationStatus.FAILED
                generation.lease_expires_at = None
                generation.completed_at = datetime.now(UTC)
                generation.last_error = f"{type(exc).__name__}: rebuild failed"[:2000]
            await session.commit()


class MemoryDiagnosticsService:
    def __init__(self, session_factory, memory: MemoryService, *, backend: str):
        self.session_factory = session_factory
        self.memory = memory
        self.backend = backend

    async def snapshot(self, *, now: datetime | None = None) -> MemoryDiagnostics:
        current_time = now or datetime.now(UTC)
        health = await self.memory.health()
        async with self.session_factory() as session:
            raw_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.text.is_not(None), func.length(func.trim(Message.text)) > 0)
                )
                or 0
            )
            ingested = int(
                await session.scalar(
                    select(func.count(distinct(MemoryIngestionReceipt.message_id)))
                    .select_from(MemoryIngestionReceipt)
                    .join(
                        MemoryIngestionState,
                        MemoryIngestionState.id == MemoryIngestionReceipt.state_id,
                    )
                    .where(MemoryIngestionState.backend == self.backend)
                )
                or 0
            )
            states = (
                (
                    await session.execute(
                        select(MemoryIngestionState).where(
                            MemoryIngestionState.backend == self.backend
                        )
                    )
                )
                .scalars()
                .all()
            )
            generations = (
                (
                    await session.execute(
                        select(MemoryGeneration).where(MemoryGeneration.backend == self.backend)
                    )
                )
                .scalars()
                .all()
            )
            registry = await session.get(MemoryBackendRegistry, self.backend)
            active = (
                await session.get(MemoryGeneration, registry.active_generation_id)
                if registry is not None and registry.active_generation_id is not None
                else None
            )
        failed = sum(state.status == MemoryIngestionStatus.FAILED for state in states)
        processing = sum(state.status == MemoryIngestionStatus.PROCESSING for state in states)
        expired = sum(
            state.status == MemoryIngestionStatus.PROCESSING
            and state.lease_expires_at is not None
            and _aware(state.lease_expires_at) <= _aware(current_time)
            for state in states
        )
        return MemoryDiagnostics(
            backend=self.backend,
            backend_ready=health.ready,
            indexing_verified_by_readiness=False,
            active_generation_id=active.id if active else None,
            active_bank_id=active.bank_id if active else None,
            active_status=active.status if active else None,
            raw_message_count=raw_count,
            ingested_message_count=ingested,
            pending_message_count=max(raw_count - ingested, 0),
            failed_ingestion_count=failed,
            processing_ingestion_count=processing,
            expired_processing_count=expired,
            building_generation_count=sum(
                item.status == MemoryGenerationStatus.BUILDING for item in generations
            ),
            failed_generation_count=sum(
                item.status == MemoryGenerationStatus.FAILED for item in generations
            ),
        )


def _archive_candidates(messages: list[Message]) -> list[ObservationCandidate]:
    grouped: dict[str, list[Message]] = defaultdict(list)
    for message in messages:
        if message.author == MessageAuthor.USER and message.text:
            grouped[" ".join(message.text.casefold().split())].append(message)
    candidates: list[ObservationCandidate] = []
    for normalized, evidence_messages in grouped.items():
        first = evidence_messages[0]
        semantics = classify_semantics(first.text or "")
        page_type, scope_key, title = _page_for(semantics.category, first.text or "")
        claim_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        candidates.append(
            ObservationCandidate(
                claim_key=f"archive:{claim_hash}",
                statement=_clip(" ".join((first.text or "").split()), 400),
                category=semantics.category,
                evidence=tuple(
                    ObservationEvidence(
                        backend_memory_id=(
                            "operation:"
                            f"{ingestion_operation_id(backend='rebuild', message_id=message.id)}"
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
    return candidates


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


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
