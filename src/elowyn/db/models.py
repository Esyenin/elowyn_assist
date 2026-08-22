from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from elowyn.db.base import Base
from elowyn.db.types import JSON_DATA, enum_type
from elowyn.domain.enums import (
    ActorType,
    DeadlineType,
    DecisionStatus,
    EntityType,
    EventType,
    GoalStatus,
    MessageAuthor,
    ProjectStatus,
    RelationType,
    SourceType,
    SuccessCriterionStatus,
    TaskStatus,
    TransportType,
)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "superseded_by_entity_id IS NULL OR superseded_by_entity_id <> id",
            name="ck_entity_not_superseded_by_self",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type: Mapped[EntityType] = mapped_column(
        enum_type(EntityType, name="entity_type"), nullable=False, index=True
    )
    superseded_by_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "transport", "external_conversation_id", name="uq_conversation_transport_external"
        ),
        CheckConstraint(
            "external_conversation_id IS NULL OR length(trim(external_conversation_id)) > 0",
            name="ck_conversation_external_id_not_blank",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    transport: Mapped[TransportType] = mapped_column(
        enum_type(TransportType, name="transport_type"), nullable=False
    )
    external_conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    messages: Mapped[list[Message]] = relationship(back_populates="conversation")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "external_message_id", name="uq_message_external"),
        CheckConstraint(
            "external_message_id IS NULL OR length(trim(external_message_id)) > 0",
            name="ck_message_external_id_not_blank",
        ),
        CheckConstraint(
            "text IS NOT NULL OR raw_payload IS NOT NULL", name="ck_message_has_content"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author: Mapped[MessageAuthor] = mapped_column(
        enum_type(MessageAuthor, name="message_author"), nullable=False
    )
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_DATA, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_source_message"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_source_confidence",
        ),
        CheckConstraint(
            "source_type <> 'USER_MESSAGE' OR message_id IS NOT NULL",
            name="ck_user_message_source_has_message",
        ),
        CheckConstraint(
            "source_type <> 'ASSISTANT_INFERENCE' OR "
            "(confidence IS NOT NULL AND reason_summary IS NOT NULL "
            "AND length(trim(reason_summary)) > 0)",
            name="ck_assistant_inference_has_assessment",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_type: Mapped[SourceType] = mapped_column(
        enum_type(SourceType, name="source_type"), nullable=False, index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceDependency(Base):
    __tablename__ = "source_dependencies"
    __table_args__ = (
        CheckConstraint("source_id <> evidence_source_id", name="ck_source_dependency_not_self"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_type: Mapped[ActorType] = mapped_column(
        enum_type(ActorType, name="actor_type"), nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "reverses_event_id IS NULL OR reverses_event_id <> id",
            name="ck_event_not_reverse_self",
        ),
        UniqueConstraint("reverses_event_id", name="uq_event_reversed_once"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[EventType] = mapped_column(
        enum_type(EventType, name="event_type"), nullable=False, index=True
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    reverses_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DATA, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "importance IS NULL OR (importance >= 1 AND importance <= 5)", name="ck_task_importance"
        ),
        CheckConstraint(
            "estimated_duration_minutes IS NULL OR estimated_duration_minutes >= 0",
            name="ck_task_estimate",
        ),
        CheckConstraint("length(trim(title)) > 0", name="ck_task_title_not_blank"),
        CheckConstraint(
            "parent_task_id IS NULL OR parent_task_id <> entity_id",
            name="ck_task_parent_not_self",
        ),
        CheckConstraint(
            "deadline_type IS NULL OR deadline_at IS NOT NULL",
            name="ck_task_deadline_type_requires_date",
        ),
        CheckConstraint(
            "(status = 'DONE' AND completed_at IS NOT NULL) OR "
            "(status <> 'DONE' AND completed_at IS NULL)",
            name="ck_task_completion_consistent",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        enum_type(TaskStatus, name="task_status"),
        default=TaskStatus.TODO,
        nullable=False,
        index=True,
    )
    importance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    importance_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_type: Mapped[DeadlineType | None] = mapped_column(
        enum_type(DeadlineType, name="task_deadline_type"), nullable=True
    )
    estimated_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimate_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.entity_id", ondelete="SET NULL"), nullable=True, index=True
    )
    primary_project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.entity_id", ondelete="SET NULL"), nullable=True, index=True
    )
    auto_complete_from_children: Mapped[bool] = mapped_column(default=False, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "importance IS NULL OR (importance >= 1 AND importance <= 5)",
            name="ck_project_importance",
        ),
        CheckConstraint("length(trim(name)) > 0", name="ck_project_name_not_blank"),
        CheckConstraint(
            "parent_project_id IS NULL OR parent_project_id <> entity_id",
            name="ck_project_parent_not_self",
        ),
        CheckConstraint(
            "target_date_type IS NULL OR target_date IS NOT NULL",
            name="ck_project_target_type_requires_date",
        ),
        CheckConstraint(
            "(status = 'COMPLETED' AND completed_at IS NOT NULL) OR "
            "(status <> 'COMPLETED' AND completed_at IS NULL)",
            name="ck_project_completion_consistent",
        ),
        CheckConstraint(
            "(current_summary IS NULL AND current_summary_updated_at IS NULL) OR "
            "(current_summary IS NOT NULL AND current_summary_updated_at IS NOT NULL)",
            name="ck_project_summary_cache_consistent",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        enum_type(ProjectStatus, name="project_status"),
        default=ProjectStatus.PLANNED,
        nullable=False,
        index=True,
    )
    importance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    importance_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    target_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_date_type: Mapped[DeadlineType | None] = mapped_column(
        enum_type(DeadlineType, name="project_target_date_type"), nullable=True
    )
    parent_project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.entity_id", ondelete="SET NULL"), nullable=True, index=True
    )
    current_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_summary_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Goal(Base):
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint(
            "importance IS NULL OR (importance >= 1 AND importance <= 5)", name="ck_goal_importance"
        ),
        CheckConstraint("length(trim(title)) > 0", name="ck_goal_title_not_blank"),
        CheckConstraint(
            "parent_goal_id IS NULL OR parent_goal_id <> entity_id",
            name="ck_goal_parent_not_self",
        ),
        CheckConstraint(
            "target_date_type IS NULL OR target_date IS NOT NULL",
            name="ck_goal_target_type_requires_date",
        ),
        CheckConstraint(
            "(status = 'ACHIEVED' AND achieved_at IS NOT NULL) OR "
            "(status <> 'ACHIEVED' AND achieved_at IS NULL)",
            name="ck_goal_achievement_consistent",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[GoalStatus] = mapped_column(
        enum_type(GoalStatus, name="goal_status"),
        default=GoalStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    importance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    importance_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    target_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_date_type: Mapped[DeadlineType | None] = mapped_column(
        enum_type(DeadlineType, name="goal_target_date_type"), nullable=True
    )
    parent_goal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("goals.entity_id", ondelete="SET NULL"), nullable=True, index=True
    )
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SuccessCriterion(Base):
    __tablename__ = "success_criteria"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_success_criterion_confidence",
        ),
        CheckConstraint(
            "length(trim(description)) > 0", name="ck_success_criterion_description_not_blank"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    goal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("goals.entity_id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SuccessCriterionStatus] = mapped_column(
        enum_type(SuccessCriterionStatus, name="success_criterion_status"),
        default=SuccessCriterionStatus.UNKNOWN,
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    evaluation_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="ck_decision_title_not_blank"),
        CheckConstraint(
            "length(trim(chosen_option)) > 0", name="ck_decision_chosen_option_not_blank"
        ),
        CheckConstraint(
            "supersedes_decision_id IS NULL OR supersedes_decision_id <> entity_id",
            name="ck_decision_not_supersede_self",
        ),
        UniqueConstraint("supersedes_decision_id", name="uq_decision_supersedes_once"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    chosen_option: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DecisionStatus] = mapped_column(
        enum_type(DecisionStatus, name="decision_status"),
        default=DecisionStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("decisions.entity_id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DecisionAlternative(Base):
    __tablename__ = "decision_alternatives"
    __table_args__ = (
        CheckConstraint("length(trim(option_text)) > 0", name="ck_decision_alternative_not_blank"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("decisions.entity_id", ondelete="CASCADE"), nullable=False, index=True
    )
    option_text: Mapped[str] = mapped_column(Text, nullable=False)
    rejection_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaskGoalLink(Base):
    __tablename__ = "task_goal_links"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_task_goal_confidence",
        ),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.entity_id", ondelete="CASCADE"), primary_key=True
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("goals.entity_id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProjectGoalLink(Base):
    __tablename__ = "project_goal_links"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_project_goal_confidence",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.entity_id", ondelete="CASCADE"), primary_key=True
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("goals.entity_id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (
        CheckConstraint(
            "prerequisite_task_id <> dependent_task_id", name="ck_task_dependency_not_self"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_task_dependency_confidence",
        ),
    )

    prerequisite_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.entity_id", ondelete="CASCADE"), primary_key=True
    )
    dependent_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.entity_id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EntityRelation(Base):
    __tablename__ = "entity_relations"
    __table_args__ = (
        CheckConstraint("source_entity_id <> target_entity_id", name="ck_entity_relation_not_self"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_entity_relation_confidence",
        ),
        UniqueConstraint(
            "source_entity_id", "target_entity_id", "relation_type", name="uq_entity_relation"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[RelationType] = mapped_column(
        enum_type(RelationType, name="relation_type"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
