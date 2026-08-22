from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import (
    Decision,
    Entity,
    Event,
    Goal,
    Message,
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
from elowyn.domain.enums import (
    DecisionStatus,
    EntityType,
    MessageAuthor,
    RelationType,
    SourceType,
    SuccessCriterionStatus,
)


@dataclass(frozen=True, order=True)
class ConsistencyIssue:
    code: str
    object_id: str
    detail: str


@dataclass(frozen=True)
class ConsistencyReport:
    issues: tuple[ConsistencyIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def require_ok(self) -> None:
        if self.issues:
            summary = "; ".join(f"{issue.code}:{issue.object_id}" for issue in self.issues[:10])
            raise AssertionError(f"database consistency check failed: {summary}")


class ConsistencyVerifier:
    """Read-only support verifier; canonical state remains in the domain tables."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._issues: list[ConsistencyIssue] = []

    def issue(self, code: str, object_id: UUID | str, detail: str) -> None:
        self._issues.append(ConsistencyIssue(code, str(object_id), detail))

    async def verify(self) -> ConsistencyReport:
        with self.session.no_autoflush:
            return await self._verify_without_autoflush()

    async def _verify_without_autoflush(self) -> ConsistencyReport:
        self._issues = []
        entities = {
            item.id: item for item in (await self.session.execute(select(Entity))).scalars()
        }
        tasks = {
            item.entity_id: item for item in (await self.session.execute(select(Task))).scalars()
        }
        projects = {
            item.entity_id: item for item in (await self.session.execute(select(Project))).scalars()
        }
        goals = {
            item.entity_id: item for item in (await self.session.execute(select(Goal))).scalars()
        }
        decisions = {
            item.entity_id: item
            for item in (await self.session.execute(select(Decision))).scalars()
        }
        typed: dict[EntityType, Mapping[UUID, object]] = {
            EntityType.TASK: tasks,
            EntityType.PROJECT: projects,
            EntityType.GOAL: goals,
            EntityType.DECISION: decisions,
        }
        for entity in entities.values():
            expected = typed[entity.entity_type]
            if entity.id not in expected:
                self.issue(
                    "PHYSICAL_TYPED_ROW_MISSING",
                    entity.id,
                    f"{entity.entity_type.value} identity has no typed row",
                )
            for other_type, rows in typed.items():
                if other_type != entity.entity_type and entity.id in rows:
                    self.issue(
                        "ENTITY_TYPE_MISMATCH",
                        entity.id,
                        f"identity is {entity.entity_type.value}, row is {other_type.value}",
                    )
        for entity_type, rows in typed.items():
            for entity_id in rows:
                if entity_id not in entities:
                    self.issue(
                        "TYPED_ROW_WITHOUT_ENTITY",
                        entity_id,
                        f"{entity_type.value} row has no identity",
                    )

        await self._verify_history_and_provenance(entities, goals)
        await self._verify_relations(entities, tasks, projects, goals)
        self._verify_supersede_chains(entities, decisions)
        self._verify_parent_cycles(tasks, "parent_task_id", "TASK_PARENT_CYCLE")
        self._verify_parent_cycles(projects, "parent_project_id", "PROJECT_PARENT_CYCLE")
        self._verify_parent_cycles(goals, "parent_goal_id", "GOAL_PARENT_CYCLE")
        return ConsistencyReport(tuple(sorted(set(self._issues))))

    async def _verify_history_and_provenance(
        self, entities: dict[UUID, Entity], goals: dict[UUID, Goal]
    ) -> None:
        messages = {
            item.id: item for item in (await self.session.execute(select(Message))).scalars()
        }
        sources = {item.id: item for item in (await self.session.execute(select(Source))).scalars()}
        operations = {
            item.id: item for item in (await self.session.execute(select(Operation))).scalars()
        }
        events = {item.id: item for item in (await self.session.execute(select(Event))).scalars()}
        dependencies = list((await self.session.execute(select(SourceDependency))).scalars().all())
        evidence_by_source: dict[UUID, set[UUID]] = {}
        for dependency in dependencies:
            evidence_by_source.setdefault(dependency.source_id, set()).add(
                dependency.evidence_source_id
            )
            if dependency.source_id not in sources or dependency.evidence_source_id not in sources:
                self.issue(
                    "DANGLING_SOURCE_DEPENDENCY",
                    dependency.source_id,
                    "source or evidence source is missing",
                )
            elif sources[dependency.source_id].source_type != SourceType.ASSISTANT_INFERENCE:
                self.issue(
                    "INVALID_INFERENCE_DEPENDENCY",
                    dependency.source_id,
                    "dependency owner is not ASSISTANT_INFERENCE",
                )

        user_sources_by_message: dict[UUID, list[UUID]] = {}
        for source in sources.values():
            if source.message_id is not None and source.message_id not in messages:
                self.issue("SOURCE_MESSAGE_MISSING", source.id, "message does not exist")
            if source.source_type == SourceType.USER_MESSAGE:
                if source.message_id is None or source.message_id not in messages:
                    self.issue(
                        "BROKEN_USER_MESSAGE_SOURCE", source.id, "message provenance is missing"
                    )
                else:
                    user_sources_by_message.setdefault(source.message_id, []).append(source.id)
                    if messages[source.message_id].author != MessageAuthor.USER:
                        self.issue(
                            "BROKEN_USER_MESSAGE_SOURCE",
                            source.id,
                            "source points to a non-user message",
                        )
            if source.source_type == SourceType.ASSISTANT_INFERENCE:
                if not source.reason_summary or source.confidence is None:
                    self.issue(
                        "BROKEN_INFERENCE_PROVENANCE",
                        source.id,
                        "confidence or reason is missing",
                    )
                if not evidence_by_source.get(source.id):
                    self.issue(
                        "BROKEN_INFERENCE_PROVENANCE",
                        source.id,
                        "evidence dependency is missing",
                    )
        for message in messages.values():
            if (
                message.author == MessageAuthor.USER
                and len(user_sources_by_message.get(message.id, [])) != 1
            ):
                self.issue(
                    "BROKEN_MESSAGE_PROVENANCE",
                    message.id,
                    "user message must have exactly one USER_MESSAGE source",
                )

        for operation in operations.values():
            if operation.source_id is not None and operation.source_id not in sources:
                self.issue("OPERATION_SOURCE_MISSING", operation.id, "source does not exist")
        for event in events.values():
            if event.operation_id not in operations:
                self.issue("EVENT_OPERATION_MISSING", event.id, "operation does not exist")
            if event.entity_id is not None and event.entity_id not in entities:
                self.issue("EVENT_ENTITY_MISSING", event.id, "entity does not exist")
            if event.source_id is not None and event.source_id not in sources:
                self.issue("EVENT_SOURCE_MISSING", event.id, "source does not exist")
            if event.reverses_event_id is not None and event.reverses_event_id not in events:
                self.issue(
                    "EVENT_REVERSE_TARGET_MISSING", event.id, "reversed event does not exist"
                )

        criteria = (await self.session.execute(select(SuccessCriterion))).scalars().all()
        for criterion in criteria:
            if criterion.goal_id not in goals:
                self.issue("CRITERION_GOAL_MISSING", criterion.id, "goal does not exist")
            evaluated = (
                criterion.status != SuccessCriterionStatus.UNKNOWN
                or criterion.confidence is not None
                or criterion.evaluation_summary is not None
            )
            if evaluated and criterion.evaluation_source_id not in sources:
                self.issue(
                    "BROKEN_CRITERION_PROVENANCE",
                    criterion.id,
                    "evaluated criterion has no live source",
                )

    async def _verify_relations(
        self,
        entities: dict[UUID, Entity],
        tasks: dict[UUID, Task],
        projects: dict[UUID, Project],
        goals: dict[UUID, Goal],
    ) -> None:
        task_goal_links = (await self.session.execute(select(TaskGoalLink))).scalars().all()
        for task_goal_link in task_goal_links:
            if task_goal_link.task_id not in tasks or task_goal_link.goal_id not in goals:
                self.issue(
                    "DANGLING_TASK_GOAL_LINK",
                    f"{task_goal_link.task_id}:{task_goal_link.goal_id}",
                    "typed endpoint is missing",
                )
        project_goal_links = (await self.session.execute(select(ProjectGoalLink))).scalars().all()
        for project_goal_link in project_goal_links:
            if (
                project_goal_link.project_id not in projects
                or project_goal_link.goal_id not in goals
            ):
                self.issue(
                    "DANGLING_PROJECT_GOAL_LINK",
                    f"{project_goal_link.project_id}:{project_goal_link.goal_id}",
                    "typed endpoint is missing",
                )
        dependencies = (await self.session.execute(select(TaskDependency))).scalars().all()
        adjacency: dict[UUID, set[UUID]] = {}
        for dependency in dependencies:
            if (
                dependency.prerequisite_task_id not in tasks
                or dependency.dependent_task_id not in tasks
            ):
                self.issue(
                    "DANGLING_TASK_DEPENDENCY",
                    f"{dependency.prerequisite_task_id}:{dependency.dependent_task_id}",
                    "typed endpoint is missing",
                )
            adjacency.setdefault(dependency.prerequisite_task_id, set()).add(
                dependency.dependent_task_id
            )
        self._verify_directed_cycles(adjacency, "TASK_DEPENDENCY_CYCLE")

        valid_relation_types = {item.value for item in RelationType}
        rows = (
            await self.session.execute(
                text(
                    "SELECT id::text, source_entity_id::text, target_entity_id::text, "
                    "relation_type FROM entity_relations"
                )
            )
        ).all()
        for relation_id, source_id, target_id, relation_type in rows:
            if UUID(source_id) not in entities or UUID(target_id) not in entities:
                self.issue("DANGLING_ENTITY_RELATION", relation_id, "endpoint is missing")
            if relation_type not in valid_relation_types:
                self.issue("INVALID_RELATION_TYPE", relation_id, str(relation_type))

    def _verify_supersede_chains(
        self, entities: dict[UUID, Entity], decisions: dict[UUID, Decision]
    ) -> None:
        for decision in decisions.values():
            predecessor_id = decision.supersedes_decision_id
            if predecessor_id is None:
                continue
            predecessor = decisions.get(predecessor_id)
            predecessor_entity = entities.get(predecessor_id)
            if predecessor is None or predecessor_entity is None:
                self.issue(
                    "INVALID_SUPERSEDE_CHAIN",
                    decision.entity_id,
                    "predecessor is missing",
                )
            elif (
                predecessor.status != DecisionStatus.SUPERSEDED
                or predecessor_entity.superseded_by_entity_id != decision.entity_id
            ):
                self.issue(
                    "INVALID_SUPERSEDE_CHAIN",
                    decision.entity_id,
                    "predecessor lifecycle/link is inconsistent",
                )
        adjacency = {
            entity.id: {entity.superseded_by_entity_id}
            for entity in entities.values()
            if entity.superseded_by_entity_id is not None
        }
        for entity_id, successors in adjacency.items():
            successor = next(iter(successors))
            if successor not in entities:
                self.issue("INVALID_SUPERSEDE_CHAIN", entity_id, "successor is missing")
            elif entities[successor].entity_type != entities[entity_id].entity_type:
                self.issue("INVALID_SUPERSEDE_CHAIN", entity_id, "successor type differs")
        self._verify_directed_cycles(adjacency, "SUPERSEDE_CYCLE")

    def _verify_parent_cycles(self, rows: dict, parent_field: str, code: str) -> None:
        adjacency = {
            row_id: {parent_id}
            for row_id, row in rows.items()
            if (parent_id := getattr(row, parent_field)) is not None
        }
        self._verify_directed_cycles(adjacency, code)

    def _verify_directed_cycles(self, adjacency: dict[UUID, set[UUID]], code: str) -> None:
        visiting: set[UUID] = set()
        visited: set[UUID] = set()

        def visit(node: UUID) -> None:
            if node in visiting:
                self.issue(code, node, "directed cycle detected")
                return
            if node in visited:
                return
            visiting.add(node)
            for target in adjacency.get(node, set()):
                visit(target)
            visiting.remove(node)
            visited.add(node)

        for node in sorted(adjacency):
            visit(node)
