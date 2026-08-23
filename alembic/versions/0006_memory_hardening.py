"""Add ignored-message receipts and durable derived refresh state.

Revision ID: 0006_memory_hardening
Revises: 0005_memory_generations
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_memory_hardening"
down_revision: str | None = "0005_memory_generations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    outcome = sa.Enum(
        "INGESTED",
        "IGNORED_BLANK",
        name="memory_ingestion_outcome",
        native_enum=False,
    )
    op.add_column(
        "memory_ingestion_receipts",
        sa.Column("outcome", outcome, nullable=False, server_default="INGESTED"),
    )
    op.add_column(
        "memory_ingestion_states",
        sa.Column("derived_dirty_through_message_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "memory_ingestion_states",
        sa.Column("derived_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "memory_ingestion_states",
        sa.Column("derived_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memory_ingestion_states",
        sa.Column("derived_last_error", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_memory_ingestion_derived_dirty_message",
        "memory_ingestion_states",
        "messages",
        ["derived_dirty_through_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_memory_ingestion_derived_attempts",
        "memory_ingestion_states",
        "derived_attempts >= 0",
    )
    op.create_index(
        "ix_memory_ingestion_states_derived_next_attempt_at",
        "memory_ingestion_states",
        ["derived_next_attempt_at"],
    )
    # v0.2.0 could confirm retain while losing an opportunistic derived refresh.
    # Conservatively reconcile every previously ingested conversation once.
    op.execute(
        sa.text(
            "UPDATE memory_ingestion_states "
            "SET derived_dirty_through_message_id = last_succeeded_message_id "
            "WHERE last_succeeded_message_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_ingestion_states_derived_next_attempt_at",
        table_name="memory_ingestion_states",
    )
    op.drop_constraint(
        "ck_memory_ingestion_derived_attempts",
        "memory_ingestion_states",
        type_="check",
    )
    op.drop_constraint(
        "fk_memory_ingestion_derived_dirty_message",
        "memory_ingestion_states",
        type_="foreignkey",
    )
    op.drop_column("memory_ingestion_states", "derived_last_error")
    op.drop_column("memory_ingestion_states", "derived_next_attempt_at")
    op.drop_column("memory_ingestion_states", "derived_attempts")
    op.drop_column("memory_ingestion_states", "derived_dirty_through_message_id")
    op.drop_column("memory_ingestion_receipts", "outcome")
