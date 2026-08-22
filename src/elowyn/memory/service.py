from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

MemoryKind = Literal["world", "experience", "observation"]
TagMatch = Literal["any", "all", "any_strict", "all_strict", "exact"]


class MemoryBackendError(RuntimeError):
    """An Elowyn-owned error boundary for replaceable memory backends."""


@dataclass(frozen=True)
class MemoryHealth:
    backend: str
    ready: bool
    api_version: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class MemorySource:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    role: str
    occurred_at: datetime


@dataclass(frozen=True)
class RetainMessage:
    source: MemorySource
    text: str
    topic_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetainResult:
    operation_id: uuid.UUID
    accepted_items: int
    asynchronous: bool = True


@dataclass(frozen=True)
class RecallQuery:
    text: str
    kinds: tuple[MemoryKind, ...] = ("world", "experience")
    max_tokens: int = 2048
    tags: tuple[str, ...] = ()
    tags_match: TagMatch = "any"
    query_timestamp: datetime | None = None


@dataclass(frozen=True)
class RecalledMemory:
    backend_id: str
    text: str
    kind: MemoryKind | str | None
    document_id: str | None
    source: MemorySource | None
    metadata: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    authoritative: Literal[False] = False


@dataclass(frozen=True)
class RecallResult:
    memories: tuple[RecalledMemory, ...]
    authoritative: Literal[False] = False


@dataclass(frozen=True)
class ReflectQuery:
    text: str
    max_tokens: int = 2048
    tags: tuple[str, ...] = ()
    tags_match: TagMatch = "any"
    kinds: tuple[MemoryKind, ...] = ("world", "experience")


@dataclass(frozen=True)
class Reflection:
    text: str
    evidence_backend_ids: tuple[str, ...] = ()
    authoritative: Literal[False] = False


class MemoryService(Protocol):
    """Backend-neutral semantic-memory boundary owned by Elowyn."""

    async def health(self) -> MemoryHealth: ...

    async def retain(
        self,
        messages: tuple[RetainMessage, ...],
        *,
        operation_id: uuid.UUID | None = None,
    ) -> RetainResult: ...

    async def recall(self, query: RecallQuery) -> RecallResult: ...

    async def reflect(self, query: ReflectQuery) -> Reflection: ...

    async def close(self) -> None: ...
