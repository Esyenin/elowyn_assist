"""Pydantic AI tool wiring for Elowyn v0.1.

All DB-backed tools are sequential because they share one SQLAlchemy AsyncSession for the turn.
"""

from __future__ import annotations

from uuid import UUID

from elowyn.assistant.deep_memory_tools import deep_memory_policy, register_deep_memory_tools
from elowyn.assistant.planning_presentation import PlanningTurnState
from elowyn.assistant.planning_tools import planning_policy, register_planning_tools
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
from elowyn.memory.deep import DeepMemoryRoute
from elowyn.services.deep_memory import DeepMemoryService
from elowyn.services.planning import PlanningService
from elowyn.services.planning_query import PlanningQueryService
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
    deep_memory_service: DeepMemoryService | None = None,
    deep_memory_route: DeepMemoryRoute = DeepMemoryRoute.NONE,
    planning_service: PlanningService | None = None,
    planning_query_service: PlanningQueryService | None = None,
    planning_turn_state: PlanningTurnState | None = None,
    conversation_id: UUID | None = None,
    user_message_id: UUID | None = None,
):
    from pydantic_ai import Agent

    from elowyn.assistant.identity import load_identity_prompt

    system_prompt = load_identity_prompt()
    if deep_memory_service is not None and deep_memory_route != DeepMemoryRoute.NONE:
        system_prompt += deep_memory_policy(deep_memory_route)
    elif (
        planning_service is not None
        and planning_query_service is not None
        and planning_turn_state is not None
    ):
        system_prompt += planning_policy()
    agent = Agent(model=model, system_prompt=system_prompt)

    @agent.tool_plain(sequential=True)
    async def query_world_state(search_text: str | None = None) -> str:
        """Read current state; returned IDs are internal and must not be shown to the user."""
        return await query_service.render_for_llm(search_text=search_text)

    if deep_memory_service is not None and deep_memory_route != DeepMemoryRoute.NONE:
        # A deep-memory answer cannot directly invoke a canonical write. If the user
        # also requests a state change, it can be handled as a separate normal turn.
        register_deep_memory_tools(agent, deep_memory_service, deep_memory_route)
        return agent

    if (
        planning_service is not None
        and planning_query_service is not None
        and planning_turn_state is not None
        and conversation_id is not None
        and user_message_id is not None
    ):
        register_planning_tools(
            agent,
            service=planning_service,
            query_service=planning_query_service,
            action_context=action_context,
            turn_state=planning_turn_state,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
        )

    @agent.tool_plain(sequential=True)
    async def create_task(command: TaskCreate) -> dict[str, str]:
        """Create a Task; send AI-generated estimates through assess_task instead."""
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
        return {
            "internal_entity_id": str(project.entity_id),
            "result": "project assessment updated",
        }

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
        """Record a significant choice, superseding an earlier Decision when applicable."""
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
        """Create a semantic relation from the controlled RelationType catalog."""
        await service.create_relation(command, action_context)
        return "semantic relation present"

    @agent.tool_plain(sequential=True)
    async def infer_relation(command: EntityRelationInference) -> str:
        """Create Elowyn's inferred semantic relation with confidence/provenance."""
        await service.infer_relation(command, evidence_source=action_context.source)
        return "inferred semantic relation present"

    @agent.tool_plain(sequential=True)
    async def undo_last_change(entity_id: str | None = None) -> str:
        """Undo the latest reversible change with a new inverse Event."""
        from uuid import UUID

        await service.undo_last_change(
            action_context,
            entity_id=UUID(entity_id) if entity_id else None,
        )
        return "inverse change recorded"

    return agent
