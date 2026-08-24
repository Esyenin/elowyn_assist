from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from elowyn.db.models import (
    Entity,
    Event,
    Goal,
    Message,
    Operation,
    Plan,
    PlanGoalLink,
    PlanItemProgress,
    PlanVersion,
    PlanVersionBasis,
    PlanVersionItem,
    PlanVersionItemDependency,
    PlanVersionPresentation,
    Source,
    Strategy,
    Task,
)
from elowyn.domain.enums import (
    ActorType,
    EntityType,
    EventType,
    MessageAuthor,
    PlanItemProgressStatus,
    PlanVersionBasisRole,
    PlanVersionStatus,
    SourceType,
)
from elowyn.domain.errors import DomainValidationError, EntityNotFoundError
from elowyn.domain.planning_commands import (
    PlanCandidateCreate,
    PlanCandidateReject,
    PlanCreate,
    PlanItemProgressUpdate,
    PlanVersionApprove,
    PlanVersionPresentationCreate,
)
from elowyn.services.domain_mutation import (
    ActionContext,
    DomainMutationService,
    assistant_inference_source,
    atomic_domain_action,
    change,
)


class PlanningService(DomainMutationService):
    """Validated write boundary for Planning v0.3 canonical state."""

    async def _active_typed_entity(self, entity_id: uuid.UUID, expected: EntityType) -> Entity:
        entity = await self.session.get(Entity, entity_id)
        if (
            entity is None
            or entity.entity_type != expected
            or entity.removed_at is not None
            or entity.superseded_by_entity_id is not None
        ):
            raise EntityNotFoundError(f"active {expected.value} {entity_id} was not found")
        return entity

    async def _plan(self, plan_id: uuid.UUID, *, for_update: bool = False) -> Plan:
        await self._active_typed_entity(plan_id, EntityType.PLAN)
        statement = select(Plan).where(Plan.entity_id == plan_id)
        if for_update and self._uses_postgresql():
            statement = statement.with_for_update().execution_options(populate_existing=True)
        plan = (await self.session.execute(statement)).scalar_one_or_none()
        if plan is None:
            raise EntityNotFoundError(f"Plan {plan_id} was not found")
        return plan

    async def _locked_version(self, version_id: uuid.UUID) -> tuple[Plan, PlanVersion]:
        version = await self.session.get(PlanVersion, version_id)
        if version is None:
            raise EntityNotFoundError(f"PlanVersion {version_id} was not found")
        plan = await self._plan(version.plan_id, for_update=True)
        statement = select(PlanVersion).where(PlanVersion.id == version_id)
        if self._uses_postgresql():
            statement = statement.with_for_update().execution_options(populate_existing=True)
        locked = (await self.session.execute(statement)).scalar_one()
        return plan, locked

    async def _persisted_source(self, source: Source | None) -> Source:
        if source is None or source.id is None:
            raise DomainValidationError("planning mutation requires a persisted Source")
        persisted = await self.session.get(Source, source.id)
        if persisted is None:
            raise DomainValidationError("planning mutation Source does not exist")
        return persisted

    async def _user_message_source(self, ctx: ActionContext) -> tuple[Source, Message]:
        source = await self._persisted_source(ctx.source)
        if source.source_type != SourceType.USER_MESSAGE or source.message_id is None:
            raise DomainValidationError("operation requires a USER_MESSAGE Source")
        message = await self.session.get(Message, source.message_id)
        if message is None or message.author != MessageAuthor.USER:
            raise DomainValidationError("approval authority must resolve to a user Message")
        return source, message

    @staticmethod
    def _assert_acyclic(item_ids: set[uuid.UUID], edges: list[tuple[uuid.UUID, uuid.UUID]]) -> None:
        outgoing: dict[uuid.UUID, set[uuid.UUID]] = {item_id: set() for item_id in item_ids}
        indegree = dict.fromkeys(item_ids, 0)
        for prerequisite, dependent in edges:
            outgoing[prerequisite].add(dependent)
            indegree[dependent] += 1
        frontier = sorted(item_id for item_id, count in indegree.items() if count == 0)
        visited = 0
        while frontier:
            current = frontier.pop(0)
            visited += 1
            for dependent in sorted(outgoing[current]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    frontier.append(dependent)
                    frontier.sort()
        if visited != len(item_ids):
            raise DomainValidationError("PlanVersion item dependencies cannot contain a cycle")

    @atomic_domain_action
    async def create_plan(self, command: PlanCreate, ctx: ActionContext) -> Plan:
        source = await self._persisted_source(ctx.source)
        goal_ids = [item.goal_id for item in command.goals]
        await self._lock_entities(goal_ids)
        for goal_id in goal_ids:
            await self._active_typed_entity(goal_id, EntityType.GOAL)
            if await self.session.get(Goal, goal_id) is None:
                raise EntityNotFoundError(f"Goal {goal_id} was not found")

        operation = await self._operation(ctx)
        entity = Entity(entity_type=EntityType.PLAN)
        self.session.add(entity)
        await self.session.flush()
        plan = Plan(entity_id=entity.id, title=command.title, description=command.description)
        self.session.add(plan)
        await self.session.flush()
        await self._append_event(
            operation=operation,
            event_type=EventType.PLAN_CREATED,
            entity_id=plan.entity_id,
            source=source,
            changes=[change("created", None, command.model_dump(mode="json"))],
        )
        for goal in command.goals:
            self.session.add(
                PlanGoalLink(
                    plan_id=plan.entity_id,
                    goal_id=goal.goal_id,
                    role=goal.role,
                    source_id=source.id,
                )
            )
            await self._append_event(
                operation=operation,
                event_type=EventType.PLAN_GOAL_LINKED,
                entity_id=plan.entity_id,
                source=source,
                changes=[change("goal_link", None, goal.model_dump(mode="json"))],
            )
        await self.session.flush()
        return plan

    @atomic_domain_action
    async def create_candidate_version(
        self, command: PlanCandidateCreate, ctx: ActionContext
    ) -> PlanVersion:
        plan = await self._plan(command.plan_id, for_update=True)
        if command.based_on_version_id is not None:
            based_on = await self.session.get(PlanVersion, command.based_on_version_id)
            if based_on is None or based_on.plan_id != plan.entity_id:
                raise DomainValidationError("based_on_version_id must belong to this Plan")

        for item in command.items:
            if item.linked_task_id is not None:
                await self._active_typed_entity(item.linked_task_id, EntityType.TASK)
                if await self.session.get(Task, item.linked_task_id) is None:
                    raise EntityNotFoundError(f"Task {item.linked_task_id} was not found")

        expected_types = {
            PlanVersionBasisRole.GOAL: EntityType.GOAL,
            PlanVersionBasisRole.TASK: EntityType.TASK,
            PlanVersionBasisRole.PROJECT: EntityType.PROJECT,
            PlanVersionBasisRole.DECISION: EntityType.DECISION,
            PlanVersionBasisRole.STRATEGY: EntityType.STRATEGY,
        }
        for basis in command.basis:
            await self._active_typed_entity(basis.entity_id, expected_types[basis.role])
            event = await self.session.get(Event, basis.event_id)
            if event is None or event.entity_id != basis.entity_id:
                raise DomainValidationError("basis Event must belong to the stated Entity")

        edges = [
            (item.prerequisite_item_id, item.dependent_item_id)
            for item in command.dependencies
        ]
        self._assert_acyclic({item.id for item in command.items}, edges)

        evidence_ids = list(dict.fromkeys(command.evidence_source_ids))
        if ctx.source is not None and ctx.source.id not in evidence_ids:
            evidence_ids.append(ctx.source.id)
        evidence: list[Source] = []
        for source_id in evidence_ids:
            source = await self.session.get(Source, source_id)
            if source is None:
                raise DomainValidationError(f"evidence Source {source_id} does not exist")
            evidence.append(source)
        inference = await assistant_inference_source(
            self.session,
            confidence=command.inference_confidence,
            reason_summary=command.inference_reason_summary,
            evidence_sources=evidence,
        )
        effective_ctx = ActionContext(
            actor_type=ActorType.ASSISTANT,
            source=inference,
            description=ctx.description or "Elowyn candidate PlanVersion synthesis",
            operation_id=None,
        )
        operation = await self._operation(effective_ctx)

        current_statement = select(PlanVersion).where(
            PlanVersion.plan_id == plan.entity_id,
            PlanVersion.status == PlanVersionStatus.CANDIDATE,
        )
        if self._uses_postgresql():
            current_statement = current_statement.with_for_update().execution_options(
                populate_existing=True
            )
        current = (await self.session.execute(current_statement)).scalar_one_or_none()
        if current is not None:
            current.status = PlanVersionStatus.SUPERSEDED
            await self._append_event(
                operation=operation,
                event_type=EventType.PLAN_VERSION_SUPERSEDED,
                entity_id=plan.entity_id,
                source=inference,
                changes=[change("version_status", "CANDIDATE", "SUPERSEDED")],
            )
            await self.session.flush()

        version_number = (
            await self.session.execute(
                select(func.coalesce(func.max(PlanVersion.version_number), 0)).where(
                    PlanVersion.plan_id == plan.entity_id
                )
            )
        ).scalar_one() + 1
        version = PlanVersion(
            plan_id=plan.entity_id,
            version_number=version_number,
            status=PlanVersionStatus.CANDIDATE,
            summary=command.summary,
            rationale=command.rationale,
            proposed_strategy_snapshot=command.proposed_strategy_snapshot,
            strategy_rationale_snapshot=command.strategy_rationale_snapshot,
            based_on_version_id=command.based_on_version_id,
            created_source_id=inference.id,
        )
        self.session.add(version)
        await self.session.flush()
        for item in command.items:
            self.session.add(
                PlanVersionItem(
                    id=item.id,
                    plan_version_id=version.id,
                    ordinal=item.ordinal,
                    title=item.title,
                    description=item.description,
                    expected_outcome=item.expected_outcome,
                    deadline_at=item.deadline_at,
                    estimated_duration_minutes=item.estimated_duration_minutes,
                    linked_task_id=item.linked_task_id,
                )
            )
        await self.session.flush()
        for dependency in command.dependencies:
            self.session.add(
                PlanVersionItemDependency(
                    plan_version_id=version.id,
                    prerequisite_item_id=dependency.prerequisite_item_id,
                    dependent_item_id=dependency.dependent_item_id,
                )
            )
        for basis in command.basis:
            self.session.add(
                PlanVersionBasis(
                    plan_version_id=version.id,
                    entity_id=basis.entity_id,
                    event_id=basis.event_id,
                    role=basis.role,
                )
            )
        await self._append_event(
            operation=operation,
            event_type=EventType.PLAN_VERSION_CREATED,
            entity_id=plan.entity_id,
            source=inference,
            changes=[
                change(
                    "plan_version",
                    None,
                    {"id": version.id, "version_number": version.version_number},
                )
            ],
        )
        await self.session.flush()
        return version

    @atomic_domain_action
    async def record_version_presentation(
        self, command: PlanVersionPresentationCreate, ctx: ActionContext
    ) -> PlanVersionPresentation:
        plan, version = await self._locked_version(command.plan_version_id)
        message = await self.session.get(Message, command.message_id)
        if message is None or message.author != MessageAuthor.ASSISTANT:
            raise DomainValidationError("PlanVersion presentation requires an assistant Message")
        existing = (
            await self.session.execute(
                select(PlanVersionPresentation).where(
                    PlanVersionPresentation.plan_version_id == version.id,
                    PlanVersionPresentation.message_id == message.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        operation = await self._operation(ctx)
        presentation = PlanVersionPresentation(
            plan_version_id=version.id,
            message_id=message.id,
        )
        self.session.add(presentation)
        await self.session.flush()
        await self._append_event(
            operation=operation,
            event_type=EventType.PLAN_VERSION_PRESENTED,
            entity_id=plan.entity_id,
            source=ctx.source,
            changes=[
                change(
                    "presentation",
                    None,
                    {"version_id": version.id, "message_id": message.id},
                )
            ],
        )
        return presentation

    @atomic_domain_action
    async def reject_candidate_version(
        self, command: PlanCandidateReject, ctx: ActionContext
    ) -> PlanVersion:
        source, _ = await self._user_message_source(ctx)
        plan, version = await self._locked_version(command.plan_version_id)
        if version.status == PlanVersionStatus.REJECTED:
            events = (
                await self.session.execute(
                    select(Event).where(
                        Event.entity_id == plan.entity_id,
                        Event.event_type == EventType.PLAN_VERSION_REJECTED,
                        Event.source_id == source.id,
                    )
                )
            ).scalars()
            if any(str(version.id) in str(event.changes) for event in events):
                return version
        if version.status != PlanVersionStatus.CANDIDATE:
            raise DomainValidationError("only a current Candidate can be rejected")
        operation = await self._operation(ctx)
        version.status = PlanVersionStatus.REJECTED
        await self._append_event(
            operation=operation,
            event_type=EventType.PLAN_VERSION_REJECTED,
            entity_id=plan.entity_id,
            source=source,
            changes=[
                change("version_status", "CANDIDATE", "REJECTED"),
                change("version_id", None, version.id),
            ],
        )
        await self.session.flush()
        return version

    async def _accept_strategy(
        self,
        *,
        plan: Plan,
        version: PlanVersion,
        source: Source,
        operation: Operation,
        accepted_at: datetime,
    ) -> Strategy:
        strategy: Strategy | None = None
        created = plan.strategy_id is None
        if plan.strategy_id is not None:
            statement = select(Strategy).where(Strategy.entity_id == plan.strategy_id)
            if self._uses_postgresql():
                statement = statement.with_for_update().execution_options(populate_existing=True)
            strategy = (await self.session.execute(statement)).scalar_one_or_none()
            if strategy is None:
                raise DomainValidationError("Plan references a missing Strategy")
        else:
            entity = Entity(entity_type=EntityType.STRATEGY)
            self.session.add(entity)
            await self.session.flush()
            strategy = Strategy(
                entity_id=entity.id,
                approach=version.proposed_strategy_snapshot,
                rationale=version.strategy_rationale_snapshot,
                accepted_from_plan_version_id=version.id,
                accepted_source_id=source.id,
                accepted_at=accepted_at,
            )
            self.session.add(strategy)
        strategy.approach = version.proposed_strategy_snapshot
        strategy.rationale = version.strategy_rationale_snapshot
        strategy.accepted_from_plan_version_id = version.id
        strategy.accepted_source_id = source.id
        strategy.accepted_at = accepted_at
        await self.session.flush()
        if created:
            plan.strategy_id = strategy.entity_id
            await self.session.flush()
        if created:
            await self._append_event(
                operation=operation,
                event_type=EventType.STRATEGY_CREATED,
                entity_id=strategy.entity_id,
                source=source,
                changes=[change("created_from_version", None, version.id)],
            )
        await self._append_event(
            operation=operation,
            event_type=EventType.STRATEGY_ACCEPTED,
            entity_id=strategy.entity_id,
            source=source,
            changes=[change("accepted_version", None, version.id)],
        )
        return strategy

    @atomic_domain_action
    async def approve_plan_version(
        self, command: PlanVersionApprove, ctx: ActionContext
    ) -> PlanVersion:
        source, approval_message = await self._user_message_source(ctx)
        plan, version = await self._locked_version(command.plan_version_id)
        if (
            version.status == PlanVersionStatus.APPROVED
            and version.approval_source_id == source.id
        ):
            return version
        if version.status != PlanVersionStatus.CANDIDATE:
            raise DomainValidationError("only the current Candidate can be approved")

        presentations = (
            await self.session.execute(
                select(PlanVersionPresentation, Message)
                .join(Message, Message.id == PlanVersionPresentation.message_id)
                .where(
                    PlanVersionPresentation.plan_version_id == version.id,
                    Message.author == MessageAuthor.ASSISTANT,
                    Message.conversation_id == approval_message.conversation_id,
                    PlanVersionPresentation.presented_at <= approval_message.sent_at,
                )
            )
        ).all()
        if not presentations:
            raise DomainValidationError(
                "Candidate must be presented in this Conversation before approval"
            )

        operation = await self._operation(ctx)
        approved_statement = select(PlanVersion).where(
            PlanVersion.plan_id == plan.entity_id,
            PlanVersion.status == PlanVersionStatus.APPROVED,
            PlanVersion.id != version.id,
        )
        if self._uses_postgresql():
            approved_statement = approved_statement.with_for_update().execution_options(
                populate_existing=True
            )
        old_approved = (await self.session.execute(approved_statement)).scalar_one_or_none()
        if old_approved is not None:
            old_approved.status = PlanVersionStatus.SUPERSEDED
            await self._append_event(
                operation=operation,
                event_type=EventType.PLAN_VERSION_SUPERSEDED,
                entity_id=plan.entity_id,
                source=source,
                changes=[change("version_id", old_approved.id, version.id)],
            )
        accepted_at = datetime.now(UTC)
        version.status = PlanVersionStatus.APPROVED
        version.approval_source_id = source.id
        version.approved_at = accepted_at
        await self.session.flush()
        await self._accept_strategy(
            plan=plan,
            version=version,
            source=source,
            operation=operation,
            accepted_at=accepted_at,
        )
        items = (
            await self.session.execute(
                select(PlanVersionItem).where(PlanVersionItem.plan_version_id == version.id)
            )
        ).scalars()
        for item in items:
            self.session.add(
                PlanItemProgress(
                    plan_version_item_id=item.id,
                    status=PlanItemProgressStatus.NOT_STARTED,
                    source_id=source.id,
                )
            )
        await self._append_event(
            operation=operation,
            event_type=EventType.PLAN_VERSION_APPROVED,
            entity_id=plan.entity_id,
            source=source,
            changes=[
                change("version_status", "CANDIDATE", "APPROVED"),
                change("version_id", None, version.id),
            ],
        )
        await self.session.flush()
        return version

    @atomic_domain_action
    async def update_plan_item_progress(
        self, command: PlanItemProgressUpdate, ctx: ActionContext
    ) -> PlanItemProgress:
        source, _ = await self._user_message_source(ctx)
        item = await self.session.get(PlanVersionItem, command.plan_version_item_id)
        if item is None:
            raise EntityNotFoundError(
                f"PlanVersionItem {command.plan_version_item_id} was not found"
            )
        plan, version = await self._locked_version(item.plan_version_id)
        if version.status != PlanVersionStatus.APPROVED:
            raise DomainValidationError("progress can change only for the current Approved version")
        progress = await self.session.get(PlanItemProgress, item.id)
        if progress is None:
            raise DomainValidationError("Approved PlanVersion item has no initialized progress")
        if progress.status == command.status and progress.source_id == source.id:
            return progress
        old_status = progress.status
        progress.status = command.status
        progress.note = command.note
        progress.source_id = source.id
        progress.updated_at = datetime.now(UTC)
        operation = await self._operation(ctx)
        await self._append_event(
            operation=operation,
            event_type=EventType.PLAN_ITEM_PROGRESS_UPDATED,
            entity_id=plan.entity_id,
            source=source,
            changes=[
                change("progress_status", old_status, command.status),
                change("item_id", None, item.id),
            ],
        )
        await self.session.flush()
        return progress
