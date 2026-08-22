from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.base import Base
from elowyn.db.models import (
    Conversation,
    MemoryObservation,
    MemoryObservationEvidence,
    MemoryPage,
    Message,
)
from elowyn.domain.enums import (
    MemoryPageType,
    MessageAuthor,
    ObservationStatus,
    SemanticCategory,
    TransportType,
)
from elowyn.memory.observations import ObservationCandidate, ObservationEvidence
from elowyn.memory.service import MemoryProvenance
from elowyn.services.memory_consolidation import (
    MemoryDerivedRebuilder,
    MemoryPageService,
    ObservationConsolidationService,
)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _evidence(
    session,
    conversation: Conversation,
    *,
    index: int,
    text: str,
    correction: bool = False,
) -> ObservationEvidence:
    sent_at = datetime(2026, 8, 22, 10, 0, tzinfo=UTC) + timedelta(minutes=index)
    message = Message(
        conversation_id=conversation.id,
        author=MessageAuthor.USER,
        text=text,
        sent_at=sent_at,
    )
    session.add(message)
    await session.flush()
    return ObservationEvidence(
        backend_memory_id=f"synthetic-memory-{index}",
        provenance=MemoryProvenance(
            conversation_id=conversation.id,
            message_id=message.id,
            role=MessageAuthor.USER.value,
            occurred_at=sent_at,
        ),
        explicit_correction=correction,
    )


def _preference_candidate(
    statement: str,
    evidence: tuple[ObservationEvidence, ...],
) -> ObservationCandidate:
    return ObservationCandidate(
        claim_key="communication.response_style",
        statement=statement,
        category=SemanticCategory.PREFERENCE,
        evidence=evidence,
        page_type=MemoryPageType.COMMUNICATION_PREFERENCES,
        page_scope_key="user",
        page_title="Communication Preferences",
    )


@pytest.mark.asyncio
async def test_single_preference_stays_candidate_and_does_not_create_page(
    session_factory,
) -> None:
    async with session_factory() as session:
        conversation = Conversation(transport=TransportType.INTERNAL)
        session.add(conversation)
        await session.flush()
        evidence = await _evidence(
            session, conversation, index=1, text="I prefer concise replies today."
        )

        observation = await ObservationConsolidationService(session).consolidate(
            _preference_candidate("User prefers concise replies.", (evidence,))
        )
        page = await MemoryPageService(session).refresh(
            page_type=MemoryPageType.COMMUNICATION_PREFERENCES,
            scope_key="user",
            title="Communication Preferences",
        )

        assert observation.status == ObservationStatus.CANDIDATE
        assert observation.authoritative is False
        assert page is None


@pytest.mark.asyncio
async def test_repeated_evidence_forms_observation_and_provenance_backed_page(
    session_factory,
) -> None:
    async with session_factory() as session:
        conversation = Conversation(transport=TransportType.INTERNAL)
        session.add(conversation)
        await session.flush()
        first = await _evidence(session, conversation, index=1, text="Please keep replies concise.")
        second = await _evidence(session, conversation, index=2, text="Concise replies work best.")
        consolidation = ObservationConsolidationService(session)
        candidate = _preference_candidate("User prefers concise replies.", (first, second))

        observation = await consolidation.consolidate(candidate)
        repeated = await consolidation.consolidate(candidate)
        page = await MemoryPageService(session).refresh(
            page_type=MemoryPageType.COMMUNICATION_PREFERENCES,
            scope_key="user",
            title="Communication Preferences",
        )

        assert observation.status == ObservationStatus.ACTIVE
        assert observation.confidence == 1.0
        assert len(observation.supporting_provenance) == 2
        assert {item.backend_memory_id for item in observation.evidence} == {
            "synthetic-memory-1",
            "synthetic-memory-2",
        }
        assert repeated.id == observation.id
        assert len(repeated.supporting_provenance) == 2
        assert page is not None
        assert page.authoritative is False
        assert len(page.entries) == 1
        assert page.entries[0].evidence_count == 2
        chain = await MemoryPageService(session).observation_chain(page.id)
        assert chain[0].id == observation.id
        assert {item.message_id for item in chain[0].supporting_provenance} == {
            first.provenance.message_id,
            second.provenance.message_id,
        }


@pytest.mark.asyncio
async def test_contradiction_is_preserved_and_explicit_correction_supersedes(
    session_factory,
) -> None:
    async with session_factory() as session:
        conversation = Conversation(transport=TransportType.INTERNAL)
        session.add(conversation)
        await session.flush()
        tea_one = await _evidence(session, conversation, index=1, text="I prefer tea.")
        tea_two = await _evidence(session, conversation, index=2, text="Tea is my preference.")
        coffee_mention = await _evidence(
            session, conversation, index=3, text="Coffee may work better now."
        )
        coffee_correction = await _evidence(
            session,
            conversation,
            index=4,
            text="Correction: I now prefer coffee, not tea.",
            correction=True,
        )
        service = ObservationConsolidationService(session)
        tea = await service.consolidate(
            _preference_candidate("User prefers tea.", (tea_one, tea_two))
        )

        coffee_candidate = await service.consolidate(
            _preference_candidate("User prefers coffee.", (coffee_mention,))
        )
        contested_tea = await service.view(tea.id)
        assert coffee_candidate.status == ObservationStatus.CANDIDATE
        assert contested_tea.status == ObservationStatus.CONTESTED
        assert contested_tea.confidence == pytest.approx(2 / 3)
        assert contested_tea.contradicting_provenance == (coffee_mention.provenance,)

        corrected = await service.consolidate(
            _preference_candidate("User prefers coffee.", (coffee_correction,))
        )
        superseded_tea = await service.view(tea.id)
        assert corrected.status == ObservationStatus.ACTIVE
        assert corrected.authoritative is False
        assert superseded_tea.status == ObservationStatus.SUPERSEDED
        assert superseded_tea.superseded_by_id == corrected.id
        assert {
            item.message_id for item in superseded_tea.contradicting_provenance
        } == {
            coffee_mention.provenance.message_id,
            coffee_correction.provenance.message_id,
        }
        page = await MemoryPageService(session).refresh(
            page_type=MemoryPageType.COMMUNICATION_PREFERENCES,
            scope_key="user",
            title="Communication Preferences",
        )
        assert page is not None
        assert [entry.statement for entry in page.entries] == ["User prefers coffee."]


@pytest.mark.asyncio
async def test_page_is_compact_relevant_and_project_page_excludes_canonical_state(
    session_factory,
) -> None:
    async with session_factory() as session:
        conversation = Conversation(transport=TransportType.INTERNAL)
        session.add(conversation)
        await session.flush()
        service = ObservationConsolidationService(session)
        for index, (claim_key, statement) in enumerate(
            [
                ("project.rationale", "The project favors local-first operation."),
                ("project.history", "The project started as a personal assistant."),
                ("task:canonical", "Canonical task status is DONE."),
            ],
            start=1,
        ):
            first = await _evidence(session, conversation, index=index * 2, text=statement)
            second = await _evidence(session, conversation, index=index * 2 + 1, text=statement)
            await service.consolidate(
                ObservationCandidate(
                    claim_key=claim_key,
                    statement=statement,
                    category=SemanticCategory.CONTEXT,
                    evidence=(first, second),
                    page_type=MemoryPageType.PROJECT,
                    page_scope_key="elowyn",
                    page_title="Project: Elowyn",
                )
            )
        unrelated_one = await _evidence(
            session, conversation, index=20, text="User likes walking."
        )
        unrelated_two = await _evidence(
            session, conversation, index=21, text="Walking is enjoyable."
        )
        await service.consolidate(
            ObservationCandidate(
                claim_key="profile.activity",
                statement="User likes walking.",
                category=SemanticCategory.PREFERENCE,
                evidence=(unrelated_one, unrelated_two),
                page_type=MemoryPageType.USER_PROFILE,
                page_scope_key="user",
                page_title="User Profile",
            )
        )

        page = await MemoryPageService(session).refresh(
            page_type=MemoryPageType.PROJECT,
            scope_key="elowyn",
            title="Project: Elowyn",
            max_entries=2,
        )

        assert page is not None
        assert len(page.entries) == 2
        statements = {entry.statement for entry in page.entries}
        assert "Canonical task status is DONE." not in statements
        assert "User likes walking." not in statements
        assert all(len(entry.statement) < 100 for entry in page.entries)


@pytest.mark.asyncio
async def test_rebuild_recreates_derived_observations_and_pages_only(session_factory) -> None:
    async with session_factory() as session:
        conversation = Conversation(transport=TransportType.INTERNAL)
        session.add(conversation)
        await session.flush()
        first = await _evidence(session, conversation, index=1, text="Keep replies concise.")
        second = await _evidence(session, conversation, index=2, text="Concise is preferred.")
        candidate = _preference_candidate("User prefers concise replies.", (first, second))
        rebuilder = MemoryDerivedRebuilder(session)

        first_pages = await rebuilder.rebuild((candidate,))
        first_content = [entry.statement for entry in first_pages[0].entries]
        second_pages = await rebuilder.rebuild((candidate,))
        second_content = [entry.statement for entry in second_pages[0].entries]

        assert first_content == second_content == ["User prefers concise replies."]
        assert await session.scalar(select(func.count()).select_from(Message)) == 2
        assert await session.scalar(select(func.count()).select_from(MemoryObservation)) == 1
        evidence_count = await session.scalar(
            select(func.count()).select_from(MemoryObservationEvidence)
        )
        assert evidence_count == 2
        assert await session.scalar(select(func.count()).select_from(MemoryPage)) == 1
