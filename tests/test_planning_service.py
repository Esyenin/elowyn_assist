from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from elowyn.db.base import Base
from elowyn.db.models import (
    Conversation,
    Decision,
    Entity,
    Event,
    Goal,
    Message,
    PlanItemProgress,
    PlanVersion,
    Project,
    Source,
    SourceDependency,
    Strategy,
    Task,
)
from elowyn.domain.commands import GoalCreate, GoalUpdate, TaskCreate
from elowyn.domain.enums import (
    ActorType,
    EventType,
    MessageAuthor,
    PlanGoalRole,
    PlanItemProgressStatus,
    PlanVersionBasisRole,
    PlanVersionStatus,
    SourceType,
    TransportType,
)
from elowyn.domain.errors import DomainValidationError, EntityNotFoundError
from elowyn.domain.planning_commands import (
    PlanCandidateCreate,
    PlanCandidateReject,
    PlanCreate,
    PlanGoalLinkCreate,
    PlanItemProgressUpdate,
    PlanVersionApprove,
    PlanVersionBasisCreate,
    PlanVersionItemCreate,
    PlanVersionItemDependencyCreate,
    PlanVersionPresentationCreate,
)
from elowyn.services.domain_mutation import ActionContext
from elowyn.services.planning import PlanningService
from elowyn.services.planning_query import PlanningQueryService
from elowyn.services.world_state import WorldStateService


class _AsyncTransaction:
    def __init__(self, transaction: AbstractContextManager):
        self.transaction = transaction

    async def __aenter__(self):
        return self.transaction.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.transaction.__exit__(exc_type, exc, tb)


class AsyncSessionShim:
    def __init__(self, session: Session):
        self.sync = session

    def add(self, obj) -> None:
        self.sync.add(obj)

    def add_all(self, objects) -> None:
        self.sync.add_all(objects)

    async def flush(self) -> None:
        self.sync.flush()

    async def get(self, model, ident):
        return self.sync.get(model, ident)

    async def execute(self, statement):
        return self.sync.execute(statement)

    async def commit(self) -> None:
        self.sync.commit()

    async def rollback(self) -> None:
        self.sync.rollback()

    def begin_nested(self):
        return _AsyncTransaction(self.sync.begin_nested())

    @property
    def no_autoflush(self):
        return self.sync.no_autoflush


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as sync_session:
        yield AsyncSessionShim(sync_session)
    engine.dispose()


async def source_with_message(
    session: AsyncSessionShim,
    *,
    conversation: Conversation | None = None,
    author: MessageAuthor = MessageAuthor.USER,
    offset_seconds: int = 0,
) -> tuple[Source, Message, Conversation]:
    if conversation is None:
        conversation = Conversation(
            transport=TransportType.INTERNAL,
            external_conversation_id=f"planning-{uuid4()}",
        )
        session.add(conversation)
        await session.flush()
    message = Message(
        conversation_id=conversation.id,
        author=author,
        text=f"synthetic {author.value}",
        sent_at=datetime.now(UTC) + timedelta(seconds=offset_seconds),
    )
    session.add(message)
    await session.flush()
    source = Source(
        source_type=(
            SourceType.USER_MESSAGE if author == MessageAuthor.USER else SourceType.SYSTEM
        ),
        message_id=message.id,
        reason_summary="synthetic planning evidence",
    )
    session.add(source)
    await session.flush()
    return source, message, conversation


async def seed_plan(session: AsyncSessionShim):
    source, _, conversation = await source_with_message(session)
    goal = await WorldStateService(session).create_goal(
        GoalCreate(title="Synthetic goal"),
        ActionContext(ActorType.USER, source),
    )
    goal_event = (
        await session.execute(
            select(Event).where(
                Event.entity_id == goal.entity_id,
                Event.event_type == EventType.GOAL_CREATED,
            )
        )
    ).scalar_one()
    plan = await PlanningService(session).create_plan(
        PlanCreate(
            title="Synthetic plan",
            goals=[PlanGoalLinkCreate(goal_id=goal.entity_id, role=PlanGoalRole.PRIMARY)],
        ),
        ActionContext(ActorType.USER, source),
    )
    return source, conversation, goal, goal_event, plan


def candidate_command(plan_id, goal, goal_event, *, strategy="First strategy", based_on=None):
    first = PlanVersionItemCreate(ordinal=1, title="First action")
    second = PlanVersionItemCreate(ordinal=2, title="Second action")
    return PlanCandidateCreate(
        plan_id=plan_id,
        summary=f"Plan using {strategy}",
        proposed_strategy_snapshot=strategy,
        strategy_rationale_snapshot="Evidence-backed rationale",
        based_on_version_id=based_on,
        items=[first, second],
        dependencies=[
            PlanVersionItemDependencyCreate(
                prerequisite_item_id=first.id,
                dependent_item_id=second.id,
            )
        ],
        basis=[
            PlanVersionBasisCreate(
                entity_id=goal.entity_id,
                event_id=goal_event.id,
                role=PlanVersionBasisRole.GOAL,
            )
        ],
    )


async def present_and_approval_source(session, service, version, evidence, conversation):
    _, assistant_message, _ = await source_with_message(
        session,
        conversation=conversation,
        author=MessageAuthor.ASSISTANT,
    )
    presentation = await service.record_version_presentation(
        PlanVersionPresentationCreate(
            plan_version_id=version.id,
            message_id=assistant_message.id,
        ),
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    approval_source, _, _ = await source_with_message(
        session,
        conversation=conversation,
        offset_seconds=2,
    )
    return presentation, approval_source


@pytest.mark.asyncio
async def test_full_planning_lifecycle_strategy_progress_query_and_staleness(session) -> None:
    evidence, conversation, goal, goal_event, plan = await seed_plan(session)
    service = PlanningService(session)
    query = PlanningQueryService(session)
    assert plan.strategy_id is None
    assert await query.get_strategy(plan.entity_id) is None

    v1 = await service.create_candidate_version(
        candidate_command(plan.entity_id, goal, goal_event),
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    assert v1.version_number == 1
    assert v1.status == PlanVersionStatus.CANDIDATE
    inference = await session.get(Source, v1.created_source_id)
    assert inference.source_type == SourceType.ASSISTANT_INFERENCE
    dependency = await session.get(SourceDependency, (inference.id, evidence.id))
    assert dependency is not None

    _, approval1 = await present_and_approval_source(
        session, service, v1, inference, conversation
    )
    world_counts_before = {
        model: (await session.execute(select(func.count()).select_from(model))).scalar_one()
        for model in (Task, Project, Goal, Decision)
    }
    approved1 = await service.approve_plan_version(
        PlanVersionApprove(plan_version_id=v1.id),
        ActionContext(ActorType.USER, approval1),
    )
    assert approved1.status == PlanVersionStatus.APPROVED
    strategy = await query.get_strategy(plan.entity_id)
    assert strategy is not None
    strategy_id = strategy.entity_id
    assert strategy.approach == "First strategy"
    assert strategy.accepted_from_plan_version_id == v1.id
    assert world_counts_before == {
        model: (await session.execute(select(func.count()).select_from(model))).scalar_one()
        for model in (Task, Project, Goal, Decision)
    }
    progress = await query.get_item_progress(v1.id)
    assert [row.status for row in progress] == [
        PlanItemProgressStatus.NOT_STARTED,
        PlanItemProgressStatus.NOT_STARTED,
    ]
    assert (await query.get_next_action(plan.entity_id)).ordinal == 1

    event_count = (
        await session.execute(
            select(func.count()).select_from(Event).where(
                Event.event_type == EventType.PLAN_VERSION_APPROVED,
                Event.source_id == approval1.id,
            )
        )
    ).scalar_one()
    retry = await service.approve_plan_version(
        PlanVersionApprove(plan_version_id=v1.id),
        ActionContext(ActorType.USER, approval1),
    )
    assert retry.id == v1.id
    assert (
        await session.execute(
            select(func.count()).select_from(Event).where(
                Event.event_type == EventType.PLAN_VERSION_APPROVED,
                Event.source_id == approval1.id,
            )
        )
    ).scalar_one() == event_count

    v2 = await service.create_candidate_version(
        candidate_command(
            plan.entity_id,
            goal,
            goal_event,
            strategy="Refined strategy",
            based_on=v1.id,
        ),
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    assert v2.version_number == 2
    assert v1.status == PlanVersionStatus.APPROVED
    assert (await query.get_current_approved(plan.entity_id)).id == v1.id
    assert (await query.get_current_candidate(plan.entity_id)).id == v2.id
    _, approval2 = await present_and_approval_source(
        session,
        service,
        v2,
        await session.get(Source, v2.created_source_id),
        conversation,
    )
    await service.approve_plan_version(
        PlanVersionApprove(plan_version_id=v2.id),
        ActionContext(ActorType.USER, approval2),
    )
    assert v1.status == PlanVersionStatus.SUPERSEDED
    assert v2.status == PlanVersionStatus.APPROVED
    refined = await query.get_strategy(plan.entity_id)
    assert refined.entity_id == strategy_id
    assert refined.approach == "Refined strategy"
    assert refined.accepted_from_plan_version_id == v2.id
    with pytest.raises(DomainValidationError, match="current Candidate"):
        await service.approve_plan_version(
            PlanVersionApprove(plan_version_id=v1.id),
            ActionContext(ActorType.USER, approval1),
        )
    assert [version.version_number for version in await query.get_plan_history(plan.entity_id)] == [
        2,
        1,
    ]
    history = await query.get_bounded_history(plan.entity_id, limit=2)
    assert [version["version_number"] for version in history] == [2, 1]
    details = await query.get_version_details(v2.id)
    assert details["creation_evidence"][0]["text"] == "synthetic USER"
    comparison = await query.compare_plan_versions(v1.id, v2.id)
    assert comparison["strategy_changed"] is True

    current_progress = await query.get_item_progress(v2.id)
    first, second = current_progress
    await service.update_plan_item_progress(
        PlanItemProgressUpdate(
            plan_version_item_id=first.plan_version_item_id,
            status=PlanItemProgressStatus.DONE,
        ),
        ActionContext(ActorType.USER, approval2),
    )
    assert (await query.get_next_action(plan.entity_id)).id == second.plan_version_item_id
    await service.update_plan_item_progress(
        PlanItemProgressUpdate(
            plan_version_item_id=second.plan_version_item_id,
            status=PlanItemProgressStatus.BLOCKED,
        ),
        ActionContext(ActorType.USER, approval2),
    )
    assert await query.get_next_action(plan.entity_id) is None

    fresh = await query.assess_plan_staleness(v2.id)
    assert fresh.is_stale is False
    fresh_details = await query.get_staleness_details(v2.id)
    assert fresh_details == {"is_basis_stale": False, "changed_basis": []}
    await WorldStateService(session).create_goal(
        GoalCreate(title="Irrelevant goal changed later"),
        ActionContext(ActorType.USER, approval2),
    )
    assert (await query.assess_plan_staleness(v2.id)).is_stale is False
    await WorldStateService(session).update_goal(
        GoalUpdate(entity_id=goal.entity_id, description="Canonical goal changed"),
        ActionContext(ActorType.USER, approval2),
    )
    stale = await query.assess_plan_staleness(v2.id)
    assert stale.is_stale is True
    assert stale.changed_basis[0].recorded_event_id == goal_event.id
    stale_details = await query.get_staleness_details(v2.id)
    assert stale_details == {
        "is_basis_stale": True,
        "changed_basis": [{"role": "GOAL", "label": "Synthetic goal"}],
    }
    v3 = await service.create_candidate_version(
        candidate_command(plan.entity_id, goal, goal_event, based_on=v1.id),
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    await service.reject_candidate_version(
        PlanCandidateReject(plan_version_id=v3.id),
        ActionContext(ActorType.USER, approval2),
    )
    assert (await query.get_current_approved(plan.entity_id)).id == v2.id
    assert (await query.get_strategy(plan.entity_id)).accepted_from_plan_version_id == v2.id
    rejected_details = await query.get_version_details(v3.id)
    assert rejected_details["status"] == "REJECTED"
    assert rejected_details["rejection_evidence"][0]["text"] == "synthetic USER"
    bounded_context = await query.render_for_agent()
    assert str(v2.id) in bounded_context
    assert str(v1.id) not in bounded_context
    assert str(v3.id) not in bounded_context
    assert "event_id" not in bounded_context
    assert '"progress": "DONE"' in bounded_context
    assert '"is_basis_stale": true' in bounded_context


@pytest.mark.asyncio
async def test_new_candidate_supersedes_only_candidate_and_allows_historical_basis(session) -> None:
    evidence, _, goal, goal_event, plan = await seed_plan(session)
    service = PlanningService(session)
    first = await service.create_candidate_version(
        candidate_command(plan.entity_id, goal, goal_event),
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    second = await service.create_candidate_version(
        candidate_command(plan.entity_id, goal, goal_event, based_on=first.id),
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    assert first.status == PlanVersionStatus.SUPERSEDED
    assert second.status == PlanVersionStatus.CANDIDATE
    assert second.based_on_version_id == first.id


@pytest.mark.asyncio
async def test_plan_goal_validation_and_dependency_cycle(session) -> None:
    evidence, _, goal, goal_event, plan = await seed_plan(session)
    task = await WorldStateService(session).create_task(
        TaskCreate(title="Not a Goal"), ActionContext(ActorType.USER, evidence)
    )
    with pytest.raises(EntityNotFoundError):
        await PlanningService(session).create_plan(
            PlanCreate(
                title="Invalid",
                goals=[PlanGoalLinkCreate(goal_id=task.entity_id)],
            ),
            ActionContext(ActorType.USER, evidence),
        )
    supporting = await WorldStateService(session).create_goal(
        GoalCreate(title="Supporting goal"), ActionContext(ActorType.USER, evidence)
    )
    multi_goal = await PlanningService(session).create_plan(
        PlanCreate(
            title="Multi-goal Plan",
            goals=[
                PlanGoalLinkCreate(goal_id=goal.entity_id, role=PlanGoalRole.PRIMARY),
                PlanGoalLinkCreate(
                    goal_id=supporting.entity_id,
                    role=PlanGoalRole.SUPPORTING,
                ),
            ],
        ),
        ActionContext(ActorType.USER, evidence),
    )
    assert multi_goal.strategy_id is None
    first = PlanVersionItemCreate(ordinal=1, title="one")
    second = PlanVersionItemCreate(ordinal=2, title="two")
    cyclic = PlanCandidateCreate(
        plan_id=plan.entity_id,
        summary="cyclic",
        proposed_strategy_snapshot="strategy",
        items=[first, second],
        dependencies=[
            PlanVersionItemDependencyCreate(
                prerequisite_item_id=first.id, dependent_item_id=second.id
            ),
            PlanVersionItemDependencyCreate(
                prerequisite_item_id=second.id, dependent_item_id=first.id
            ),
        ],
        basis=[
            PlanVersionBasisCreate(
                entity_id=goal.entity_id,
                event_id=goal_event.id,
                role=PlanVersionBasisRole.GOAL,
            )
        ],
    )
    with pytest.raises(DomainValidationError, match="cycle"):
        await PlanningService(session).create_candidate_version(
            cyclic, ActionContext(ActorType.ASSISTANT, evidence)
        )


@pytest.mark.asyncio
async def test_presentation_reject_and_authority_validation(session) -> None:
    evidence, conversation, goal, goal_event, plan = await seed_plan(session)
    service = PlanningService(session)
    version = await service.create_candidate_version(
        candidate_command(plan.entity_id, goal, goal_event),
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    with pytest.raises(DomainValidationError, match="assistant Message"):
        await service.record_version_presentation(
            PlanVersionPresentationCreate(
                plan_version_id=version.id,
                message_id=(await session.get(Source, evidence.id)).message_id,
            ),
            ActionContext(ActorType.ASSISTANT, evidence),
        )
    with pytest.raises(DomainValidationError, match="presented"):
        await service.approve_plan_version(
            PlanVersionApprove(plan_version_id=version.id),
            ActionContext(ActorType.USER, evidence),
        )
    system_source = Source(source_type=SourceType.SYSTEM, reason_summary="not user authority")
    session.add(system_source)
    await session.flush()
    with pytest.raises(DomainValidationError, match="USER_MESSAGE"):
        await service.approve_plan_version(
            PlanVersionApprove(plan_version_id=version.id),
            ActionContext(ActorType.SYSTEM, system_source),
        )
    with pytest.raises(DomainValidationError, match="USER_MESSAGE"):
        await service.reject_candidate_version(
            PlanCandidateReject(plan_version_id=version.id),
            ActionContext(ActorType.SYSTEM, system_source),
        )
    rejected = await service.reject_candidate_version(
        PlanCandidateReject(plan_version_id=version.id),
        ActionContext(ActorType.USER, evidence),
    )
    assert rejected.status == PlanVersionStatus.REJECTED
    repeated = await service.reject_candidate_version(
        PlanCandidateReject(plan_version_id=version.id),
        ActionContext(ActorType.USER, evidence),
    )
    assert repeated.id == version.id
    assert plan.strategy_id is None
    assert await PlanningQueryService(session).get_current_approved(plan.entity_id) is None


@pytest.mark.asyncio
async def test_presentation_is_idempotent_and_progress_never_mutates_linked_task(session) -> None:
    evidence, conversation, goal, goal_event, plan = await seed_plan(session)
    task = await WorldStateService(session).create_task(
        TaskCreate(title="Canonical task"), ActionContext(ActorType.USER, evidence)
    )
    command = candidate_command(plan.entity_id, goal, goal_event)
    command.items[0] = command.items[0].model_copy(update={"linked_task_id": task.entity_id})
    service = PlanningService(session)
    version = await service.create_candidate_version(
        command, ActionContext(ActorType.ASSISTANT, evidence)
    )
    with pytest.raises(DomainValidationError, match="Approved"):
        await service.update_plan_item_progress(
            PlanItemProgressUpdate(
                plan_version_item_id=command.items[0].id,
                status=PlanItemProgressStatus.IN_PROGRESS,
            ),
            ActionContext(ActorType.USER, evidence),
        )
    inference = await session.get(Source, version.created_source_id)
    presentation, approval = await present_and_approval_source(
        session, service, version, inference, conversation
    )
    repeated = await service.record_version_presentation(
        PlanVersionPresentationCreate(
            plan_version_id=version.id,
            message_id=presentation.message_id,
        ),
        ActionContext(ActorType.ASSISTANT, inference),
    )
    assert repeated.id == presentation.id
    await service.approve_plan_version(
        PlanVersionApprove(plan_version_id=version.id),
        ActionContext(ActorType.USER, approval),
    )
    version_count = (
        await session.execute(select(func.count()).select_from(PlanVersion))
    ).scalar_one()
    await service.update_plan_item_progress(
        PlanItemProgressUpdate(
            plan_version_item_id=command.items[0].id,
            status=PlanItemProgressStatus.IN_PROGRESS,
        ),
        ActionContext(ActorType.USER, approval),
    )
    event_count = (
        await session.execute(
            select(func.count()).select_from(Event).where(
                Event.event_type == EventType.PLAN_ITEM_PROGRESS_UPDATED
            )
        )
    ).scalar_one()
    await service.update_plan_item_progress(
        PlanItemProgressUpdate(
            plan_version_item_id=command.items[0].id,
            status=PlanItemProgressStatus.IN_PROGRESS,
        ),
        ActionContext(ActorType.USER, approval),
    )
    assert (
        await session.execute(
            select(func.count()).select_from(Event).where(
                Event.event_type == EventType.PLAN_ITEM_PROGRESS_UPDATED
            )
        )
    ).scalar_one() == event_count
    with pytest.raises(DomainValidationError, match="USER_MESSAGE"):
        await service.update_plan_item_progress(
            PlanItemProgressUpdate(
                plan_version_item_id=command.items[0].id,
                status=PlanItemProgressStatus.DONE,
            ),
            ActionContext(ActorType.ASSISTANT, inference),
        )
    assert (
        await PlanningQueryService(session).get_next_action(plan.entity_id)
    ).id == command.items[0].id
    persisted_task = await session.get(Task, task.entity_id)
    assert persisted_task.title == "Canonical task"
    assert (
        await session.execute(select(func.count()).select_from(PlanVersion))
    ).scalar_one() == version_count


@pytest.mark.asyncio
async def test_approval_failure_rolls_back_version_strategy_and_progress(session) -> None:
    evidence, conversation, goal, goal_event, plan = await seed_plan(session)
    service = PlanningService(session)
    version = await service.create_candidate_version(
        candidate_command(plan.entity_id, goal, goal_event),
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    _, approval = await present_and_approval_source(
        session,
        service,
        version,
        await session.get(Source, version.created_source_id),
        conversation,
    )

    async def fail_strategy(**kwargs):
        raise RuntimeError("synthetic strategy failure")

    service._accept_strategy = fail_strategy
    with pytest.raises(RuntimeError, match="synthetic"):
        await service.approve_plan_version(
            PlanVersionApprove(plan_version_id=version.id),
            ActionContext(ActorType.USER, approval),
        )
    persisted = await session.get(PlanVersion, version.id)
    assert persisted.status == PlanVersionStatus.CANDIDATE
    assert (await session.get(Entity, plan.entity_id)) is not None
    assert (await PlanningQueryService(session).get_plan(plan.entity_id)).strategy_id is None
    assert (
        await session.execute(select(func.count()).select_from(Strategy))
    ).scalar_one() == 0
    assert (
        await session.execute(select(func.count()).select_from(PlanItemProgress))
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_structured_version_comparison_detects_obvious_changes(session) -> None:
    evidence, _, goal, goal_event, plan = await seed_plan(session)
    service = PlanningService(session)
    first_command = candidate_command(plan.entity_id, goal, goal_event)
    first = await service.create_candidate_version(
        first_command,
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    retained = PlanVersionItemCreate(
        ordinal=2,
        title="First action",
        description="More detail",
        estimated_duration_minutes=30,
    )
    added = PlanVersionItemCreate(ordinal=1, title="Portfolio action")
    second_command = PlanCandidateCreate(
        plan_id=plan.entity_id,
        summary="Changed plan",
        proposed_strategy_snapshot="Changed strategy",
        based_on_version_id=first.id,
        items=[added, retained],
        dependencies=[
            PlanVersionItemDependencyCreate(
                prerequisite_item_id=added.id,
                dependent_item_id=retained.id,
            )
        ],
    )
    second = await service.create_candidate_version(
        second_command,
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    comparison = await PlanningQueryService(session).compare_plan_versions(
        first.id,
        second.id,
    )
    assert comparison["strategy_changed"] is True
    assert comparison["added_items"] == ["Portfolio action"]
    assert comparison["removed_items"] == ["Second action"]
    assert comparison["order_changes"] == [
        {"title": "First action", "before": 1, "after": 2}
    ]
    assert comparison["changed_items"] == [
        {
            "title": "First action",
            "fields": {
                "description": {"before": None, "after": "More detail"},
                "estimated_duration_minutes": {"before": None, "after": 30},
            },
        }
    ]
    assert comparison["dependencies_added"] == [
        ("Portfolio action", "First action")
    ]


@pytest.mark.asyncio
async def test_current_planning_context_bounds_large_version_items(session) -> None:
    evidence, _, _, _, plan = await seed_plan(session)
    command = PlanCandidateCreate(
        plan_id=plan.entity_id,
        summary="Large bounded plan",
        proposed_strategy_snapshot="Bound the prompt",
        items=[
            PlanVersionItemCreate(ordinal=ordinal, title=f"Item {ordinal}")
            for ordinal in range(1, 31)
        ],
    )
    await PlanningService(session).create_candidate_version(
        command,
        ActionContext(ActorType.ASSISTANT, evidence),
    )
    context = json.loads(
        await PlanningQueryService(session).render_for_agent(max_items_per_version=7)
    )
    assert len(context["plans"][0]["current_candidate"]["items"]) == 7
