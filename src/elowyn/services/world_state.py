from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import wraps
from typing import Any

from sqlalchemy import select, text
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import (
    Decision,
    DecisionAlternative,
    Entity,
    EntityRelation,
    Event,
    Goal,
    Operation,
    Project,
    ProjectGoalLink,
    Source,
    SourceDependency,
    SuccessCriterion,
    Task,
    TaskDependency,
    TaskGoalLink,
)
from elowyn.domain.commands import (
    DecisionCreate,
    DecisionRevoke,
    EntityRelationCreate,
    EntityRelationInference,
    GoalAssessment,
    GoalCreate,
    GoalUpdate,
    ProjectAssessment,
    ProjectCreate,
    ProjectGoalLinkCreate,
    ProjectSummaryCacheUpdate,
    ProjectUpdate,
    SuccessCriterionAssessment,
    SuccessCriterionUpdate,
    TaskAssessment,
    TaskCreate,
    TaskDependencyCreate,
    TaskGoalLinkCreate,
    TaskUpdate,
)
from elowyn.domain.enums import (
    ActorType,
    DeadlineType,
    DecisionStatus,
    EntityType,
    EventType,
    GoalStatus,
    ProjectStatus,
    SourceType,
    SuccessCriterionStatus,
    TaskStatus,
)
from elowyn.domain.errors import DomainValidationError, EntityNotFoundError


def atomic_domain_action(method):
    """Rollback a failed tool call to a SAVEPOINT without discarding the whole user turn."""

    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        exclusive = method.__name__ == "undo_last_change" and kwargs.get("entity_id") is None
        await self._lock_world_state(exclusive=exclusive)
        async with self.session.begin_nested():
            return await method(self, *args, **kwargs)

    return wrapped


@dataclass(frozen=True)
class ActionContext:
    actor_type: ActorType
    source: Source | None = None
    description: str | None = None
    operation_id: uuid.UUID | None = None


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, uuid.UUID)):
        return str(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _change(field: str, old: Any, new: Any) -> dict[str, Any]:
    return {"field": field, "old": _json_value(old), "new": _json_value(new)}


class WorldStateService:
    """Validated write boundary between the LLM/tool layer and persistent state."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._last_event_at: datetime | None = None

    def _uses_postgresql(self) -> bool:
        get_bind = getattr(self.session, "get_bind", None)
        if get_bind is not None:
            return get_bind().dialect.name == "postgresql"
        sync_session = getattr(self.session, "sync", None)
        return sync_session is not None and sync_session.get_bind().dialect.name == "postgresql"

    async def _lock_world_state(self, *, exclusive: bool) -> None:
        """Coordinate global undo with concurrent domain actions for this transaction."""

        if not self._uses_postgresql():
            return
        function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        await self.session.execute(text(f"SELECT {function}(5044031582654955025)"))

    async def _lock_entities(self, entity_ids: list[uuid.UUID]) -> None:
        """Lock identities in UUID order so concurrent multi-row actions cannot deadlock."""

        ordered_ids = sorted(set(entity_ids))
        if not ordered_ids or not self._uses_postgresql():
            return
        with self.session.no_autoflush:
            await self.session.execute(
                select(Entity)
                .where(Entity.id.in_(ordered_ids))
                .order_by(Entity.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )

    async def _lock_typed_graph(
        self,
        model: type[Task] | type[Project] | type[Goal],
        entity_type: EntityType,
    ) -> None:
        """Serialize graph mutations while locking rows in a stable global UUID order."""

        if not self._uses_postgresql():
            return
        with self.session.no_autoflush:
            await self.session.execute(
                select(Entity)
                .where(Entity.entity_type == entity_type)
                .order_by(Entity.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            key = model.entity_id
            await self.session.execute(
                select(model)
                .order_by(key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )

    async def _operation(self, ctx: ActionContext) -> Operation:
        if ctx.operation_id is not None:
            existing = await self.session.get(Operation, ctx.operation_id)
            if existing is not None:
                return existing

        op = Operation(
            id=ctx.operation_id if ctx.operation_id is not None else uuid.uuid4(),
            actor_type=ctx.actor_type,
            source_id=ctx.source.id if ctx.source else None,
            description=ctx.description,
        )
        self.session.add(op)
        await self.session.flush()
        return op

    async def _entity(self, entity_type: EntityType) -> Entity:
        entity = Entity(entity_type=entity_type)
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def _event(
        self,
        *,
        operation: Operation,
        event_type: EventType,
        entity_id: uuid.UUID | None,
        source: Source | None,
        changes: list[dict[str, Any]],
        reverses_event_id: uuid.UUID | None = None,
    ) -> Event:
        # current_summary is a derived cache, never authoritative state. Any canonical
        # domain event conservatively invalidates project summaries in v0.1.
        await self.session.execute(
            sqlalchemy_update(Project).values(
                current_summary=None,
                current_summary_updated_at=None,
            )
        )
        created_at = datetime.now(UTC)
        if self._last_event_at is not None and created_at <= self._last_event_at:
            created_at = self._last_event_at + timedelta(microseconds=1)
        self._last_event_at = created_at
        event = Event(
            operation_id=operation.id,
            event_type=event_type,
            entity_id=entity_id,
            source_id=source.id if source else None,
            reverses_event_id=reverses_event_id,
            changes=changes,
            created_at=created_at,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def _active_entity(
        self,
        entity_id: uuid.UUID,
        expected_type: EntityType | None = None,
        *,
        for_update: bool = False,
    ) -> Entity:
        if for_update and self._uses_postgresql():
            entity = (
                await self.session.execute(
                    select(Entity)
                    .where(Entity.id == entity_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        else:
            entity = await self.session.get(Entity, entity_id)
        if (
            entity is None
            or entity.removed_at is not None
            or entity.superseded_by_entity_id is not None
            or (expected_type is not None and entity.entity_type != expected_type)
        ):
            label = expected_type.value if expected_type else "entity"
            raise EntityNotFoundError(f"active {label} {entity_id} was not found")
        return entity

    async def _active_task(self, task_id: uuid.UUID, *, for_update: bool = False) -> Task:
        await self._active_entity(task_id, EntityType.TASK, for_update=for_update)
        if for_update and self._uses_postgresql():
            task = (
                await self.session.execute(
                    select(Task)
                    .where(Task.entity_id == task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        else:
            task = await self.session.get(Task, task_id)
        if task is None:
            raise EntityNotFoundError(f"active TASK {task_id} was not found")
        return task

    async def _active_project(self, project_id: uuid.UUID, *, for_update: bool = False) -> Project:
        await self._active_entity(project_id, EntityType.PROJECT, for_update=for_update)
        if for_update and self._uses_postgresql():
            project = (
                await self.session.execute(
                    select(Project)
                    .where(Project.entity_id == project_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        else:
            project = await self.session.get(Project, project_id)
        if project is None:
            raise EntityNotFoundError(f"active PROJECT {project_id} was not found")
        return project

    async def _active_goal(self, goal_id: uuid.UUID, *, for_update: bool = False) -> Goal:
        await self._active_entity(goal_id, EntityType.GOAL, for_update=for_update)
        if for_update and self._uses_postgresql():
            goal = (
                await self.session.execute(
                    select(Goal)
                    .where(Goal.entity_id == goal_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        else:
            goal = await self.session.get(Goal, goal_id)
        if goal is None:
            raise EntityNotFoundError(f"active GOAL {goal_id} was not found")
        return goal

    async def _success_criterion(
        self, criterion_id: uuid.UUID, *, for_update: bool = False
    ) -> SuccessCriterion:
        if for_update and self._uses_postgresql():
            criterion = (
                await self.session.execute(
                    select(SuccessCriterion)
                    .where(SuccessCriterion.id == criterion_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        else:
            criterion = await self.session.get(SuccessCriterion, criterion_id)
        if criterion is None:
            raise EntityNotFoundError(f"success criterion {criterion_id} was not found")
        await self._active_goal(criterion.goal_id)
        return criterion

    async def _active_decision(
        self, decision_id: uuid.UUID, *, for_update: bool = False
    ) -> Decision:
        await self._active_entity(decision_id, EntityType.DECISION, for_update=for_update)
        if for_update and self._uses_postgresql():
            decision = (
                await self.session.execute(
                    select(Decision)
                    .where(Decision.entity_id == decision_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        else:
            decision = await self.session.get(Decision, decision_id)
        if decision is None:
            raise EntityNotFoundError(f"active DECISION {decision_id} was not found")
        return decision

    async def _validate_parent_chain(
        self,
        *,
        child_id: uuid.UUID,
        parent_id: uuid.UUID | None,
        model: type[Task] | type[Project] | type[Goal],
        parent_field: str,
        entity_type: EntityType,
    ) -> None:
        if parent_id is None:
            return
        if parent_id == child_id:
            raise DomainValidationError(f"{entity_type.value} cannot be its own parent")

        seen = {child_id}
        current_id: uuid.UUID | None = parent_id
        while current_id is not None:
            if current_id in seen:
                raise DomainValidationError(
                    f"{entity_type.value} parent hierarchy cannot contain a cycle"
                )
            seen.add(current_id)
            await self._active_entity(current_id, entity_type)
            current = await self.session.get(model, current_id)
            if current is None:
                raise EntityNotFoundError(f"active {entity_type.value} {current_id} was not found")
            current_id = getattr(current, parent_field)

    async def _dependency_would_cycle(
        self, prerequisite_task_id: uuid.UUID, dependent_task_id: uuid.UUID
    ) -> bool:
        if prerequisite_task_id == dependent_task_id:
            return True

        target = prerequisite_task_id
        frontier = {dependent_task_id}
        seen: set[uuid.UUID] = set()
        while frontier:
            if target in frontier:
                return True
            seen |= frontier
            rows = await self.session.execute(
                select(TaskDependency.dependent_task_id).where(
                    TaskDependency.prerequisite_task_id.in_(frontier)
                )
            )
            frontier = set(rows.scalars().all()) - seen
        return False

    @atomic_domain_action
    async def create_task(self, command: TaskCreate, ctx: ActionContext) -> Task:
        if command.parent_task_id is not None:
            await self._active_task(command.parent_task_id)
        if command.primary_project_id is not None:
            await self._active_project(command.primary_project_id)
        goal_ids = list(dict.fromkeys(command.goal_ids))
        for goal_id in goal_ids:
            await self._active_goal(goal_id)
        prerequisite_ids = list(dict.fromkeys(command.prerequisite_task_ids))
        for prerequisite_id in prerequisite_ids:
            await self._active_task(prerequisite_id)

        op = await self._operation(ctx)
        entity = await self._entity(EntityType.TASK)
        task = Task(
            entity_id=entity.id,
            title=command.title,
            description=command.description,
            status=command.status,
            importance=command.importance,
            importance_source_id=ctx.source.id
            if command.importance is not None and ctx.source
            else None,
            deadline_at=command.deadline_at,
            deadline_type=command.deadline_type,
            estimated_duration_minutes=command.estimated_duration_minutes,
            estimate_source_id=(
                ctx.source.id
                if command.estimated_duration_minutes is not None and ctx.source
                else None
            ),
            parent_task_id=command.parent_task_id,
            primary_project_id=command.primary_project_id,
            auto_complete_from_children=command.auto_complete_from_children,
            completed_at=datetime.now(UTC) if command.status == TaskStatus.DONE else None,
        )
        self.session.add(task)
        for goal_id in goal_ids:
            self.session.add(
                TaskGoalLink(
                    task_id=entity.id,
                    goal_id=goal_id,
                    source_id=ctx.source.id if ctx.source else None,
                    confidence=ctx.source.confidence if ctx.source else None,
                )
            )
        for prerequisite_id in prerequisite_ids:
            self.session.add(
                TaskDependency(
                    prerequisite_task_id=prerequisite_id,
                    dependent_task_id=entity.id,
                    source_id=ctx.source.id if ctx.source else None,
                    confidence=ctx.source.confidence if ctx.source else None,
                )
            )
        await self._event(
            operation=op,
            event_type=EventType.TASK_CREATED,
            entity_id=entity.id,
            source=ctx.source,
            changes=[{"field": "created", "old": None, "new": command.model_dump(mode="json")}],
        )
        await self.session.flush()
        return task

    @atomic_domain_action
    async def update_task(self, command: TaskUpdate, ctx: ActionContext) -> Task:
        fields = set(command.model_fields_set) - {"entity_id"}
        if "parent_task_id" in fields:
            await self._lock_typed_graph(Task, EntityType.TASK)
        task = await self._active_task(command.entity_id, for_update=True)

        if "parent_task_id" in fields:
            await self._validate_parent_chain(
                child_id=command.entity_id,
                parent_id=command.parent_task_id,
                model=Task,
                parent_field="parent_task_id",
                entity_type=EntityType.TASK,
            )
        if "primary_project_id" in fields and command.primary_project_id is not None:
            await self._active_project(command.primary_project_id)

        resulting_deadline = command.deadline_at if "deadline_at" in fields else task.deadline_at
        resulting_type = command.deadline_type if "deadline_type" in fields else task.deadline_type
        if resulting_type is not None and resulting_deadline is None:
            if (
                "deadline_at" in fields
                and command.deadline_at is None
                and "deadline_type" not in fields
            ):
                fields.add("deadline_type")
                command.deadline_type = None
            else:
                raise DomainValidationError("deadline_type requires deadline_at")

        op = await self._operation(ctx)
        changes: list[dict[str, Any]] = []
        ordered_fields = [
            "title",
            "description",
            "status",
            "importance",
            "deadline_at",
            "deadline_type",
            "estimated_duration_minutes",
            "parent_task_id",
            "primary_project_id",
            "auto_complete_from_children",
        ]
        old_status = task.status
        for field in ordered_fields:
            if field not in fields:
                continue
            old = getattr(task, field)
            new = getattr(command, field)
            if old == new:
                continue
            setattr(task, field, new)
            changes.append(_change(field, old, new))
            if field == "importance":
                task.importance_source_id = ctx.source.id if ctx.source else None
            elif field == "estimated_duration_minutes":
                task.estimate_source_id = ctx.source.id if ctx.source else None

        if "status" in fields and task.status != old_status:
            old_completed_at = task.completed_at
            if task.status == TaskStatus.DONE:
                task.completed_at = datetime.now(UTC)
            elif old_status == TaskStatus.DONE:
                task.completed_at = None
            if old_completed_at != task.completed_at:
                changes.append(_change("completed_at", old_completed_at, task.completed_at))

        if not changes:
            raise DomainValidationError("task update does not change current state")

        if task.status == TaskStatus.DONE and old_status != TaskStatus.DONE:
            event_type = EventType.TASK_COMPLETED
        elif task.status == TaskStatus.CANCELLED and old_status != TaskStatus.CANCELLED:
            event_type = EventType.TASK_CANCELLED
        elif "status" in fields and task.status != old_status:
            event_type = EventType.TASK_STATUS_CHANGED
        else:
            event_type = EventType.TASK_UPDATED

        await self._event(
            operation=op,
            event_type=event_type,
            entity_id=task.entity_id,
            source=ctx.source,
            changes=changes,
        )
        await self.session.flush()
        return task

    @atomic_domain_action
    async def assess_task(
        self, command: TaskAssessment, *, evidence_source: Source | None = None
    ) -> Task:
        inference = await assistant_inference_source(
            self.session,
            confidence=command.confidence,
            reason_summary=command.reason_summary,
            evidence_source=evidence_source,
        )
        payload: dict[str, Any] = {"entity_id": command.entity_id}
        if "importance" in command.model_fields_set:
            payload["importance"] = command.importance
        if "estimated_duration_minutes" in command.model_fields_set:
            payload["estimated_duration_minutes"] = command.estimated_duration_minutes
        return await self.update_task(
            TaskUpdate(**payload),
            ActionContext(
                actor_type=ActorType.ASSISTANT,
                source=inference,
                description="Elowyn task assessment",
                operation_id=None,
            ),
        )

    @atomic_domain_action
    async def create_project(self, command: ProjectCreate, ctx: ActionContext) -> Project:
        if command.parent_project_id is not None:
            await self._active_project(command.parent_project_id)
        goal_ids = list(dict.fromkeys(command.goal_ids))
        for goal_id in goal_ids:
            await self._active_goal(goal_id)

        op = await self._operation(ctx)
        entity = await self._entity(EntityType.PROJECT)
        project = Project(
            entity_id=entity.id,
            name=command.name,
            description=command.description,
            status=command.status,
            importance=command.importance,
            importance_source_id=ctx.source.id
            if command.importance is not None and ctx.source
            else None,
            target_date=command.target_date,
            target_date_type=command.target_date_type,
            parent_project_id=command.parent_project_id,
            completed_at=datetime.now(UTC) if command.status == ProjectStatus.COMPLETED else None,
        )
        self.session.add(project)
        for goal_id in goal_ids:
            self.session.add(
                ProjectGoalLink(
                    project_id=entity.id,
                    goal_id=goal_id,
                    source_id=ctx.source.id if ctx.source else None,
                    confidence=ctx.source.confidence if ctx.source else None,
                )
            )
        await self._event(
            operation=op,
            event_type=EventType.PROJECT_CREATED,
            entity_id=entity.id,
            source=ctx.source,
            changes=[{"field": "created", "old": None, "new": command.model_dump(mode="json")}],
        )
        await self.session.flush()
        return project

    @atomic_domain_action
    async def update_project(self, command: ProjectUpdate, ctx: ActionContext) -> Project:
        fields = set(command.model_fields_set) - {"entity_id"}
        if "parent_project_id" in fields:
            await self._lock_typed_graph(Project, EntityType.PROJECT)
        project = await self._active_project(command.entity_id, for_update=True)

        if "parent_project_id" in fields:
            await self._validate_parent_chain(
                child_id=command.entity_id,
                parent_id=command.parent_project_id,
                model=Project,
                parent_field="parent_project_id",
                entity_type=EntityType.PROJECT,
            )

        resulting_date = command.target_date if "target_date" in fields else project.target_date
        resulting_type = (
            command.target_date_type if "target_date_type" in fields else project.target_date_type
        )
        if resulting_type is not None and resulting_date is None:
            if (
                "target_date" in fields
                and command.target_date is None
                and "target_date_type" not in fields
            ):
                fields.add("target_date_type")
                command.target_date_type = None
            else:
                raise DomainValidationError("target_date_type requires target_date")

        op = await self._operation(ctx)
        changes: list[dict[str, Any]] = []
        old_status = project.status
        for field in [
            "name",
            "description",
            "status",
            "importance",
            "target_date",
            "target_date_type",
            "parent_project_id",
        ]:
            if field not in fields:
                continue
            old = getattr(project, field)
            new = getattr(command, field)
            if old == new:
                continue
            setattr(project, field, new)
            changes.append(_change(field, old, new))
            if field == "importance":
                project.importance_source_id = ctx.source.id if ctx.source else None

        if "status" in fields and project.status != old_status:
            old_completed_at = project.completed_at
            if project.status == ProjectStatus.COMPLETED:
                project.completed_at = datetime.now(UTC)
            elif old_status == ProjectStatus.COMPLETED:
                project.completed_at = None
            if old_completed_at != project.completed_at:
                changes.append(_change("completed_at", old_completed_at, project.completed_at))

        if not changes:
            raise DomainValidationError("project update does not change current state")

        if project.status == ProjectStatus.COMPLETED and old_status != ProjectStatus.COMPLETED:
            event_type = EventType.PROJECT_COMPLETED
        elif project.status == ProjectStatus.CANCELLED and old_status != ProjectStatus.CANCELLED:
            event_type = EventType.PROJECT_CANCELLED
        elif "status" in fields and project.status != old_status:
            event_type = EventType.PROJECT_STATUS_CHANGED
        else:
            event_type = EventType.PROJECT_UPDATED

        await self._event(
            operation=op,
            event_type=event_type,
            entity_id=project.entity_id,
            source=ctx.source,
            changes=changes,
        )
        await self.session.flush()
        return project

    @atomic_domain_action
    async def cache_project_summary(self, command: ProjectSummaryCacheUpdate) -> Project:
        project = await self._active_project(command.entity_id, for_update=True)
        project.current_summary = command.summary.strip()
        project.current_summary_updated_at = datetime.now(UTC)
        await self.session.flush()
        return project

    @atomic_domain_action
    async def assess_project(
        self, command: ProjectAssessment, *, evidence_source: Source | None = None
    ) -> Project:
        inference = await assistant_inference_source(
            self.session,
            confidence=command.confidence,
            reason_summary=command.reason_summary,
            evidence_source=evidence_source,
        )
        return await self.update_project(
            ProjectUpdate(entity_id=command.entity_id, importance=command.importance),
            ActionContext(
                actor_type=ActorType.ASSISTANT,
                source=inference,
                description="Elowyn project importance assessment",
            ),
        )

    @atomic_domain_action
    async def create_goal(self, command: GoalCreate, ctx: ActionContext) -> Goal:
        if command.parent_goal_id is not None:
            await self._active_goal(command.parent_goal_id)

        op = await self._operation(ctx)
        entity = await self._entity(EntityType.GOAL)
        goal = Goal(
            entity_id=entity.id,
            title=command.title,
            description=command.description,
            status=command.status,
            importance=command.importance,
            importance_source_id=ctx.source.id
            if command.importance is not None and ctx.source
            else None,
            target_date=command.target_date,
            target_date_type=command.target_date_type,
            parent_goal_id=command.parent_goal_id,
            achieved_at=datetime.now(UTC) if command.status == GoalStatus.ACHIEVED else None,
        )
        self.session.add(goal)
        for criterion in command.success_criteria:
            self.session.add(
                SuccessCriterion(
                    goal_id=entity.id,
                    description=criterion.description,
                    created_source_id=ctx.source.id if ctx.source else None,
                )
            )
        await self._event(
            operation=op,
            event_type=EventType.GOAL_CREATED,
            entity_id=entity.id,
            source=ctx.source,
            changes=[{"field": "created", "old": None, "new": command.model_dump(mode="json")}],
        )
        await self.session.flush()
        return goal

    @atomic_domain_action
    async def update_goal(self, command: GoalUpdate, ctx: ActionContext) -> Goal:
        fields = set(command.model_fields_set) - {"entity_id"}
        if "parent_goal_id" in fields:
            await self._lock_typed_graph(Goal, EntityType.GOAL)
        goal = await self._active_goal(command.entity_id, for_update=True)

        if "parent_goal_id" in fields:
            await self._validate_parent_chain(
                child_id=command.entity_id,
                parent_id=command.parent_goal_id,
                model=Goal,
                parent_field="parent_goal_id",
                entity_type=EntityType.GOAL,
            )

        resulting_date = command.target_date if "target_date" in fields else goal.target_date
        resulting_type = (
            command.target_date_type if "target_date_type" in fields else goal.target_date_type
        )
        if resulting_type is not None and resulting_date is None:
            if (
                "target_date" in fields
                and command.target_date is None
                and "target_date_type" not in fields
            ):
                fields.add("target_date_type")
                command.target_date_type = None
            else:
                raise DomainValidationError("target_date_type requires target_date")

        op = await self._operation(ctx)
        changes: list[dict[str, Any]] = []
        old_status = goal.status
        for field in [
            "title",
            "description",
            "status",
            "importance",
            "target_date",
            "target_date_type",
            "parent_goal_id",
        ]:
            if field not in fields:
                continue
            old = getattr(goal, field)
            new = getattr(command, field)
            if old == new:
                continue
            setattr(goal, field, new)
            changes.append(_change(field, old, new))
            if field == "importance":
                goal.importance_source_id = ctx.source.id if ctx.source else None

        if "status" in fields and goal.status != old_status:
            old_achieved_at = goal.achieved_at
            if goal.status == GoalStatus.ACHIEVED:
                goal.achieved_at = datetime.now(UTC)
            elif old_status == GoalStatus.ACHIEVED:
                goal.achieved_at = None
            if old_achieved_at != goal.achieved_at:
                changes.append(_change("achieved_at", old_achieved_at, goal.achieved_at))

        if not changes:
            raise DomainValidationError("goal update does not change current state")

        event_type = (
            EventType.GOAL_ACHIEVED
            if goal.status == GoalStatus.ACHIEVED and old_status != GoalStatus.ACHIEVED
            else EventType.GOAL_STATUS_CHANGED
            if "status" in fields and goal.status != old_status
            else EventType.GOAL_UPDATED
        )
        await self._event(
            operation=op,
            event_type=event_type,
            entity_id=goal.entity_id,
            source=ctx.source,
            changes=changes,
        )
        await self.session.flush()
        return goal

    @atomic_domain_action
    async def assess_goal(
        self, command: GoalAssessment, *, evidence_source: Source | None = None
    ) -> Goal:
        inference = await assistant_inference_source(
            self.session,
            confidence=command.confidence,
            reason_summary=command.reason_summary,
            evidence_source=evidence_source,
        )
        return await self.update_goal(
            GoalUpdate(entity_id=command.entity_id, importance=command.importance),
            ActionContext(
                actor_type=ActorType.ASSISTANT,
                source=inference,
                description="Elowyn goal importance assessment",
            ),
        )

    @atomic_domain_action
    async def update_success_criterion(
        self, command: SuccessCriterionUpdate, ctx: ActionContext
    ) -> SuccessCriterion:
        criterion = await self._success_criterion(command.criterion_id, for_update=True)
        fields = set(command.model_fields_set) - {"criterion_id"}
        changed_fields: list[str] = []
        old_values: dict[str, Any] = {}
        for field in ["description", "status", "confidence", "evaluation_summary"]:
            if field not in fields:
                continue
            old = getattr(criterion, field)
            new = getattr(command, field)
            if old == new:
                continue
            old_values[field] = old
            setattr(criterion, field, new)
            changed_fields.append(field)

        if not changed_fields:
            raise DomainValidationError("success criterion update does not change current state")

        if {"status", "confidence", "evaluation_summary"} & set(changed_fields):
            old_values["evaluation_source_id"] = criterion.evaluation_source_id
            criterion.evaluation_source_id = ctx.source.id if ctx.source else None
            changed_fields.append("evaluation_source_id")

        new_values = {field: getattr(criterion, field) for field in changed_fields}
        op = await self._operation(ctx)
        await self._event(
            operation=op,
            event_type=EventType.SUCCESS_CRITERION_UPDATED,
            entity_id=criterion.goal_id,
            source=ctx.source,
            changes=[
                {
                    "field": "success_criterion",
                    "criterion_id": str(criterion.id),
                    "old": _json_value(old_values),
                    "new": _json_value(new_values),
                }
            ],
        )
        await self.session.flush()
        return criterion

    @atomic_domain_action
    async def assess_success_criterion(
        self,
        command: SuccessCriterionAssessment,
        *,
        evidence_source: Source | None = None,
    ) -> SuccessCriterion:
        inference = await assistant_inference_source(
            self.session,
            confidence=command.confidence,
            reason_summary=command.reason_summary,
            evidence_source=evidence_source,
        )
        return await self.update_success_criterion(
            SuccessCriterionUpdate(
                criterion_id=command.criterion_id,
                status=command.status,
                confidence=command.confidence,
                evaluation_summary=command.evaluation_summary,
            ),
            ActionContext(
                actor_type=ActorType.ASSISTANT,
                source=inference,
                description="Elowyn success criterion assessment",
            ),
        )

    @atomic_domain_action
    async def create_decision(self, command: DecisionCreate, ctx: ActionContext) -> Decision:
        superseded: Decision | None = None
        superseded_entity: Entity | None = None
        if command.supersedes_decision_id is not None:
            if command.status != DecisionStatus.ACTIVE:
                raise DomainValidationError("a superseding decision must be ACTIVE")
            superseded = await self._active_decision(
                command.supersedes_decision_id, for_update=True
            )
            if superseded.status != DecisionStatus.ACTIVE:
                raise DomainValidationError("only an ACTIVE decision can be superseded")
            superseded_entity = await self._active_entity(
                command.supersedes_decision_id, EntityType.DECISION
            )

        op = await self._operation(ctx)
        entity = await self._entity(EntityType.DECISION)
        decision = Decision(
            entity_id=entity.id,
            title=command.title,
            description=command.description,
            chosen_option=command.chosen_option,
            reasoning_summary=command.reasoning_summary,
            status=command.status,
            supersedes_decision_id=command.supersedes_decision_id,
        )
        self.session.add(decision)
        for alternative in command.alternatives:
            self.session.add(
                DecisionAlternative(
                    decision_id=entity.id,
                    option_text=alternative.option_text,
                    rejection_summary=alternative.rejection_summary,
                )
            )

        if superseded is not None and superseded_entity is not None:
            old_status = superseded.status
            superseded.status = DecisionStatus.SUPERSEDED
            superseded_entity.superseded_by_entity_id = entity.id
            await self._event(
                operation=op,
                event_type=EventType.DECISION_SUPERSEDED,
                entity_id=superseded.entity_id,
                source=ctx.source,
                changes=[
                    _change("status", old_status, DecisionStatus.SUPERSEDED),
                    _change("superseded_by_entity_id", None, entity.id),
                ],
            )

        await self._event(
            operation=op,
            event_type=EventType.DECISION_CREATED,
            entity_id=entity.id,
            source=ctx.source,
            changes=[{"field": "created", "old": None, "new": command.model_dump(mode="json")}],
        )
        await self.session.flush()
        return decision

    @atomic_domain_action
    async def revoke_decision(self, command: DecisionRevoke, ctx: ActionContext) -> Decision:
        decision = await self._active_decision(command.entity_id, for_update=True)
        if decision.status != DecisionStatus.ACTIVE:
            raise DomainValidationError("only an ACTIVE decision can be revoked")
        op = await self._operation(ctx)
        old = decision.status
        decision.status = DecisionStatus.REVOKED
        changes = [_change("status", old, decision.status)]
        if command.reason_summary:
            changes.append(_change("revoke_reason", None, command.reason_summary))
        await self._event(
            operation=op,
            event_type=EventType.DECISION_REVOKED,
            entity_id=decision.entity_id,
            source=ctx.source,
            changes=changes,
        )
        await self.session.flush()
        return decision

    @atomic_domain_action
    async def link_task_goal(self, command: TaskGoalLinkCreate, ctx: ActionContext) -> TaskGoalLink:
        await self._lock_entities([command.task_id, command.goal_id])
        await self._active_task(command.task_id)
        await self._active_goal(command.goal_id)
        existing = await self.session.get(TaskGoalLink, (command.task_id, command.goal_id))
        if existing is not None:
            return existing

        op = await self._operation(ctx)
        link = TaskGoalLink(
            task_id=command.task_id,
            goal_id=command.goal_id,
            source_id=ctx.source.id if ctx.source else None,
            confidence=ctx.source.confidence if ctx.source else None,
        )
        self.session.add(link)
        await self._event(
            operation=op,
            event_type=EventType.RELATION_CREATED,
            entity_id=command.task_id,
            source=ctx.source,
            changes=[_change("task_goal_link", None, command.model_dump(mode="json"))],
        )
        await self.session.flush()
        return link

    @atomic_domain_action
    async def link_project_goal(
        self, command: ProjectGoalLinkCreate, ctx: ActionContext
    ) -> ProjectGoalLink:
        await self._lock_entities([command.project_id, command.goal_id])
        await self._active_project(command.project_id)
        await self._active_goal(command.goal_id)
        existing = await self.session.get(ProjectGoalLink, (command.project_id, command.goal_id))
        if existing is not None:
            return existing

        op = await self._operation(ctx)
        link = ProjectGoalLink(
            project_id=command.project_id,
            goal_id=command.goal_id,
            source_id=ctx.source.id if ctx.source else None,
            confidence=ctx.source.confidence if ctx.source else None,
        )
        self.session.add(link)
        await self._event(
            operation=op,
            event_type=EventType.RELATION_CREATED,
            entity_id=command.project_id,
            source=ctx.source,
            changes=[_change("project_goal_link", None, command.model_dump(mode="json"))],
        )
        await self.session.flush()
        return link

    @atomic_domain_action
    async def add_task_dependency(
        self, command: TaskDependencyCreate, ctx: ActionContext
    ) -> TaskDependency:
        await self._lock_typed_graph(Task, EntityType.TASK)
        await self._active_task(command.prerequisite_task_id)
        await self._active_task(command.dependent_task_id)
        existing = await self.session.get(
            TaskDependency, (command.prerequisite_task_id, command.dependent_task_id)
        )
        if existing is not None:
            return existing
        if await self._dependency_would_cycle(
            command.prerequisite_task_id, command.dependent_task_id
        ):
            raise DomainValidationError("task dependency graph cannot contain a cycle")

        op = await self._operation(ctx)
        dependency = TaskDependency(
            prerequisite_task_id=command.prerequisite_task_id,
            dependent_task_id=command.dependent_task_id,
            source_id=ctx.source.id if ctx.source else None,
            confidence=ctx.source.confidence if ctx.source else None,
        )
        self.session.add(dependency)
        await self._event(
            operation=op,
            event_type=EventType.RELATION_CREATED,
            entity_id=command.dependent_task_id,
            source=ctx.source,
            changes=[_change("task_dependency", None, command.model_dump(mode="json"))],
        )
        await self.session.flush()
        return dependency

    @atomic_domain_action
    async def create_relation(
        self, command: EntityRelationCreate, ctx: ActionContext
    ) -> EntityRelation:
        await self._lock_entities([command.source_entity_id, command.target_entity_id])
        await self._active_entity(command.source_entity_id)
        await self._active_entity(command.target_entity_id)
        existing = (
            await self.session.execute(
                select(EntityRelation).where(
                    EntityRelation.source_entity_id == command.source_entity_id,
                    EntityRelation.target_entity_id == command.target_entity_id,
                    EntityRelation.relation_type == command.relation_type,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        op = await self._operation(ctx)
        relation = EntityRelation(
            source_entity_id=command.source_entity_id,
            target_entity_id=command.target_entity_id,
            relation_type=command.relation_type,
            source_id=ctx.source.id if ctx.source else None,
            confidence=ctx.source.confidence if ctx.source else None,
        )
        self.session.add(relation)
        await self._event(
            operation=op,
            event_type=EventType.RELATION_CREATED,
            entity_id=command.source_entity_id,
            source=ctx.source,
            changes=[_change("entity_relation", None, command.model_dump(mode="json"))],
        )
        await self.session.flush()
        return relation

    @atomic_domain_action
    async def infer_relation(
        self, command: EntityRelationInference, *, evidence_source: Source | None = None
    ) -> EntityRelation:
        inference = await assistant_inference_source(
            self.session,
            confidence=command.confidence,
            reason_summary=command.reason_summary,
            evidence_source=evidence_source,
        )
        return await self.create_relation(
            EntityRelationCreate(
                source_entity_id=command.source_entity_id,
                target_entity_id=command.target_entity_id,
                relation_type=command.relation_type,
            ),
            ActionContext(
                actor_type=ActorType.ASSISTANT,
                source=inference,
                description="Elowyn semantic relation inference",
            ),
        )

    @atomic_domain_action
    async def undo_last_change(
        self, ctx: ActionContext, *, entity_id: uuid.UUID | None = None
    ) -> Event:
        reversed_ids = select(Event.reverses_event_id).where(Event.reverses_event_id.is_not(None))
        undoable = [
            EventType.TASK_UPDATED,
            EventType.TASK_STATUS_CHANGED,
            EventType.TASK_COMPLETED,
            EventType.TASK_CANCELLED,
            EventType.PROJECT_UPDATED,
            EventType.PROJECT_STATUS_CHANGED,
            EventType.PROJECT_COMPLETED,
            EventType.PROJECT_CANCELLED,
            EventType.GOAL_UPDATED,
            EventType.GOAL_STATUS_CHANGED,
            EventType.GOAL_ACHIEVED,
            EventType.SUCCESS_CRITERION_UPDATED,
        ]
        base_stmt = select(Event).where(
            Event.event_type.in_(undoable),
            Event.id.not_in(reversed_ids),
        )
        if entity_id is None:
            candidate = (
                await self.session.execute(
                    base_stmt.order_by(Event.created_at.desc(), Event.id.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if candidate is None or candidate.entity_id is None:
                raise DomainValidationError("there is no reversible state change")
            entity_id = candidate.entity_id

        await self._active_entity(entity_id, for_update=True)
        target = (
            await self.session.execute(
                base_stmt.where(Event.entity_id == entity_id)
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if target is None or target.entity_id is None:
            raise DomainValidationError("there is no reversible state change")

        reversed_changes: list[dict[str, Any]] = []
        if target.event_type == EventType.SUCCESS_CRITERION_UPDATED:
            if len(target.changes) != 1:
                raise DomainValidationError("success criterion event is not reversible")
            change = target.changes[0]
            if change.get("field") != "success_criterion" or not change.get("criterion_id"):
                raise DomainValidationError("success criterion event is not reversible")
            criterion = await self._success_criterion(
                uuid.UUID(change["criterion_id"]), for_update=True
            )
            old_values = change.get("old")
            if not isinstance(old_values, dict):
                raise DomainValidationError("success criterion event is not reversible")
            current_values: dict[str, Any] = {}
            restored_values: dict[str, Any] = {}
            for field, value in old_values.items():
                if field not in {
                    "description",
                    "status",
                    "confidence",
                    "evaluation_summary",
                    "evaluation_source_id",
                }:
                    raise DomainValidationError("success criterion event is not reversible")
                current_values[field] = getattr(criterion, field)
                restored: Any = value
                if field == "status" and value is not None:
                    restored = SuccessCriterionStatus(value)
                elif field == "evaluation_source_id" and value is not None:
                    restored = uuid.UUID(value)
                setattr(criterion, field, restored)
                restored_values[field] = restored
            reversed_changes = [
                {
                    "field": "success_criterion",
                    "criterion_id": str(criterion.id),
                    "old": _json_value(current_values),
                    "new": _json_value(restored_values),
                }
            ]
            op = await self._operation(ctx)
            undo_event = await self._event(
                operation=op,
                event_type=EventType.UNDO_APPLIED,
                entity_id=criterion.goal_id,
                source=ctx.source,
                changes=reversed_changes,
                reverses_event_id=target.id,
            )
            await self.session.flush()
            return undo_event

        if target.event_type.value.startswith("TASK_"):
            obj: Task | Project | Goal = await self._active_task(target.entity_id, for_update=True)
            enum_fields = {"status": TaskStatus, "deadline_type": DeadlineType}
            datetime_fields = {"deadline_at", "completed_at"}
            uuid_fields = {"parent_task_id", "primary_project_id"}
        elif target.event_type.value.startswith("PROJECT_"):
            obj = await self._active_project(target.entity_id, for_update=True)
            enum_fields = {"status": ProjectStatus, "target_date_type": DeadlineType}
            datetime_fields = {"target_date", "completed_at"}
            uuid_fields = {"parent_project_id"}
        else:
            obj = await self._active_goal(target.entity_id, for_update=True)
            enum_fields = {"status": GoalStatus, "target_date_type": DeadlineType}
            datetime_fields = {"target_date", "achieved_at"}
            uuid_fields = {"parent_goal_id"}

        for original in target.changes:
            field = original.get("field")
            if not isinstance(field, str) or not hasattr(obj, field):
                raise DomainValidationError(f"event {target.id} contains a non-reversible field")
            old_value = original.get("old")
            current_value = getattr(obj, field)
            restored = self._restore_value(
                field,
                old_value,
                enum_fields=enum_fields,
                datetime_fields=datetime_fields,
                uuid_fields=uuid_fields,
            )
            if field == "parent_task_id":
                await self._validate_parent_chain(
                    child_id=target.entity_id,
                    parent_id=restored,
                    model=Task,
                    parent_field="parent_task_id",
                    entity_type=EntityType.TASK,
                )
            elif field == "parent_project_id":
                await self._validate_parent_chain(
                    child_id=target.entity_id,
                    parent_id=restored,
                    model=Project,
                    parent_field="parent_project_id",
                    entity_type=EntityType.PROJECT,
                )
            elif field == "parent_goal_id":
                await self._validate_parent_chain(
                    child_id=target.entity_id,
                    parent_id=restored,
                    model=Goal,
                    parent_field="parent_goal_id",
                    entity_type=EntityType.GOAL,
                )
            setattr(obj, field, restored)
            reversed_changes.append(_change(field, current_value, restored))
            if isinstance(obj, Task) and field == "importance":
                obj.importance_source_id = ctx.source.id if ctx.source else None
            if isinstance(obj, Task) and field == "estimated_duration_minutes":
                obj.estimate_source_id = ctx.source.id if ctx.source else None
            if isinstance(obj, (Project, Goal)) and field == "importance":
                obj.importance_source_id = ctx.source.id if ctx.source else None

        op = await self._operation(ctx)
        undo_event = await self._event(
            operation=op,
            event_type=EventType.UNDO_APPLIED,
            entity_id=target.entity_id,
            source=ctx.source,
            changes=reversed_changes,
            reverses_event_id=target.id,
        )
        await self.session.flush()
        return undo_event

    @staticmethod
    def _restore_value(
        field: str,
        value: Any,
        *,
        enum_fields: Mapping[str, type[Enum]],
        datetime_fields: set[str],
        uuid_fields: set[str],
    ) -> Any:
        if value is None:
            return None
        if field in enum_fields:
            return enum_fields[field](value)
        if field in datetime_fields:
            return datetime.fromisoformat(value)
        if field in uuid_fields:
            return uuid.UUID(value)
        return value


async def assistant_inference_source(
    session: AsyncSession,
    *,
    confidence: float,
    reason_summary: str,
    evidence_source: Source | None = None,
) -> Source:
    if not 0 <= confidence <= 1:
        raise DomainValidationError("assistant inference confidence must be between 0 and 1")
    if not reason_summary.strip():
        raise DomainValidationError("assistant inference requires reason_summary")

    source = Source(
        source_type=SourceType.ASSISTANT_INFERENCE,
        confidence=confidence,
        reason_summary=reason_summary.strip(),
    )
    session.add(source)
    await session.flush()
    if evidence_source is not None:
        session.add(SourceDependency(source_id=source.id, evidence_source_id=evidence_source.id))
        await session.flush()
    return source
