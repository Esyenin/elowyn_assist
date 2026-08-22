from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import (
    Decision,
    DecisionAlternative,
    Entity,
    EntityRelation,
    Goal,
    Project,
    ProjectGoalLink,
    SuccessCriterion,
    Task,
    TaskDependency,
    TaskGoalLink,
)


def _value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, UUID)):
        return str(value)
    return value


class WorldStateQueryService:
    """Read-only structured view used by Elowyn for reference resolution and context."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _active_entity_clause():
        return (Entity.removed_at.is_(None), Entity.superseded_by_entity_id.is_(None))

    async def snapshot(self, *, search_text: str | None = None) -> dict[str, Any]:
        active = self._active_entity_clause()
        tasks = (
            (
                await self.session.execute(
                    select(Task).join(Entity, Entity.id == Task.entity_id).where(*active)
                )
            )
            .scalars()
            .all()
        )
        projects = (
            (
                await self.session.execute(
                    select(Project).join(Entity, Entity.id == Project.entity_id).where(*active)
                )
            )
            .scalars()
            .all()
        )
        goals = (
            (
                await self.session.execute(
                    select(Goal).join(Entity, Entity.id == Goal.entity_id).where(*active)
                )
            )
            .scalars()
            .all()
        )
        decisions = (
            (
                await self.session.execute(
                    select(Decision).join(Entity, Entity.id == Decision.entity_id).where(*active)
                )
            )
            .scalars()
            .all()
        )

        needle = search_text.casefold().strip() if search_text else None
        if needle:
            tasks = [t for t in tasks if needle in f"{t.title} {t.description or ''}".casefold()]
            projects = [
                p for p in projects if needle in f"{p.name} {p.description or ''}".casefold()
            ]
            goals = [g for g in goals if needle in f"{g.title} {g.description or ''}".casefold()]
            decisions = [
                d
                for d in decisions
                if needle in f"{d.title} {d.description or ''} {d.chosen_option}".casefold()
            ]

        task_ids = {t.entity_id for t in tasks}
        project_ids = {p.entity_id for p in projects}
        goal_ids = {g.entity_id for g in goals}
        decision_ids = {d.entity_id for d in decisions}
        visible_ids = task_ids | project_ids | goal_ids | decision_ids

        success_criteria = (
            (
                await self.session.execute(
                    select(SuccessCriterion).where(
                        SuccessCriterion.goal_id.in_(goal_ids) if goal_ids else false()
                    )
                )
            )
            .scalars()
            .all()
        )
        decision_alternatives = (
            (
                await self.session.execute(
                    select(DecisionAlternative).where(
                        DecisionAlternative.decision_id.in_(decision_ids)
                        if decision_ids
                        else false()
                    )
                )
            )
            .scalars()
            .all()
        )

        criteria_by_goal: dict[UUID, list[SuccessCriterion]] = {}
        for criterion in success_criteria:
            criteria_by_goal.setdefault(criterion.goal_id, []).append(criterion)
        alternatives_by_decision: dict[UUID, list[DecisionAlternative]] = {}
        for alternative in decision_alternatives:
            alternatives_by_decision.setdefault(alternative.decision_id, []).append(alternative)

        task_goal_links = (
            (
                await self.session.execute(
                    select(TaskGoalLink).where(
                        TaskGoalLink.task_id.in_(task_ids) if task_ids else false(),
                        TaskGoalLink.goal_id.in_(goal_ids) if goal_ids else false(),
                    )
                )
            )
            .scalars()
            .all()
        )
        project_goal_links = (
            (
                await self.session.execute(
                    select(ProjectGoalLink).where(
                        ProjectGoalLink.project_id.in_(project_ids) if project_ids else false(),
                        ProjectGoalLink.goal_id.in_(goal_ids) if goal_ids else false(),
                    )
                )
            )
            .scalars()
            .all()
        )
        dependencies = (
            (
                await self.session.execute(
                    select(TaskDependency).where(
                        TaskDependency.prerequisite_task_id.in_(task_ids) if task_ids else false(),
                        TaskDependency.dependent_task_id.in_(task_ids) if task_ids else false(),
                    )
                )
            )
            .scalars()
            .all()
        )
        semantic_relations = (
            (
                await self.session.execute(
                    select(EntityRelation).where(
                        EntityRelation.source_entity_id.in_(visible_ids)
                        if visible_ids
                        else false(),
                        EntityRelation.target_entity_id.in_(visible_ids)
                        if visible_ids
                        else false(),
                    )
                )
            )
            .scalars()
            .all()
        )

        return {
            "tasks": [
                {
                    "entity_id": str(t.entity_id),
                    "title": t.title,
                    "description": t.description,
                    "status": _value(t.status),
                    "importance": t.importance,
                    "deadline_at": _value(t.deadline_at),
                    "deadline_type": _value(t.deadline_type),
                    "estimated_duration_minutes": t.estimated_duration_minutes,
                    "parent_task_id": _value(t.parent_task_id),
                    "primary_project_id": _value(t.primary_project_id),
                    "auto_complete_from_children": t.auto_complete_from_children,
                    "completed_at": _value(t.completed_at),
                }
                for t in tasks
            ],
            "projects": [
                {
                    "entity_id": str(p.entity_id),
                    "name": p.name,
                    "description": p.description,
                    "status": _value(p.status),
                    "importance": p.importance,
                    "target_date": _value(p.target_date),
                    "target_date_type": _value(p.target_date_type),
                    "parent_project_id": _value(p.parent_project_id),
                    "current_summary": p.current_summary,
                    "current_summary_updated_at": _value(p.current_summary_updated_at),
                    "completed_at": _value(p.completed_at),
                }
                for p in projects
            ],
            "goals": [
                {
                    "entity_id": str(g.entity_id),
                    "title": g.title,
                    "description": g.description,
                    "status": _value(g.status),
                    "importance": g.importance,
                    "target_date": _value(g.target_date),
                    "target_date_type": _value(g.target_date_type),
                    "parent_goal_id": _value(g.parent_goal_id),
                    "achieved_at": _value(g.achieved_at),
                    "success_criteria": [
                        {
                            "criterion_id": str(c.id),
                            "description": c.description,
                            "status": _value(c.status),
                            "confidence": c.confidence,
                            "evaluation_summary": c.evaluation_summary,
                        }
                        for c in criteria_by_goal.get(g.entity_id, [])
                    ],
                }
                for g in goals
            ],
            "decisions": [
                {
                    "entity_id": str(d.entity_id),
                    "title": d.title,
                    "description": d.description,
                    "status": _value(d.status),
                    "chosen_option": d.chosen_option,
                    "reasoning_summary": d.reasoning_summary,
                    "supersedes_decision_id": _value(d.supersedes_decision_id),
                    "decided_at": _value(d.decided_at),
                    "alternatives": [
                        {
                            "alternative_id": str(a.id),
                            "option_text": a.option_text,
                            "rejection_summary": a.rejection_summary,
                        }
                        for a in alternatives_by_decision.get(d.entity_id, [])
                    ],
                }
                for d in decisions
            ],
            "relations": {
                "task_goal": [
                    {"task_id": str(link.task_id), "goal_id": str(link.goal_id)}
                    for link in task_goal_links
                ],
                "project_goal": [
                    {"project_id": str(link.project_id), "goal_id": str(link.goal_id)}
                    for link in project_goal_links
                ],
                "task_dependencies": [
                    {
                        "prerequisite_task_id": str(dep.prerequisite_task_id),
                        "dependent_task_id": str(dep.dependent_task_id),
                    }
                    for dep in dependencies
                ],
                "semantic": [
                    {
                        "source_entity_id": str(rel.source_entity_id),
                        "target_entity_id": str(rel.target_entity_id),
                        "relation_type": _value(rel.relation_type),
                        "confidence": rel.confidence,
                    }
                    for rel in semantic_relations
                ],
            },
        }

    async def render_for_llm(self, *, search_text: str | None = None) -> str:
        snapshot = await self.snapshot(search_text=search_text)
        return json.dumps(snapshot, ensure_ascii=False, indent=2)
