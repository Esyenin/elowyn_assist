"""Add evidence-backed observations and compact Memory Pages.

Revision ID: 0004_memory_observations_pages
Revises: 0003_memory_ingestion_state
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from elowyn.db.types import JSON_DATA

revision: str = "0004_memory_observations_pages"
down_revision: str | None = "0003_memory_ingestion_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def upgrade() -> None:
    op.create_table(
        "memory_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_key", sa.String(length=255), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column(
            "category",
            _enum(
                "memory_semantic_category",
                "FACT",
                "PREFERENCE",
                "CONTEXT",
                "IDEA",
                "EPISODE",
                "CONSTRAINT",
                "OBSERVATION",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum(
                "memory_observation_status",
                "CANDIDATE",
                "ACTIVE",
                "CONTESTED",
                "SUPERSEDED",
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "page_type",
            _enum(
                "memory_page_type",
                "USER_PROFILE",
                "COMMUNICATION_PREFERENCES",
                "PROJECT",
                "TOPIC",
            ),
            nullable=False,
        ),
        sa.Column("page_scope_key", sa.String(length=255), nullable=False),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("derivation_version", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(trim(claim_key)) > 0", name="ck_observation_claim_key"),
        sa.CheckConstraint("length(trim(statement)) > 0", name="ck_observation_statement"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_observation_confidence"
        ),
        sa.CheckConstraint(
            "superseded_by_id IS NULL OR superseded_by_id <> id",
            name="ck_observation_not_superseded_by_self",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"], ["memory_observations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_observations_claim_key", "memory_observations", ["claim_key"])
    op.create_index("ix_memory_observations_status", "memory_observations", ["status"])
    op.create_index("ix_memory_observations_page_type", "memory_observations", ["page_type"])
    op.create_index(
        "ix_memory_observations_page_scope_key", "memory_observations", ["page_scope_key"]
    )
    op.create_table(
        "memory_observation_evidence",
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("backend_memory_id", sa.String(length=255), nullable=False),
        sa.Column(
            "stance",
            _enum("memory_evidence_stance", "SUPPORTS", "CONTRADICTS"),
            nullable=False,
        ),
        sa.Column("assertion_text", sa.Text(), nullable=False),
        sa.Column("explicit_correction", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(trim(backend_memory_id)) > 0", name="ck_evidence_backend_id"
        ),
        sa.CheckConstraint("length(trim(assertion_text)) > 0", name="ck_evidence_assertion"),
        sa.ForeignKeyConstraint(
            ["observation_id"], ["memory_observations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("observation_id", "message_id"),
    )
    op.create_table(
        "memory_pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "page_type",
            _enum(
                "memory_page_type",
                "USER_PROFILE",
                "COMMUNICATION_PREFERENCES",
                "PROJECT",
                "TOPIC",
            ),
            nullable=False,
        ),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("entries", JSON_DATA, nullable=False),
        sa.Column("max_entries", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("derivation_version", sa.String(length=100), nullable=False),
        sa.Column(
            "refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(trim(scope_key)) > 0", name="ck_memory_page_scope"),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_memory_page_title"),
        sa.CheckConstraint(
            "max_entries > 0 AND max_entries <= 12", name="ck_memory_page_limit"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page_type", "scope_key", name="uq_memory_page_scope"),
    )
    op.create_index("ix_memory_pages_page_type", "memory_pages", ["page_type"])
    op.create_table(
        "memory_page_observations",
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["page_id"], ["memory_pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["observation_id"], ["memory_observations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("page_id", "observation_id"),
    )


def downgrade() -> None:
    op.drop_table("memory_page_observations")
    op.drop_index("ix_memory_pages_page_type", table_name="memory_pages")
    op.drop_table("memory_pages")
    op.drop_table("memory_observation_evidence")
    op.drop_index("ix_memory_observations_page_scope_key", table_name="memory_observations")
    op.drop_index("ix_memory_observations_page_type", table_name="memory_observations")
    op.drop_index("ix_memory_observations_status", table_name="memory_observations")
    op.drop_index("ix_memory_observations_claim_key", table_name="memory_observations")
    op.drop_table("memory_observations")
