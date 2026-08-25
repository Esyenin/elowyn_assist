"""Add the canonical Planning v0.3 schema.

Revision ID: 0007_planning_v03
Revises: 0006_memory_hardening
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_planning_v03"
down_revision: str | None = "0006_memory_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTITY_TYPES = ("TASK", "PROJECT", "GOAL", "DECISION", "PLAN", "STRATEGY")
V01_EVENT_TYPES = (
    "ENTITY_CREATED",
    "ENTITY_UPDATED",
    "ENTITY_SUPERSEDED",
    "TASK_CREATED",
    "TASK_UPDATED",
    "TASK_STATUS_CHANGED",
    "TASK_COMPLETED",
    "TASK_CANCELLED",
    "PROJECT_CREATED",
    "PROJECT_UPDATED",
    "PROJECT_STATUS_CHANGED",
    "PROJECT_COMPLETED",
    "PROJECT_CANCELLED",
    "GOAL_CREATED",
    "GOAL_UPDATED",
    "GOAL_STATUS_CHANGED",
    "GOAL_ACHIEVED",
    "SUCCESS_CRITERION_UPDATED",
    "DECISION_CREATED",
    "DECISION_SUPERSEDED",
    "DECISION_REVOKED",
    "RELATION_CREATED",
    "RELATION_REMOVED",
    "UNDO_APPLIED",
)
PLANNING_EVENT_TYPES = (
    "PLAN_CREATED",
    "PLAN_VERSION_CREATED",
    "PLAN_VERSION_PRESENTED",
    "PLAN_VERSION_APPROVED",
    "PLAN_VERSION_REJECTED",
    "PLAN_VERSION_SUPERSEDED",
    "PLAN_ITEM_PROGRESS_UPDATED",
    "STRATEGY_CREATED",
    "STRATEGY_ACCEPTED",
    "PLAN_GOAL_LINKED",
)


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def _allowed(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    op.drop_constraint("entity_type", "entities", type_="check")
    op.create_check_constraint("entity_type", "entities", _allowed("entity_type", ENTITY_TYPES))
    op.drop_constraint("event_type", "events", type_="check")
    op.alter_column(
        "events",
        "event_type",
        existing_type=sa.String(length=25),
        type_=sa.String(length=26),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "event_type",
        "events",
        _allowed("event_type", V01_EVENT_TYPES + PLANNING_EVENT_TYPES),
    )

    op.create_table(
        "strategies",
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("approach", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("accepted_from_plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("accepted_source_id", sa.Uuid(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(trim(approach)) > 0", name="ck_strategy_approach_not_blank"
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["accepted_source_id"], ["sources.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("entity_id"),
    )
    op.create_table(
        "plans",
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_plan_title_not_blank"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["strategy_id"], ["strategies.entity_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("entity_id"),
    )
    op.create_index("ix_plans_strategy_id", "plans", ["strategy_id"])
    op.create_table(
        "plan_goal_links",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("goal_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role", _enum("plan_goal_role", "PRIMARY", "SUPPORTING"), nullable=False
        ),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.entity_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.entity_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("plan_id", "goal_id"),
    )
    op.create_index(
        "uq_plan_goal_primary",
        "plan_goal_links",
        ["plan_id"],
        unique=True,
        postgresql_where=sa.text("role = 'PRIMARY'"),
    )
    op.create_table(
        "plan_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            _enum(
                "plan_version_status",
                "CANDIDATE",
                "APPROVED",
                "SUPERSEDED",
                "REJECTED",
            ),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("proposed_strategy_snapshot", sa.Text(), nullable=False),
        sa.Column("strategy_rationale_snapshot", sa.Text(), nullable=True),
        sa.Column("based_on_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_source_id", sa.Uuid(), nullable=False),
        sa.Column("approval_source_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version_number > 0", name="ck_plan_version_number_positive"),
        sa.CheckConstraint(
            "length(trim(summary)) > 0", name="ck_plan_version_summary_not_blank"
        ),
        sa.CheckConstraint(
            "length(trim(proposed_strategy_snapshot)) > 0",
            name="ck_plan_version_strategy_not_blank",
        ),
        sa.CheckConstraint(
            "based_on_version_id IS NULL OR based_on_version_id <> id",
            name="ck_plan_version_not_based_on_self",
        ),
        sa.CheckConstraint(
            "(approval_source_id IS NULL AND approved_at IS NULL) OR "
            "(approval_source_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_plan_version_approval_pair",
        ),
        sa.CheckConstraint(
            "status <> 'APPROVED' OR "
            "(approval_source_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_plan_version_approved_metadata",
        ),
        sa.CheckConstraint(
            "status NOT IN ('CANDIDATE', 'REJECTED') OR "
            "(approval_source_id IS NULL AND approved_at IS NULL)",
            name="ck_plan_version_unapproved_metadata",
        ),
        sa.ForeignKeyConstraint(
            ["approval_source_id"], ["sources.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "based_on_version_id"],
            ["plan_versions.plan_id", "plan_versions.id"],
            name="fk_plan_version_based_on_same_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_source_id"], ["sources.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.entity_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "version_number", name="uq_plan_version_number"),
        sa.UniqueConstraint("plan_id", "id", name="uq_plan_version_identity"),
    )
    op.create_index("ix_plan_versions_plan_id", "plan_versions", ["plan_id"])
    op.create_index("ix_plan_versions_status", "plan_versions", ["status"])
    op.create_index(
        "uq_plan_version_current_candidate",
        "plan_versions",
        ["plan_id"],
        unique=True,
        postgresql_where=sa.text("status = 'CANDIDATE'"),
    )
    op.create_index(
        "uq_plan_version_current_approved",
        "plan_versions",
        ["plan_id"],
        unique=True,
        postgresql_where=sa.text("status = 'APPROVED'"),
    )
    op.create_foreign_key(
        "fk_strategies_accepted_plan_version",
        "strategies",
        "plan_versions",
        ["accepted_from_plan_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "plan_version_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("expected_outcome", sa.Text(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("linked_task_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("ordinal > 0", name="ck_plan_version_item_ordinal_positive"),
        sa.CheckConstraint(
            "length(trim(title)) > 0", name="ck_plan_version_item_title_not_blank"
        ),
        sa.CheckConstraint(
            "estimated_duration_minutes IS NULL OR estimated_duration_minutes > 0",
            name="ck_plan_version_item_duration_positive",
        ),
        sa.ForeignKeyConstraint(
            ["linked_task_id"], ["tasks.entity_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"], ["plan_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_version_id", "id", name="uq_plan_version_item_identity"
        ),
        sa.UniqueConstraint(
            "plan_version_id", "ordinal", name="uq_plan_version_item_ordinal"
        ),
    )
    op.create_index(
        "ix_plan_version_items_plan_version_id", "plan_version_items", ["plan_version_id"]
    )
    op.create_index(
        "ix_plan_version_items_linked_task_id", "plan_version_items", ["linked_task_id"]
    )
    op.create_table(
        "plan_version_item_dependencies",
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("prerequisite_item_id", sa.Uuid(), nullable=False),
        sa.Column("dependent_item_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "prerequisite_item_id <> dependent_item_id",
            name="ck_plan_version_item_dependency_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id", "dependent_item_id"],
            ["plan_version_items.plan_version_id", "plan_version_items.id"],
            name="fk_plan_item_dependency_dependent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id", "prerequisite_item_id"],
            ["plan_version_items.plan_version_id", "plan_version_items.id"],
            name="fk_plan_item_dependency_prerequisite",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "plan_version_id", "prerequisite_item_id", "dependent_item_id"
        ),
    )
    op.create_table(
        "plan_item_progress",
        sa.Column("plan_version_item_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            _enum(
                "plan_item_progress_status",
                "NOT_STARTED",
                "IN_PROGRESS",
                "WAITING",
                "BLOCKED",
                "DONE",
                "SKIPPED",
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_item_id"], ["plan_version_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("plan_version_item_id"),
    )
    op.create_table(
        "plan_version_presentations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column(
            "presented_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["plan_version_id"], ["plan_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_version_id", "message_id", name="uq_plan_version_presentation_message"
        ),
    )
    op.create_index(
        "ix_plan_version_presentations_plan_version_id",
        "plan_version_presentations",
        ["plan_version_id"],
    )
    op.create_index(
        "ix_plan_version_presentations_message_id",
        "plan_version_presentations",
        ["message_id"],
    )
    op.create_table(
        "plan_version_basis",
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            _enum(
                "plan_version_basis_role",
                "GOAL",
                "TASK",
                "PROJECT",
                "DECISION",
                "STRATEGY",
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["plan_version_id"], ["plan_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("plan_version_id", "entity_id", "event_id", "role"),
    )


def downgrade() -> None:
    op.drop_table("plan_version_basis")
    op.drop_index(
        "ix_plan_version_presentations_message_id", table_name="plan_version_presentations"
    )
    op.drop_index(
        "ix_plan_version_presentations_plan_version_id",
        table_name="plan_version_presentations",
    )
    op.drop_table("plan_version_presentations")
    op.drop_table("plan_item_progress")
    op.drop_table("plan_version_item_dependencies")
    op.drop_index("ix_plan_version_items_linked_task_id", table_name="plan_version_items")
    op.drop_index("ix_plan_version_items_plan_version_id", table_name="plan_version_items")
    op.drop_table("plan_version_items")
    op.drop_constraint(
        "fk_strategies_accepted_plan_version", "strategies", type_="foreignkey"
    )
    op.drop_index("uq_plan_version_current_approved", table_name="plan_versions")
    op.drop_index("uq_plan_version_current_candidate", table_name="plan_versions")
    op.drop_index("ix_plan_versions_status", table_name="plan_versions")
    op.drop_index("ix_plan_versions_plan_id", table_name="plan_versions")
    op.drop_table("plan_versions")
    op.drop_index("uq_plan_goal_primary", table_name="plan_goal_links")
    op.drop_table("plan_goal_links")
    op.drop_index("ix_plans_strategy_id", table_name="plans")
    op.drop_table("plans")
    op.drop_table("strategies")

    op.execute(
        sa.text(
            "DELETE FROM events WHERE event_type IN ("
            + ", ".join(f"'{value}'" for value in PLANNING_EVENT_TYPES)
            + ")"
        )
    )
    op.execute(sa.text("DELETE FROM entities WHERE entity_type IN ('PLAN', 'STRATEGY')"))
    op.drop_constraint("event_type", "events", type_="check")
    op.create_check_constraint("event_type", "events", _allowed("event_type", V01_EVENT_TYPES))
    op.alter_column(
        "events",
        "event_type",
        existing_type=sa.String(length=26),
        type_=sa.String(length=25),
        existing_nullable=False,
    )
    op.drop_constraint("entity_type", "entities", type_="check")
    op.create_check_constraint(
        "entity_type", "entities", _allowed("entity_type", ENTITY_TYPES[:-2])
    )
