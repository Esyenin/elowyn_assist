"""Pydantic AI tool wiring for Elowyn v0.1.

All DB-backed tools are sequential because they share one SQLAlchemy AsyncSession for the turn.
"""

from __future__ import annotations

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
from elowyn.services.query import WorldStateQueryService
from elowyn.services.world_state import (
    ActionContext,
    WorldStateService,
)


def build_agent(
    *,
    model,
    service: WorldStateService,
    query_service: WorldStateQueryService,
    action_context: ActionContext,
):
    from pydantic_ai import Agent

    from elowyn.assistant.identity import load_identity_prompt

    agent = Agent(model=model, system_prompt=load_identity_prompt())

    @agent.tool_plain(sequential=True)
    async def query_world_state(search_text: str | None = None) -> str:
        """Read current structured state. IDs in the result are internal and must not be shown to user."""
        return await query_service.render_for_llm(search_text=search_text)

    @agent.tool_plain(sequential=True)
    async def create_task(command: TaskCreate) -> dict[str, str]:
        """Create an unambiguous Task. Only copy user-stated estimates here; use assess_task for AI estimates."""
        task = await service.create_task(command, action_context)
        return {"internal_entity_id": str(task.entity_id), "result": "task created"}

    @agent.tool_plain(sequential=True)
    async def update_task(command: TaskUpdate) -> dict[str, str]:
        """Apply an unambiguous user correction/update to an existing Task."""
        task = await service.update_task(command, action_context)
        return {"internal_entity_id": str(task.entity_id), "result": "task updated"}

    @agent.tool_plain(sequential=True)
    async def assess_task(command: TaskAssessment) -> dict[str, str]:
        """Set Elowyn's own importance/duration estimate with confidence and provenance."""
        task = await service.assess_task(command, evidence_source=action_context.source)
        return {"internal_entity_id": str(task.entity_id), "result": "task assessment updated"}

    @agent.tool_plain(sequential=True)
    async def create_project(command: ProjectCreate) -> dict[str, str]:
        """Create an unambiguous Project."""
        project = await service.create_project(command, action_context)
        return {"internal_entity_id": str(project.entity_id), "result": "project created"}

    @agent.tool_plain(sequential=True)
    async def update_project(command: ProjectUpdate) -> dict[str, str]:
        """Apply an unambiguous user correction/update to an existing Project."""
        project = await service.update_project(command, action_context)
        return {"internal_entity_id": str(project.entity_id), "result": "project updated"}

    @agent.tool_plain(sequential=True)
    async def cache_project_summary(command: ProjectSummaryCacheUpdate) -> str:
        """Refresh a non-authoritative Project summary cache from current structured state."""
        await service.cache_project_summary(command)
        return "project summary cache refreshed"

    @agent.tool_plain(sequential=True)
    async def assess_project(command: ProjectAssessment) -> dict[str, str]:
        """Set Elowyn's own Project importance estimate with confidence and provenance."""
        project = await service.assess_project(command, evidence_source=action_context.source)
        return {"internal_entity_id": str(project.entity_id), "result": "project assessment updated"}

    @agent.tool_plain(sequential=True)
    async def create_goal(command: GoalCreate) -> dict[str, str]:
        """Create an unambiguous Goal (desired state, not an action)."""
        goal = await service.create_goal(command, action_context)
        return {"internal_entity_id": str(goal.entity_id), "result": "goal created"}

    @agent.tool_plain(sequential=True)
    async def update_goal(command: GoalUpdate) -> dict[str, str]:
        """Apply an unambiguous user correction/update to an existing Goal."""
        goal = await service.update_goal(command, action_context)
        return {"internal_entity_id": str(goal.entity_id), "result": "goal updated"}

    @agent.tool_plain(sequential=True)
    async def assess_goal(command: GoalAssessment) -> dict[str, str]:
        """Set Elowyn's own Goal importance estimate with confidence and provenance."""
        goal = await service.assess_goal(command, evidence_source=action_context.source)
        return {"internal_entity_id": str(goal.entity_id), "result": "goal assessment updated"}

    @agent.tool_plain(sequential=True)
    async def update_success_criterion(command: SuccessCriterionUpdate) -> str:
        """Apply an explicit user correction to a Goal success criterion."""
        await service.update_success_criterion(command, action_context)
        return "success criterion updated"

    @agent.tool_plain(sequential=True)
    async def assess_success_criterion(command: SuccessCriterionAssessment) -> str:
        """Evaluate a Goal success criterion as Elowyn with confidence and provenance."""
        await service.assess_success_criterion(command, evidence_source=action_context.source)
        return "success criterion assessment updated"

    @agent.tool_plain(sequential=True)
    async def record_decision(command: DecisionCreate) -> dict[str, str]:
        """Record a significant choice; use supersedes_decision_id when revising an earlier Decision."""
        decision = await service.create_decision(command, action_context)
        return {"internal_entity_id": str(decision.entity_id), "result": "decision recorded"}

    @agent.tool_plain(sequential=True)
    async def revoke_decision(command: DecisionRevoke) -> dict[str, str]:
        """Revoke an active Decision without deleting history."""
        decision = await service.revoke_decision(command, action_context)
        return {"internal_entity_id": str(decision.entity_id), "result": "decision revoked"}

    @agent.tool_plain(sequential=True)
    async def link_task_goal(command: TaskGoalLinkCreate) -> str:
        """Create the strict Task↔Goal relation."""
        await service.link_task_goal(command, action_context)
        return "task-goal link present"

    @agent.tool_plain(sequential=True)
    async def link_project_goal(command: ProjectGoalLinkCreate) -> str:
        """Create the strict Project↔Goal relation."""
        await service.link_project_goal(command, action_context)
        return "project-goal link present"

    @agent.tool_plain(sequential=True)
    async def add_task_dependency(command: TaskDependencyCreate) -> str:
        """Create a directed strict task dependency; cycles are rejected by Core."""
        await service.add_task_dependency(command, action_context)
        return "task dependency present"

    @agent.tool_plain(sequential=True)
    async def create_relation(command: EntityRelationCreate) -> str:
        """Create an additional semantic relation using only the controlled RelationType catalog."""
        await service.create_relation(command, action_context)
        return "semantic relation present"

    @agent.tool_plain(sequential=True)
    async def infer_relation(command: EntityRelationInference) -> str:
        """Create Elowyn's inferred semantic relation with confidence/provenance."""
        await service.infer_relation(command, evidence_source=action_context.source)
        return "inferred semantic relation present"

    @agent.tool_plain(sequential=True)
    async def undo_last_change(entity_id: str | None = None) -> str:
        """Undo the latest reversible state change by writing a new inverse Event; never delete history."""
        from uuid import UUID

        event = await service.undo_last_change(
            action_context,
            entity_id=UUID(entity_id) if entity_id else None,
        )
        return "inverse change recorded"

    return agent
