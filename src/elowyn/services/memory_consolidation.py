from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import (
    MemoryObservation,
    MemoryObservationEvidence,
    MemoryPage,
    MemoryPageObservation,
    Message,
)
from elowyn.domain.enums import (
    EvidenceStance,
    MemoryPageType,
    ObservationStatus,
    SemanticCategory,
)
from elowyn.memory.observations import (
    OBSERVATION_DERIVATION_VERSION,
    PAGE_DERIVATION_VERSION,
    MemoryPageEntry,
    MemoryPageView,
    ObservationCandidate,
    ObservationEvidence,
    ObservationEvidenceView,
    ObservationView,
)
from elowyn.memory.service import MemoryProvenance
from elowyn.services.memory_provenance import MemoryProvenanceService

_CANONICAL_PROJECT_PREFIXES = ("task:", "goal:", "decision:")


class ObservationConsolidationService:
    """Consolidate atomic evidence without granting observations authority."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def consolidate(self, candidate: ObservationCandidate) -> ObservationView:
        normalized = await self._validated(candidate)
        observations = list(
            (
                await self.session.execute(
                    select(MemoryObservation)
                    .where(
                        MemoryObservation.claim_key == normalized.claim_key,
                        MemoryObservation.page_type == normalized.page_type,
                        MemoryObservation.page_scope_key == normalized.page_scope_key,
                        MemoryObservation.status != ObservationStatus.SUPERSEDED,
                    )
                    .order_by(MemoryObservation.created_at, MemoryObservation.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        same = next(
            (
                observation
                for observation in observations
                if _normalized_statement(observation.statement)
                == _normalized_statement(normalized.statement)
            ),
            None,
        )
        competing = [observation for observation in observations if observation is not same]

        if same is None:
            same = MemoryObservation(
                claim_key=normalized.claim_key,
                statement=normalized.statement,
                category=normalized.category,
                status=ObservationStatus.CANDIDATE,
                confidence=0.0,
                page_type=normalized.page_type,
                page_scope_key=normalized.page_scope_key,
                derivation_version=OBSERVATION_DERIVATION_VERSION,
            )
            self.session.add(same)
            await self.session.flush()

        for evidence in normalized.evidence:
            await self._add_evidence(
                same,
                evidence,
                stance=EvidenceStance.SUPPORTS,
                assertion_text=normalized.statement,
            )
            for old in competing:
                await self._add_evidence(
                    old,
                    evidence,
                    stance=EvidenceStance.CONTRADICTS,
                    assertion_text=normalized.statement,
                )

        explicit_correction = bool(competing) and any(
            evidence.explicit_correction for evidence in normalized.evidence
        )
        await self._refresh_observation(same)
        for old in competing:
            await self._refresh_observation(old)
            if explicit_correction:
                old.status = ObservationStatus.SUPERSEDED
                old.superseded_by_id = same.id

        if explicit_correction:
            same.status = ObservationStatus.ACTIVE
        await self.session.flush()
        return await self.view(same.id)

    async def view(self, observation_id: uuid.UUID) -> ObservationView:
        observation = await self.session.get(MemoryObservation, observation_id)
        if observation is None:
            raise LookupError("memory observation was not found")
        evidence_rows = (
            (
                await self.session.execute(
                    select(MemoryObservationEvidence, Message)
                    .join(Message, Message.id == MemoryObservationEvidence.message_id)
                    .where(MemoryObservationEvidence.observation_id == observation.id)
                    .order_by(MemoryObservationEvidence.created_at, Message.id)
                )
            )
            .all()
        )
        evidence_views: list[ObservationEvidenceView] = []
        for evidence, message in evidence_rows:
            provenance = MemoryProvenance(
                conversation_id=message.conversation_id,
                message_id=message.id,
                role=message.author.value,
                occurred_at=_as_utc(message.sent_at),
            )
            evidence_views.append(
                ObservationEvidenceView(
                    backend_memory_id=evidence.backend_memory_id,
                    provenance=provenance,
                    stance=evidence.stance,
                    assertion_text=evidence.assertion_text,
                    explicit_correction=evidence.explicit_correction,
                )
            )
        return ObservationView(
            id=observation.id,
            claim_key=observation.claim_key,
            statement=observation.statement,
            category=observation.category,
            status=observation.status,
            confidence=observation.confidence,
            evidence=tuple(evidence_views),
            superseded_by_id=observation.superseded_by_id,
        )

    async def _validated(self, candidate: ObservationCandidate) -> ObservationCandidate:
        claim_key = candidate.claim_key.strip().casefold()
        statement = " ".join(candidate.statement.split())
        scope_key = candidate.page_scope_key.strip()
        title = " ".join(candidate.page_title.split())
        if not claim_key or len(claim_key) > 255:
            raise ValueError("observation claim key is invalid")
        if not statement or len(statement) > 400:
            raise ValueError("observation statement is invalid")
        if not scope_key or len(scope_key) > 255 or not title or len(title) > 255:
            raise ValueError("observation page scope is invalid")
        if not candidate.evidence:
            raise ValueError("observation requires evidence")
        distinct: dict[uuid.UUID, ObservationEvidence] = {}
        resolver = MemoryProvenanceService(self.session)
        for evidence in candidate.evidence:
            backend_memory_id = evidence.backend_memory_id.strip()
            if not backend_memory_id:
                raise ValueError("observation backend memory ID is required")
            await resolver.resolve_message(evidence.provenance)
            if evidence.explicit_correction and evidence.provenance.role != "USER":
                raise ValueError("only explicit user evidence can be a correction")
            distinct[evidence.provenance.message_id] = ObservationEvidence(
                backend_memory_id=backend_memory_id,
                provenance=evidence.provenance,
                explicit_correction=evidence.explicit_correction,
            )
        return ObservationCandidate(
            claim_key=claim_key,
            statement=statement,
            category=candidate.category,
            evidence=tuple(distinct.values()),
            page_type=candidate.page_type,
            page_scope_key=scope_key,
            page_title=title,
        )

    async def _add_evidence(
        self,
        observation: MemoryObservation,
        evidence: ObservationEvidence,
        *,
        stance: EvidenceStance,
        assertion_text: str,
    ) -> None:
        existing = await self.session.get(
            MemoryObservationEvidence,
            (observation.id, evidence.provenance.message_id),
        )
        if existing is not None:
            return
        self.session.add(
            MemoryObservationEvidence(
                observation_id=observation.id,
                message_id=evidence.provenance.message_id,
                backend_memory_id=evidence.backend_memory_id,
                stance=stance,
                assertion_text=assertion_text,
                explicit_correction=evidence.explicit_correction,
                occurred_at=evidence.provenance.occurred_at,
            )
        )
        await self.session.flush()

    async def _refresh_observation(self, observation: MemoryObservation) -> None:
        evidence = (
            (
                await self.session.execute(
                    select(MemoryObservationEvidence).where(
                        MemoryObservationEvidence.observation_id == observation.id
                    )
                )
            )
            .scalars()
            .all()
        )
        supports = sum(
            2 if item.explicit_correction else 1
            for item in evidence
            if item.stance == EvidenceStance.SUPPORTS
        )
        contradicts = sum(
            2 if item.explicit_correction else 1
            for item in evidence
            if item.stance == EvidenceStance.CONTRADICTS
        )
        total = supports + contradicts
        observation.confidence = supports / total if total else 0.0
        if observation.status == ObservationStatus.SUPERSEDED:
            return
        support_messages = sum(item.stance == EvidenceStance.SUPPORTS for item in evidence)
        if support_messages < 2:
            observation.status = ObservationStatus.CANDIDATE
        elif contradicts:
            observation.status = ObservationStatus.CONTESTED
        else:
            observation.status = ObservationStatus.ACTIVE


class MemoryPageService:
    """Materialize compact Elowyn-owned pages from visible observations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def refresh(
        self,
        *,
        page_type: MemoryPageType,
        scope_key: str,
        title: str,
        max_entries: int = 6,
    ) -> MemoryPageView | None:
        scope_key = scope_key.strip()
        title = " ".join(title.split())
        if not scope_key or not title:
            raise ValueError("memory page scope and title are required")
        if max_entries < 1 or max_entries > 12:
            raise ValueError("memory page entry limit must be between 1 and 12")
        observations = list(
            (
                await self.session.execute(
                    select(MemoryObservation).where(
                        MemoryObservation.page_type == page_type,
                        MemoryObservation.page_scope_key == scope_key,
                        MemoryObservation.status.in_(
                            (ObservationStatus.ACTIVE, ObservationStatus.CONTESTED)
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        if page_type == MemoryPageType.PROJECT:
            observations = [
                observation
                for observation in observations
                if not observation.claim_key.startswith(_CANONICAL_PROJECT_PREFIXES)
            ]
        observations.sort(
            key=lambda observation: (
                observation.status == ObservationStatus.ACTIVE,
                observation.confidence,
                observation.updated_at,
            ),
            reverse=True,
        )
        selected = observations[:max_entries]
        existing = (
            await self.session.execute(
                select(MemoryPage).where(
                    MemoryPage.page_type == page_type,
                    MemoryPage.scope_key == scope_key,
                )
            )
        ).scalar_one_or_none()
        if not selected:
            if existing is not None:
                await self.session.delete(existing)
                await self.session.flush()
            return None
        page = existing or MemoryPage(page_type=page_type, scope_key=scope_key)
        if existing is None:
            self.session.add(page)
        page.title = title
        page.max_entries = max_entries
        page.derivation_version = PAGE_DERIVATION_VERSION
        page.refreshed_at = datetime.now(UTC)
        page.entries = [await self._entry(observation) for observation in selected]
        await self.session.flush()
        await self.session.execute(
            delete(MemoryPageObservation).where(MemoryPageObservation.page_id == page.id)
        )
        for observation in selected:
            self.session.add(
                MemoryPageObservation(page_id=page.id, observation_id=observation.id)
            )
        await self.session.flush()
        return _page_view(page)

    async def get(self, page_id: uuid.UUID) -> MemoryPageView:
        page = await self.session.get(MemoryPage, page_id)
        if page is None:
            raise LookupError("memory page was not found")
        return _page_view(page)

    async def observation_chain(self, page_id: uuid.UUID) -> tuple[ObservationView, ...]:
        observation_ids = (
            (
                await self.session.execute(
                    select(MemoryPageObservation.observation_id).where(
                        MemoryPageObservation.page_id == page_id
                    )
                )
            )
            .scalars()
            .all()
        )
        consolidation = ObservationConsolidationService(self.session)
        return tuple(
            [await consolidation.view(observation_id) for observation_id in observation_ids]
        )

    async def _entry(self, observation: MemoryObservation) -> dict[str, object]:
        evidence_count = await self.session.scalar(
            select(func.count())
            .select_from(MemoryObservationEvidence)
            .where(MemoryObservationEvidence.observation_id == observation.id)
        )
        return {
            "observation_id": str(observation.id),
            "statement": observation.statement,
            "category": observation.category.value,
            "status": observation.status.value,
            "confidence": round(observation.confidence, 3),
            "evidence_count": int(evidence_count or 0),
        }


class MemoryDerivedRebuilder:
    """Delete and deterministically recreate only disposable memory derivatives."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def rebuild(
        self, candidates: tuple[ObservationCandidate, ...]
    ) -> tuple[MemoryPageView, ...]:
        await self.session.execute(delete(MemoryPageObservation))
        await self.session.execute(delete(MemoryPage))
        await self.session.execute(delete(MemoryObservationEvidence))
        await self.session.execute(delete(MemoryObservation))
        await self.session.flush()
        consolidation = ObservationConsolidationService(self.session)
        page_specs: dict[tuple[MemoryPageType, str], tuple[str, int]] = {}
        for candidate in candidates:
            await consolidation.consolidate(candidate)
            page_specs[(candidate.page_type, candidate.page_scope_key.strip())] = (
                candidate.page_title,
                6,
            )
        pages = MemoryPageService(self.session)
        rebuilt: list[MemoryPageView] = []
        for (page_type, scope_key), (title, max_entries) in page_specs.items():
            page = await pages.refresh(
                page_type=page_type,
                scope_key=scope_key,
                title=title,
                max_entries=max_entries,
            )
            if page is not None:
                rebuilt.append(page)
        return tuple(rebuilt)


def _page_view(page: MemoryPage) -> MemoryPageView:
    entries = tuple(
        MemoryPageEntry(
            observation_id=uuid.UUID(str(item["observation_id"])),
            statement=str(item["statement"]),
            category=_category(str(item["category"])),
            status=ObservationStatus(str(item["status"])),
            confidence=float(item["confidence"]),
            evidence_count=int(item["evidence_count"]),
        )
        for item in page.entries
    )
    return MemoryPageView(
        id=page.id,
        page_type=page.page_type,
        scope_key=page.scope_key,
        title=page.title,
        entries=entries,
    )


def _category(value: str) -> SemanticCategory:
    return SemanticCategory(value)


def _normalized_statement(value: str) -> str:
    return " ".join(value.split()).casefold()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
