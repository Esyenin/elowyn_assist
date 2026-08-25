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
    Plan,
    PlanGoalLink,
    PlanItemProgress,
    PlanVersion,
    PlanVersionBasis,
    PlanVersionItem,
    PlanVersionItemDependency,
    PlanVersionPresentation,
    Project,
    ProjectGoalLink,
    Source,
    SourceDependency,
    Strategy,
    SuccessCriterion,
    Task,
    TaskDependency,
    TaskGoalLink,
)
from elowyn.domain.enums import (
    DecisionStatus,
    EntityType,
    MessageAuthor,
    PlanVersionStatus,
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
        plans = {
            item.entity_id: item for item in (await self.session.execute(select(Plan))).scalars()
        }
        strategies = {
            item.entity_id: item
            for item in (await self.session.execute(select(Strategy))).scalars()
        }
        typed: dict[EntityType, Mapping[UUID, object]] = {
            EntityType.TASK: tasks,
            EntityType.PROJECT: projects,
            EntityType.GOAL: goals,
            EntityType.DECISION: decisions,
            EntityType.PLAN: plans,
            EntityType.STRATEGY: strategies,
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
        await self._verify_planning(entities)
        self._verify_supersede_chains(entities, decisions)
        self._verify_parent_cycles(tasks, "parent_task_id", "TASK_PARENT_CYCLE")
        self._verify_parent_cycles(projects, "parent_project_id", "PROJECT_PARENT_CYCLE")
        self._verify_parent_cycles(goals, "parent_goal_id", "GOAL_PARENT_CYCLE")
        return ConsistencyReport(tuple(sorted(set(self._issues))))

    async def _verify_planning(self, entities: dict[UUID, Entity]) -> None:
        plans = {
            item.entity_id: item for item in (await self.session.execute(select(Plan))).scalars()
        }
        strategies = {
            item.entity_id: item
            for item in (await self.session.execute(select(Strategy))).scalars()
        }
        versions = {
            item.id: item for item in (await self.session.execute(select(PlanVersion))).scalars()
        }
        items = {
            item.id: item
            for item in (await self.session.execute(select(PlanVersionItem))).scalars()
        }
        messages = {
            item.id: item for item in (await self.session.execute(select(Message))).scalars()
        }
        events = {item.id: item for item in (await self.session.execute(select(Event))).scalars()}
        sources = {
            item.id: item for item in (await self.session.execute(select(Source))).scalars()
        }

        versions_by_plan: dict[UUID, list[PlanVersion]] = {}
        for version in versions.values():
            versions_by_plan.setdefault(version.plan_id, []).append(version)
        for plan_id, plan_versions in versions_by_plan.items():
            for status in (PlanVersionStatus.CANDIDATE, PlanVersionStatus.APPROVED):
                if sum(version.status == status for version in plan_versions) > 1:
                    self.issue(
                        "PLAN_CURRENT_VERSION_CARDINALITY",
                        plan_id,
                        f"more than one current {status.value} version",
                    )

        for plan in plans.values():
            if plan.strategy_id is not None and plan.strategy_id not in strategies:
                self.issue("PLAN_STRATEGY_MISSING", plan.entity_id, "strategy does not exist")
        for strategy in strategies.values():
            accepted = versions.get(strategy.accepted_from_plan_version_id)
            if accepted is None:
                self.issue(
                    "STRATEGY_ACCEPTED_VERSION_MISSING",
                    strategy.entity_id,
                    "accepted PlanVersion does not exist",
                )
            else:
                accepted_plan = plans.get(accepted.plan_id)
                if accepted_plan is None or accepted_plan.strategy_id != strategy.entity_id:
                    self.issue(
                        "STRATEGY_PLAN_MISMATCH",
                        strategy.entity_id,
                        "accepted PlanVersion Plan does not reference this Strategy",
                    )

        for version in versions.values():
            if version.plan_id not in plans:
                self.issue("PLAN_VERSION_PLAN_MISSING", version.id, "Plan does not exist")
            if version.based_on_version_id is not None:
                basis_version = versions.get(version.based_on_version_id)
                if basis_version is None or basis_version.plan_id != version.plan_id:
                    self.issue(
                        "PLAN_VERSION_LINEAGE_INVALID",
                        version.id,
                        "based-on version is missing or belongs to another Plan",
                    )
        for item in items.values():
            if item.plan_version_id not in versions:
                self.issue("PLAN_ITEM_VERSION_MISSING", item.id, "PlanVersion does not exist")

        dependencies = (
            await self.session.execute(select(PlanVersionItemDependency))
        ).scalars().all()
        for dependency in dependencies:
            prerequisite = items.get(dependency.prerequisite_item_id)
            dependent = items.get(dependency.dependent_item_id)
            if (
                prerequisite is None
                or dependent is None
                or prerequisite.plan_version_id != dependency.plan_version_id
                or dependent.plan_version_id != dependency.plan_version_id
            ):
                self.issue(
                    "PLAN_ITEM_DEPENDENCY_VERSION_MISMATCH",
                    f"{dependency.prerequisite_item_id}:{dependency.dependent_item_id}",
                    "dependency items do not belong to its PlanVersion",
                )

        presentations = (
            await self.session.execute(select(PlanVersionPresentation))
        ).scalars().all()
        presented_version_ids = {presentation.plan_version_id for presentation in presentations}
        for presentation in presentations:
            message = messages.get(presentation.message_id)
            if presentation.plan_version_id not in versions:
                self.issue(
                    "PLAN_PRESENTATION_VERSION_MISSING",
                    presentation.id,
                    "PlanVersion does not exist",
                )
            if message is None or message.author != MessageAuthor.ASSISTANT:
                self.issue(
                    "PLAN_PRESENTATION_MESSAGE_INVALID",
                    presentation.id,
                    "presentation must resolve to an assistant Message",
                )

        for version in versions.values():
            if version.status != PlanVersionStatus.APPROVED:
                continue
            approval_source = (
                sources.get(version.approval_source_id)
                if version.approval_source_id is not None
                else None
            )
            approval_message = (
                messages.get(approval_source.message_id)
                if approval_source is not None and approval_source.message_id is not None
                else None
            )
            if (
                approval_source is None
                or approval_source.source_type != SourceType.USER_MESSAGE
                or approval_message is None
                or approval_message.author != MessageAuthor.USER
            ):
                self.issue(
                    "PLAN_APPROVAL_SOURCE_INVALID",
                    version.id,
                    "Approved PlanVersion must resolve to a user Message Source",
                )
            if version.id not in presented_version_ids:
                self.issue(
                    "PLAN_APPROVED_WITHOUT_PRESENTATION",
                    version.id,
                    "Approved PlanVersion has no recorded Presentation",
                )

        expected_role_type = {
            "GOAL": EntityType.GOAL,
            "TASK": EntityType.TASK,
            "PROJECT": EntityType.PROJECT,
            "DECISION": EntityType.DECISION,
            "STRATEGY": EntityType.STRATEGY,
        }
        basis_rows = (await self.session.execute(select(PlanVersionBasis))).scalars().all()
        for basis in basis_rows:
            entity = entities.get(basis.entity_id)
            event = events.get(basis.event_id)
            if basis.plan_version_id not in versions:
                self.issue("PLAN_BASIS_VERSION_MISSING", basis.event_id, "PlanVersion is missing")
            if entity is None or event is None or event.entity_id != basis.entity_id:
                self.issue(
                    "PLAN_BASIS_EVENT_MISMATCH",
                    basis.event_id,
                    "basis Event does not resolve to the stated Entity",
                )
            elif entity.entity_type != expected_role_type[basis.role.value]:
                self.issue(
                    "PLAN_BASIS_ROLE_MISMATCH",
                    basis.event_id,
                    "basis role does not match Entity type",
                )

        links = (await self.session.execute(select(PlanGoalLink))).scalars().all()
        for link in links:
            if link.plan_id not in plans or link.goal_id not in {
                entity_id
                for entity_id, entity in entities.items()
                if entity.entity_type == EntityType.GOAL
            }:
                self.issue(
                    "PLAN_GOAL_LINK_INVALID",
                    f"{link.plan_id}:{link.goal_id}",
                    "typed Plan or Goal endpoint is missing",
                )

        progress_rows = (await self.session.execute(select(PlanItemProgress))).scalars().all()
        for progress in progress_rows:
            progress_item = items.get(progress.plan_version_item_id)
            progress_version = (
                versions.get(progress_item.plan_version_id)
                if progress_item is not None
                else None
            )
            if progress_version is None or progress_version.approval_source_id is None:
                self.issue(
                    "PLAN_PROGRESS_WITHOUT_APPROVAL",
                    progress.plan_version_item_id,
                    "progress belongs to an item that has never been Approved",
                )

        dependencies_by_version: dict[UUID, list[PlanVersionItemDependency]] = {}
        for dependency in dependencies:
            dependencies_by_version.setdefault(dependency.plan_version_id, []).append(dependency)
        for version_id, version_dependencies in dependencies_by_version.items():
            nodes = {
                item.id for item in items.values() if item.plan_version_id == version_id
            }
            outgoing: dict[UUID, set[UUID]] = {node: set() for node in nodes}
            indegree = dict.fromkeys(nodes, 0)
            for dependency in version_dependencies:
                if (
                    dependency.prerequisite_item_id not in nodes
                    or dependency.dependent_item_id not in nodes
                ):
                    continue
                outgoing[dependency.prerequisite_item_id].add(dependency.dependent_item_id)
                indegree[dependency.dependent_item_id] += 1
            frontier = [node for node, count in indegree.items() if count == 0]
            visited = 0
            while frontier:
                current = frontier.pop()
                visited += 1
                for dependent_id in outgoing[current]:
                    indegree[dependent_id] -= 1
                    if indegree[dependent_id] == 0:
                        frontier.append(dependent_id)
            if visited != len(nodes):
                self.issue(
                    "PLAN_ITEM_DEPENDENCY_CYCLE",
                    version_id,
                    "PlanVersion item dependencies contain a cycle",
                )

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
