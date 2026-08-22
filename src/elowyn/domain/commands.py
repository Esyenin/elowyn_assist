from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from elowyn.domain.enums import (
    DeadlineType,
    DecisionStatus,
    GoalStatus,
    ProjectStatus,
    RelationType,
    SuccessCriterionStatus,
    TaskStatus,
)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    status: TaskStatus = TaskStatus.TODO
    importance: int | None = Field(default=None, ge=1, le=5)
    deadline_at: datetime | None = None
    deadline_type: DeadlineType | None = None
    estimated_duration_minutes: int | None = Field(default=None, ge=0)
    parent_task_id: UUID | None = None
    primary_project_id: UUID | None = None
    auto_complete_from_children: bool = False
    goal_ids: list[UUID] = Field(default_factory=list)
    prerequisite_task_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def deadline_type_requires_date(self) -> "TaskCreate":
        if self.deadline_type is not None and self.deadline_at is None:
            raise ValueError("deadline_type requires deadline_at")
        return self


class TaskUpdate(BaseModel):
    entity_id: UUID
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status: TaskStatus | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    deadline_at: datetime | None = None
    deadline_type: DeadlineType | None = None
    estimated_duration_minutes: int | None = Field(default=None, ge=0)
    parent_task_id: UUID | None = None
    primary_project_id: UUID | None = None
    auto_complete_from_children: bool | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "TaskUpdate":
        changed = self.model_fields_set - {"entity_id"}
        if not changed:
            raise ValueError("at least one task field must be provided")
        if "title" in changed and (self.title is None or not self.title.strip()):
            raise ValueError("title cannot be empty")
        if (
            "deadline_type" in changed
            and self.deadline_type is not None
            and "deadline_at" in changed
            and self.deadline_at is None
        ):
            raise ValueError("deadline_type requires deadline_at")
        return self


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    description: str | None = None
    status: ProjectStatus = ProjectStatus.PLANNED
    importance: int | None = Field(default=None, ge=1, le=5)
    target_date: datetime | None = None
    target_date_type: DeadlineType | None = None
    parent_project_id: UUID | None = None
    goal_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def target_type_requires_date(self) -> "ProjectCreate":
        if self.target_date_type is not None and self.target_date is None:
            raise ValueError("target_date_type requires target_date")
        return self


class ProjectUpdate(BaseModel):
    entity_id: UUID
    name: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status: ProjectStatus | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    target_date: datetime | None = None
    target_date_type: DeadlineType | None = None
    parent_project_id: UUID | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "ProjectUpdate":
        changed = self.model_fields_set - {"entity_id"}
        if not changed:
            raise ValueError("at least one project field must be provided")
        if "name" in changed and (self.name is None or not self.name.strip()):
            raise ValueError("name cannot be empty")
        if (
            "target_date_type" in changed
            and self.target_date_type is not None
            and "target_date" in changed
            and self.target_date is None
        ):
            raise ValueError("target_date_type requires target_date")
        return self


class SuccessCriterionCreate(BaseModel):
    description: str = Field(min_length=1)


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    status: GoalStatus = GoalStatus.ACTIVE
    importance: int | None = Field(default=None, ge=1, le=5)
    target_date: datetime | None = None
    target_date_type: DeadlineType | None = None
    parent_goal_id: UUID | None = None
    success_criteria: list[SuccessCriterionCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def target_type_requires_date(self) -> "GoalCreate":
        if self.target_date_type is not None and self.target_date is None:
            raise ValueError("target_date_type requires target_date")
        return self


class GoalUpdate(BaseModel):
    entity_id: UUID
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status: GoalStatus | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    target_date: datetime | None = None
    target_date_type: DeadlineType | None = None
    parent_goal_id: UUID | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "GoalUpdate":
        changed = self.model_fields_set - {"entity_id"}
        if not changed:
            raise ValueError("at least one goal field must be provided")
        if "title" in changed and (self.title is None or not self.title.strip()):
            raise ValueError("title cannot be empty")
        if (
            "target_date_type" in changed
            and self.target_date_type is not None
            and "target_date" in changed
            and self.target_date is None
        ):
            raise ValueError("target_date_type requires target_date")
        return self


class DecisionAlternativeCreate(BaseModel):
    option_text: str = Field(min_length=1)
    rejection_summary: str | None = None


class DecisionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    chosen_option: str = Field(min_length=1)
    reasoning_summary: str | None = None
    status: DecisionStatus = DecisionStatus.ACTIVE
    supersedes_decision_id: UUID | None = None
    alternatives: list[DecisionAlternativeCreate] = Field(default_factory=list)


class DecisionRevoke(BaseModel):
    entity_id: UUID
    reason_summary: str | None = None


class TaskGoalLinkCreate(BaseModel):
    task_id: UUID
    goal_id: UUID


class ProjectGoalLinkCreate(BaseModel):
    project_id: UUID
    goal_id: UUID


class TaskDependencyCreate(BaseModel):
    prerequisite_task_id: UUID
    dependent_task_id: UUID

    @model_validator(mode="after")
    def no_self_dependency(self) -> "TaskDependencyCreate":
        if self.prerequisite_task_id == self.dependent_task_id:
            raise ValueError("task cannot depend on itself")
        return self


class EntityRelationCreate(BaseModel):
    source_entity_id: UUID
    target_entity_id: UUID
    relation_type: RelationType

    @model_validator(mode="after")
    def no_self_relation(self) -> "EntityRelationCreate":
        if self.source_entity_id == self.target_entity_id:
            raise ValueError("entity cannot relate to itself")
        return self


class TaskAssessment(BaseModel):
    entity_id: UUID
    importance: int | None = Field(default=None, ge=1, le=5)
    estimated_duration_minutes: int | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0, le=1)
    reason_summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def at_least_one_assessment(self) -> "TaskAssessment":
        if "importance" not in self.model_fields_set and "estimated_duration_minutes" not in self.model_fields_set:
            raise ValueError("importance or estimated_duration_minutes must be provided")
        return self




class ProjectSummaryCacheUpdate(BaseModel):
    entity_id: UUID
    summary: str = Field(min_length=1, max_length=12000)


class ProjectAssessment(BaseModel):
    entity_id: UUID
    importance: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    reason_summary: str = Field(min_length=1, max_length=2000)


class GoalAssessment(BaseModel):
    entity_id: UUID
    importance: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    reason_summary: str = Field(min_length=1, max_length=2000)


class SuccessCriterionUpdate(BaseModel):
    criterion_id: UUID
    description: str | None = None
    status: SuccessCriterionStatus | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evaluation_summary: str | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "SuccessCriterionUpdate":
        changed = self.model_fields_set - {"criterion_id"}
        if not changed:
            raise ValueError("at least one success criterion field must be provided")
        if "description" in changed and (self.description is None or not self.description.strip()):
            raise ValueError("description cannot be empty")
        if "status" in changed and self.status is None:
            raise ValueError("status cannot be null")
        return self


class SuccessCriterionAssessment(BaseModel):
    criterion_id: UUID
    status: SuccessCriterionStatus
    confidence: float = Field(ge=0, le=1)
    evaluation_summary: str = Field(min_length=1, max_length=2000)
    reason_summary: str = Field(min_length=1, max_length=2000)


class EntityRelationInference(BaseModel):
    source_entity_id: UUID
    target_entity_id: UUID
    relation_type: RelationType
    confidence: float = Field(ge=0, le=1)
    reason_summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def no_self_relation(self) -> "EntityRelationInference":
        if self.source_entity_id == self.target_entity_id:
            raise ValueError("entity cannot relate to itself")
        return self
