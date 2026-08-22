from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

MemoryKind = Literal["world", "experience", "observation"]
TagMatch = Literal["any", "all", "any_strict", "all_strict", "exact"]


class SemanticCategory(StrEnum):
    FACT = "FACT"
    PREFERENCE = "PREFERENCE"
    CONTEXT = "CONTEXT"
    IDEA = "IDEA"
    EPISODE = "EPISODE"
    CONSTRAINT = "CONSTRAINT"
    OBSERVATION = "OBSERVATION"


class EpistemicStatus(StrEnum):
    MENTIONED = "MENTIONED"
    CONSIDERED = "CONSIDERED"
    PREFERRED = "PREFERRED"
    DECIDED = "DECIDED"
    CURRENTLY_TRUE = "CURRENTLY_TRUE"


class MemoryBackendError(RuntimeError):
    """An Elowyn-owned error boundary for replaceable memory backends."""


@dataclass(frozen=True)
class MemoryHealth:
    backend: str
    ready: bool
    api_version: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class MemoryProvenance:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    role: str
    occurred_at: datetime

    @property
    def source_ref(self) -> str:
        return f"elowyn:message:{self.message_id}"

    @property
    def document_id(self) -> str:
        return f"elowyn:conversation:{self.conversation_id}"


# Compatibility name for Slice 3 callers. The object is Elowyn-owned provenance,
# not a Hindsight metadata record and not a duplicate Core Source row.
MemorySource = MemoryProvenance


@dataclass(frozen=True)
class MemorySemantics:
    category: SemanticCategory
    status: EpistemicStatus


@dataclass(frozen=True)
class MemoryTemporal:
    mentioned_at: datetime
    occurred_start: datetime | None = None
    occurred_end: datetime | None = None


@dataclass(frozen=True)
class RetainMessage:
    source: MemorySource
    text: str
    topic_tags: tuple[str, ...] = ()
    semantics: MemorySemantics | None = None

    @property
    def provenance(self) -> MemoryProvenance:
        return self.source


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
    semantics: MemorySemantics
    backend_kind: MemoryKind | str | None
    document_id: str | None
    source: MemorySource | None
    temporal: MemoryTemporal
    metadata: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    authoritative: Literal[False] = False

    @property
    def kind(self) -> SemanticCategory:
        return self.semantics.category

    @property
    def provenance(self) -> MemoryProvenance | None:
        return self.source


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
