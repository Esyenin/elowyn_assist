from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from elowyn.memory.service import MemoryProvenance, MemorySemantics


class DeepMemoryRoute(StrEnum):
    NONE = "NONE"
    RECALL = "RECALL"
    REFLECT = "REFLECT"
    EXACT_SOURCE = "EXACT_SOURCE"


@dataclass(frozen=True)
class DeepRecallItem:
    backend_id: str
    text: str
    semantics: MemorySemantics
    provenance: MemoryProvenance
    mentioned_at: datetime
    text_truncated: bool = False
    authoritative: Literal[False] = False


@dataclass(frozen=True)
class DeepRecallView:
    available: bool
    context: str
    items: tuple[DeepRecallItem, ...]
    token_upper_bound: int
    truncated: bool
    authoritative: Literal[False] = False


@dataclass(frozen=True)
class DeepReflectionView:
    available: bool
    synthesis: str
    evidence_backend_ids: tuple[str, ...]
    token_upper_bound: int
    truncated: bool
    authoritative: Literal[False] = False


@dataclass(frozen=True)
class ExactSourceContextMessage:
    message_id: uuid.UUID
    role: str
    sent_at: datetime
    raw_text: str
    text_truncated: bool = False


@dataclass(frozen=True)
class ExactSourceView:
    found: bool
    source_ref: str
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    role: str | None
    sent_at: datetime | None
    raw_text: str | None
    surrounding_context: tuple[ExactSourceContextMessage, ...] = ()
    token_upper_bound: int = 0
    truncated: bool = False
    context_complete: bool = False
    canonical_raw_source: bool = False
    world_state_authority: Literal[False] = False
