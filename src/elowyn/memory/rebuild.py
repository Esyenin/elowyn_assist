from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from elowyn.domain.enums import MemoryGenerationStatus


@dataclass(frozen=True)
class MemoryRebuildResult:
    generation_id: uuid.UUID
    bank_id: str
    messages_replayed: int
    messages_verified: int
    active: Literal[True] = True


@dataclass(frozen=True)
class MemoryCleanupCandidate:
    generation_id: uuid.UUID
    bank_id: str
    status: MemoryGenerationStatus


@dataclass(frozen=True)
class MemoryDiagnostics:
    backend: str
    backend_ready: bool
    indexing_verified_by_readiness: Literal[False]
    active_generation_id: uuid.UUID | None
    active_bank_id: str | None
    active_status: MemoryGenerationStatus | None
    raw_message_count: int
    ingested_message_count: int
    pending_message_count: int
    failed_ingestion_count: int
    processing_ingestion_count: int
    expired_processing_count: int
    building_generation_count: int
    failed_generation_count: int
    derived_refresh_pending_count: int


class MemoryRebuildError(RuntimeError):
    """A sanitized failure boundary for explicit rebuild maintenance."""
