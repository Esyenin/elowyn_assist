from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import wraps
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import Entity, Event, Operation, Source, SourceDependency
from elowyn.domain.enums import ActorType, EventType, SourceType
from elowyn.domain.errors import DomainValidationError


@dataclass(frozen=True)
class ActionContext:
    actor_type: ActorType
    source: Source | None = None
    description: str | None = None
    operation_id: uuid.UUID | None = None


def atomic_domain_action(method):
    """Rollback a failed domain action to a SAVEPOINT without discarding its outer turn."""

    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        exclusive = method.__name__ == "undo_last_change" and kwargs.get("entity_id") is None
        await self._lock_world_state(exclusive=exclusive)
        async with self.session.begin_nested():
            return await method(self, *args, **kwargs)

    return wrapped


def json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, uuid.UUID)):
        return str(value)
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def change(field: str, old: Any, new: Any) -> dict[str, Any]:
    return {"field": field, "old": json_value(old), "new": json_value(new)}


class DomainMutationService:
    """Shared transaction, locking, and provenance primitives for canonical domain services."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._last_event_at: datetime | None = None

    def _uses_postgresql(self) -> bool:
        get_bind = getattr(self.session, "get_bind", None)
        if get_bind is not None:
            return get_bind().dialect.name == "postgresql"
        sync_session = getattr(self.session, "sync", None)
        return sync_session is not None and sync_session.get_bind().dialect.name == "postgresql"

    async def _lock_world_state(self, *, exclusive: bool) -> None:
        """Coordinate global undo with concurrent canonical mutations in this transaction."""

        if not self._uses_postgresql():
            return
        function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        await self.session.execute(text(f"SELECT {function}(5044031582654955025)"))

    async def _lock_entities(self, entity_ids: list[uuid.UUID]) -> None:
        """Lock identities in UUID order so concurrent multi-row actions cannot deadlock."""

        ordered_ids = sorted(set(entity_ids))
        if not ordered_ids or not self._uses_postgresql():
            return
        with self.session.no_autoflush:
            await self.session.execute(
                select(Entity)
                .where(Entity.id.in_(ordered_ids))
                .order_by(Entity.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )

    async def _operation(self, ctx: ActionContext) -> Operation:
        if ctx.operation_id is not None:
            existing = await self.session.get(Operation, ctx.operation_id)
            if existing is not None:
                return existing

        operation = Operation(
            id=ctx.operation_id if ctx.operation_id is not None else uuid.uuid4(),
            actor_type=ctx.actor_type,
            source_id=ctx.source.id if ctx.source else None,
            description=ctx.description,
        )
        self.session.add(operation)
        await self.session.flush()
        return operation

    async def _append_event(
        self,
        *,
        operation: Operation,
        event_type: EventType,
        entity_id: uuid.UUID | None,
        source: Source | None,
        changes: list[dict[str, Any]],
        reverses_event_id: uuid.UUID | None = None,
    ) -> Event:
        created_at = datetime.now(UTC)
        if self._last_event_at is not None and created_at <= self._last_event_at:
            created_at = self._last_event_at + timedelta(microseconds=1)
        self._last_event_at = created_at
        event = Event(
            operation_id=operation.id,
            event_type=event_type,
            entity_id=entity_id,
            source_id=source.id if source else None,
            reverses_event_id=reverses_event_id,
            changes=changes,
            created_at=created_at,
        )
        self.session.add(event)
        await self.session.flush()
        return event


async def assistant_inference_source(
    session: AsyncSession,
    *,
    confidence: float,
    reason_summary: str,
    evidence_sources: list[Source] | None = None,
) -> Source:
    """Create one inference Source grounded in one or more existing evidence Sources."""

    if not 0 <= confidence <= 1:
        raise DomainValidationError("assistant inference confidence must be between 0 and 1")
    if not reason_summary.strip():
        raise DomainValidationError("assistant inference requires reason_summary")
    source = Source(
        source_type=SourceType.ASSISTANT_INFERENCE,
        confidence=confidence,
        reason_summary=reason_summary.strip(),
    )
    session.add(source)
    await session.flush()
    for evidence in dict.fromkeys(evidence_sources or []):
        session.add(SourceDependency(source_id=source.id, evidence_source_id=evidence.id))
    await session.flush()
    return source
