"""Add Core-owned conversation summary and memory ingestion cursor.

Revision ID: 0003_memory_ingestion_state
Revises: 0002_structural_invariants
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from elowyn.db.types import JSON_DATA

revision: str = "0003_memory_ingestion_state"
down_revision: str | None = "0002_structural_invariants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_summaries",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("short_summary", sa.Text(), nullable=False),
        sa.Column("topics", JSON_DATA, nullable=False),
        sa.Column("related_entity_ids", JSON_DATA, nullable=False),
        sa.Column("last_processed_message_id", sa.Uuid(), nullable=True),
        sa.Column("derivation_version", sa.String(length=100), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(trim(short_summary)) > 0", name="ck_summary_not_blank"),
        sa.CheckConstraint(
            "length(trim(derivation_version)) > 0", name="ck_summary_version_not_blank"
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_processed_message_id"], ["messages.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_table(
        "memory_ingestion_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("backend", sa.String(length=100), nullable=False),
        sa.Column("last_succeeded_message_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "IDLE", "PROCESSING", "FAILED", name="memory_ingestion_status", native_enum=False
            ),
            nullable=False,
            server_default="IDLE",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(trim(backend)) > 0", name="ck_memory_backend_not_blank"),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_ingestion_attempts"),
        sa.CheckConstraint(
            "(status = 'PROCESSING' AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'PROCESSING' AND lease_expires_at IS NULL)",
            name="ck_memory_ingestion_lease_consistent",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_succeeded_message_id"], ["messages.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "backend", name="uq_memory_ingestion_backend"),
    )
    op.create_index(
        op.f("ix_memory_ingestion_states_conversation_id"),
        "memory_ingestion_states",
        ["conversation_id"],
    )
    op.create_index(
        op.f("ix_memory_ingestion_states_next_attempt_at"),
        "memory_ingestion_states",
        ["next_attempt_at"],
    )
    op.create_index(
        op.f("ix_memory_ingestion_states_status"), "memory_ingestion_states", ["status"]
    )
    op.create_table(
        "memory_ingestion_receipts",
        sa.Column("state_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column(
            "succeeded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["state_id"], ["memory_ingestion_states.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("state_id", "message_id"),
    )


def downgrade() -> None:
    op.drop_table("memory_ingestion_receipts")
    op.drop_index(op.f("ix_memory_ingestion_states_status"), table_name="memory_ingestion_states")
    op.drop_index(
        op.f("ix_memory_ingestion_states_next_attempt_at"), table_name="memory_ingestion_states"
    )
    op.drop_index(
        op.f("ix_memory_ingestion_states_conversation_id"),
        table_name="memory_ingestion_states",
    )
    op.drop_table("memory_ingestion_states")
    op.drop_table("conversation_summaries")
