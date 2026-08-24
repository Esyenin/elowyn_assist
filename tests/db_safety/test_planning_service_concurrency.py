from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from elowyn.db.models import Conversation, Message, PlanVersion, Source, Strategy
from elowyn.domain.enums import (
    ActorType,
    MessageAuthor,
    PlanVersionStatus,
    SourceType,
    TransportType,
)
from elowyn.domain.errors import DomainValidationError
from elowyn.domain.planning_commands import (
    PlanCandidateCreate,
    PlanCreate,
    PlanVersionApprove,
    PlanVersionItemCreate,
    PlanVersionPresentationCreate,
)
from elowyn.services.domain_mutation import ActionContext
from elowyn.services.planning import PlanningService
from elowyn.support.consistency import ConsistencyVerifier

pytestmark = pytest.mark.postgres


async def add_user_source(session, conversation_id, *, offset: int) -> Source:
    message = Message(
        conversation_id=conversation_id,
        author=MessageAuthor.USER,
        text="synthetic approval evidence",
        sent_at=datetime.now(UTC) + timedelta(seconds=offset),
    )
    session.add(message)
    await session.flush()
    source = Source(
        source_type=SourceType.USER_MESSAGE,
        message_id=message.id,
        reason_summary="synthetic concurrency evidence",
    )
    session.add(source)
    await session.flush()
    return source


def candidate(plan_id, label: str) -> PlanCandidateCreate:
    return PlanCandidateCreate(
        plan_id=plan_id,
        summary=f"Candidate {label}",
        proposed_strategy_snapshot=f"Strategy {label}",
        items=[PlanVersionItemCreate(ordinal=1, title=f"Action {label}")],
    )


async def seed_plan(factory):
    async with factory() as session:
        conversation = Conversation(
            transport=TransportType.INTERNAL,
            external_conversation_id=f"planning-concurrency-{uuid4()}",
        )
        session.add(conversation)
        await session.flush()
        evidence = await add_user_source(session, conversation.id, offset=0)
        plan = await PlanningService(session).create_plan(
            PlanCreate(title="Concurrent Plan"),
            ActionContext(ActorType.USER, evidence),
        )
        ids = plan.entity_id, conversation.id, evidence.id
        await session.commit()
        return ids


async def seed_presented_candidate(factory):
    plan_id, conversation_id, evidence_id = await seed_plan(factory)
    async with factory() as session:
        evidence = await session.get(Source, evidence_id)
        service = PlanningService(session)
        version = await service.create_candidate_version(
            candidate(plan_id, "one"),
            ActionContext(ActorType.ASSISTANT, evidence),
        )
        assistant = Message(
            conversation_id=conversation_id,
            author=MessageAuthor.ASSISTANT,
            text="Presented Candidate one",
            sent_at=datetime.now(UTC),
        )
        session.add(assistant)
        await session.flush()
        await service.record_version_presentation(
            PlanVersionPresentationCreate(
                plan_version_id=version.id,
                message_id=assistant.id,
            ),
            ActionContext(ActorType.ASSISTANT, evidence),
        )
        first_approval = await add_user_source(session, conversation_id, offset=5)
        second_approval = await add_user_source(session, conversation_id, offset=6)
        result = version.id, evidence.id, first_approval.id, second_approval.id
        await session.commit()
        return plan_id, result


async def test_two_candidate_creations_serialize_version_numbers(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    plan_id, _, evidence_id = await seed_plan(factory)
    async with factory() as first, factory() as second:
        first_source = await first.get(Source, evidence_id)
        second_source = await second.get(Source, evidence_id)
        first_version = await PlanningService(first).create_candidate_version(
            candidate(plan_id, "first"),
            ActionContext(ActorType.ASSISTANT, first_source),
        )
        pending = asyncio.create_task(
            PlanningService(second).create_candidate_version(
                candidate(plan_id, "second"),
                ActionContext(ActorType.ASSISTANT, second_source),
            )
        )
        await asyncio.sleep(0.05)
        assert not pending.done()
        await first.commit()
        second_version = await asyncio.wait_for(pending, timeout=3)
        await second.commit()

    async with factory() as verify:
        versions = list(
            (
                await verify.execute(
                    select(PlanVersion)
                    .where(PlanVersion.plan_id == plan_id)
                    .order_by(PlanVersion.version_number)
                )
            )
            .scalars()
            .all()
        )
        assert [version.version_number for version in versions] == [1, 2]
        assert first_version.id == versions[0].id
        assert second_version.id == versions[1].id
        assert [version.status for version in versions] == [
            PlanVersionStatus.SUPERSEDED,
            PlanVersionStatus.CANDIDATE,
        ]


async def test_conflicting_approvals_leave_one_approved_and_matching_strategy(
    safety_engine,
) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    plan_id, seeded = await seed_presented_candidate(factory)
    version_id, _, first_source_id, second_source_id = seeded
    async with factory() as first, factory() as second:
        first_source = await first.get(Source, first_source_id)
        second_source = await second.get(Source, second_source_id)
        await PlanningService(first).approve_plan_version(
            PlanVersionApprove(plan_version_id=version_id),
            ActionContext(ActorType.USER, first_source),
        )
        pending = asyncio.create_task(
            PlanningService(second).approve_plan_version(
                PlanVersionApprove(plan_version_id=version_id),
                ActionContext(ActorType.USER, second_source),
            )
        )
        await asyncio.sleep(0.05)
        assert not pending.done()
        await first.commit()
        with pytest.raises(DomainValidationError, match="current Candidate"):
            await asyncio.wait_for(pending, timeout=3)
        await second.rollback()

    async with factory() as verify:
        version = await verify.get(PlanVersion, version_id)
        strategy = (
            await verify.execute(
                select(Strategy).where(Strategy.accepted_from_plan_version_id == version_id)
            )
        ).scalar_one()
        assert version.status == PlanVersionStatus.APPROVED
        assert version.approval_source_id == first_source_id
        assert strategy.approach == version.proposed_strategy_snapshot
        assert (
            await verify.execute(
                select(func.count()).select_from(PlanVersion).where(
                    PlanVersion.plan_id == plan_id,
                    PlanVersion.status == PlanVersionStatus.APPROVED,
                )
            )
        ).scalar_one() == 1


async def test_candidate_creation_waits_for_approval_without_deadlock(safety_engine) -> None:
    factory = async_sessionmaker(safety_engine, expire_on_commit=False)
    plan_id, seeded = await seed_presented_candidate(factory)
    version_id, evidence_id, approval_id, _ = seeded
    async with factory() as approval_session, factory() as candidate_session:
        approval_source = await approval_session.get(Source, approval_id)
        evidence = await candidate_session.get(Source, evidence_id)
        await PlanningService(approval_session).approve_plan_version(
            PlanVersionApprove(plan_version_id=version_id),
            ActionContext(ActorType.USER, approval_source),
        )
        pending = asyncio.create_task(
            PlanningService(candidate_session).create_candidate_version(
                candidate(plan_id, "next"),
                ActionContext(ActorType.ASSISTANT, evidence),
            )
        )
        await asyncio.sleep(0.05)
        assert not pending.done()
        await approval_session.commit()
        next_version = await asyncio.wait_for(pending, timeout=3)
        await candidate_session.commit()

    async with factory() as verify:
        approved = await verify.get(PlanVersion, version_id)
        candidate_version = await verify.get(PlanVersion, next_version.id)
        strategy = (
            await verify.execute(
                select(Strategy).where(Strategy.accepted_from_plan_version_id == version_id)
            )
        ).scalar_one()
        assert approved.status == PlanVersionStatus.APPROVED
        assert candidate_version.status == PlanVersionStatus.CANDIDATE
        assert strategy.approach == approved.proposed_strategy_snapshot
        (await ConsistencyVerifier(verify).verify()).require_ok()
