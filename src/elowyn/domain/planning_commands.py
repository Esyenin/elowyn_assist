from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from elowyn.domain.enums import (
    PlanGoalRole,
    PlanItemProgressStatus,
    PlanVersionBasisRole,
)


class PlanGoalLinkCreate(BaseModel):
    goal_id: UUID
    role: PlanGoalRole = PlanGoalRole.SUPPORTING


class PlanCreate(BaseModel):
    title: str = Field(max_length=500)
    description: str | None = None
    goals: list[PlanGoalLinkCreate] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def unique_goals_and_primary(self) -> PlanCreate:
        ids = [goal.goal_id for goal in self.goals]
        if len(ids) != len(set(ids)):
            raise ValueError("goal links must be unique")
        if sum(goal.role == PlanGoalRole.PRIMARY for goal in self.goals) > 1:
            raise ValueError("a Plan can have at most one PRIMARY Goal")
        return self


class PlanVersionItemCreate(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    ordinal: int = Field(gt=0)
    title: str = Field(max_length=500)
    description: str | None = None
    expected_outcome: str | None = None
    deadline_at: datetime | None = None
    estimated_duration_minutes: int | None = Field(default=None, gt=0)
    linked_task_id: UUID | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("item title cannot be blank")
        return value.strip()


class PlanVersionItemDependencyCreate(BaseModel):
    prerequisite_item_id: UUID
    dependent_item_id: UUID

    @model_validator(mode="after")
    def no_self_dependency(self) -> PlanVersionItemDependencyCreate:
        if self.prerequisite_item_id == self.dependent_item_id:
            raise ValueError("an item cannot depend on itself")
        return self


class PlanVersionBasisCreate(BaseModel):
    entity_id: UUID
    event_id: UUID
    role: PlanVersionBasisRole


class PlanCandidateCreate(BaseModel):
    plan_id: UUID
    summary: str
    rationale: str | None = None
    proposed_strategy_snapshot: str
    strategy_rationale_snapshot: str | None = None
    based_on_version_id: UUID | None = None
    items: list[PlanVersionItemCreate] = Field(default_factory=list)
    dependencies: list[PlanVersionItemDependencyCreate] = Field(default_factory=list)
    basis: list[PlanVersionBasisCreate] = Field(default_factory=list)
    evidence_source_ids: list[UUID] = Field(default_factory=list)
    inference_confidence: float = Field(default=1.0, ge=0, le=1)
    inference_reason_summary: str = Field(
        default="Elowyn synthesized a candidate PlanVersion from recorded evidence",
        max_length=2000,
    )

    @field_validator("summary", "proposed_strategy_snapshot", "inference_reason_summary")
    @classmethod
    def required_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required planning text cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_graph_references(self) -> PlanCandidateCreate:
        item_ids = [item.id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("item identifiers must be unique")
        ordinals = [item.ordinal for item in self.items]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("item ordinals must be unique")
        known = set(item_ids)
        edges: set[tuple[UUID, UUID]] = set()
        for dependency in self.dependencies:
            edge = (dependency.prerequisite_item_id, dependency.dependent_item_id)
            if edge in edges:
                raise ValueError("dependencies must be unique")
            if edge[0] not in known or edge[1] not in known:
                raise ValueError("dependency references an unknown item")
            edges.add(edge)
        basis_keys = {(item.entity_id, item.event_id, item.role) for item in self.basis}
        if len(basis_keys) != len(self.basis):
            raise ValueError("basis references must be unique")
        if len(self.evidence_source_ids) != len(set(self.evidence_source_ids)):
            raise ValueError("evidence sources must be unique")
        return self


class PlanVersionPresentationCreate(BaseModel):
    plan_version_id: UUID
    message_id: UUID


class PlanCandidateReject(BaseModel):
    plan_version_id: UUID


class PlanVersionApprove(BaseModel):
    plan_version_id: UUID


class PlanItemProgressUpdate(BaseModel):
    plan_version_item_id: UUID
    status: PlanItemProgressStatus
    note: str | None = Field(default=None, max_length=4000)
