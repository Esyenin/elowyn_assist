from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from elowyn.domain.enums import (
    EvidenceStance,
    MemoryPageType,
    ObservationStatus,
    SemanticCategory,
)
from elowyn.memory.service import MemoryProvenance

OBSERVATION_DERIVATION_VERSION = "elowyn-observation-v1"
PAGE_DERIVATION_VERSION = "elowyn-memory-page-v1"


@dataclass(frozen=True)
class ObservationEvidence:
    backend_memory_id: str
    provenance: MemoryProvenance
    explicit_correction: bool = False


@dataclass(frozen=True)
class ObservationCandidate:
    claim_key: str
    statement: str
    category: SemanticCategory
    evidence: tuple[ObservationEvidence, ...]
    page_type: MemoryPageType
    page_scope_key: str
    page_title: str


@dataclass(frozen=True)
class ObservationEvidenceView:
    backend_memory_id: str
    provenance: MemoryProvenance
    stance: EvidenceStance
    assertion_text: str
    explicit_correction: bool


@dataclass(frozen=True)
class ObservationView:
    id: uuid.UUID
    claim_key: str
    statement: str
    category: SemanticCategory
    status: ObservationStatus
    confidence: float
    evidence: tuple[ObservationEvidenceView, ...]
    superseded_by_id: uuid.UUID | None
    authoritative: Literal[False] = False

    @property
    def supporting_provenance(self) -> tuple[MemoryProvenance, ...]:
        return tuple(
            item.provenance for item in self.evidence if item.stance == EvidenceStance.SUPPORTS
        )

    @property
    def contradicting_provenance(self) -> tuple[MemoryProvenance, ...]:
        return tuple(
            item.provenance
            for item in self.evidence
            if item.stance == EvidenceStance.CONTRADICTS
        )


@dataclass(frozen=True)
class MemoryPageEntry:
    observation_id: uuid.UUID
    claim_key: str
    statement: str
    category: SemanticCategory
    status: ObservationStatus
    confidence: float
    evidence_count: int


@dataclass(frozen=True)
class MemoryPageView:
    id: uuid.UUID
    page_type: MemoryPageType
    scope_key: str
    title: str
    entries: tuple[MemoryPageEntry, ...]
    authoritative: Literal[False] = False
