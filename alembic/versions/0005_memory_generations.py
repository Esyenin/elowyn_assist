"""Add rebuild generation journal and active pointer.

Revision ID: 0005_memory_generations
Revises: 0004_memory_observations_pages
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_memory_generations"
down_revision: str | None = "0004_memory_observations_pages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    generation_status = sa.Enum(
        "BUILDING",
        "ACTIVE",
        "SUPERSEDED",
        "FAILED",
        name="memory_generation_status",
        native_enum=False,
    )
    op.create_table(
        "memory_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("backend", sa.String(length=100), nullable=False),
        sa.Column("bank_id", sa.String(length=255), nullable=False),
        sa.Column("status", generation_status, nullable=False),
        sa.Column("messages_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_replayed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_verified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(trim(backend)) > 0", name="ck_memory_generation_backend"
        ),
        sa.CheckConstraint("length(trim(bank_id)) > 0", name="ck_memory_generation_bank"),
        sa.CheckConstraint("messages_total >= 0", name="ck_memory_generation_total"),
        sa.CheckConstraint("messages_replayed >= 0", name="ck_memory_generation_replayed"),
        sa.CheckConstraint("messages_verified >= 0", name="ck_memory_generation_verified"),
        sa.CheckConstraint(
            "messages_replayed <= messages_total", name="ck_memory_generation_progress"
        ),
        sa.CheckConstraint(
            "(status = 'BUILDING' AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'BUILDING' AND lease_expires_at IS NULL)",
            name="ck_memory_generation_lease",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bank_id", name="uq_memory_generation_bank"),
    )
    op.create_index("ix_memory_generations_backend", "memory_generations", ["backend"])
    op.create_index("ix_memory_generations_status", "memory_generations", ["status"])
    op.create_index(
        "ix_memory_generations_lease_expires_at",
        "memory_generations",
        ["lease_expires_at"],
    )
    op.create_table(
        "memory_backend_registries",
        sa.Column("backend", sa.String(length=100), nullable=False),
        sa.Column("active_generation_id", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(trim(backend)) > 0", name="ck_memory_registry_backend"),
        sa.ForeignKeyConstraint(
            ["active_generation_id"], ["memory_generations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("backend"),
    )


def downgrade() -> None:
    op.drop_table("memory_backend_registries")
    op.drop_index("ix_memory_generations_lease_expires_at", table_name="memory_generations")
    op.drop_index("ix_memory_generations_status", table_name="memory_generations")
    op.drop_index("ix_memory_generations_backend", table_name="memory_generations")
    op.drop_table("memory_generations")
