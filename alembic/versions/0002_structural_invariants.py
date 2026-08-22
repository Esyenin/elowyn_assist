"""Strengthen PostgreSQL-enforceable structural invariants.

Revision ID: 0002_structural_invariants
Revises: 0001_initial
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_structural_invariants"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    checks = [
        (
            "entities",
            "ck_entity_not_superseded_by_self",
            "superseded_by_entity_id IS NULL OR superseded_by_entity_id <> id",
        ),
        (
            "conversations",
            "ck_conversation_external_id_not_blank",
            "external_conversation_id IS NULL OR length(trim(external_conversation_id)) > 0",
        ),
        (
            "messages",
            "ck_message_external_id_not_blank",
            "external_message_id IS NULL OR length(trim(external_message_id)) > 0",
        ),
        ("messages", "ck_message_has_content", "text IS NOT NULL OR raw_payload IS NOT NULL"),
        (
            "sources",
            "ck_user_message_source_has_message",
            "source_type <> 'USER_MESSAGE' OR message_id IS NOT NULL",
        ),
        (
            "sources",
            "ck_assistant_inference_has_assessment",
            "source_type <> 'ASSISTANT_INFERENCE' OR "
            "(confidence IS NOT NULL AND reason_summary IS NOT NULL "
            "AND length(trim(reason_summary)) > 0)",
        ),
        (
            "events",
            "ck_event_not_reverse_self",
            "reverses_event_id IS NULL OR reverses_event_id <> id",
        ),
        ("tasks", "ck_task_title_not_blank", "length(trim(title)) > 0"),
        (
            "tasks",
            "ck_task_parent_not_self",
            "parent_task_id IS NULL OR parent_task_id <> entity_id",
        ),
        (
            "tasks",
            "ck_task_deadline_type_requires_date",
            "deadline_type IS NULL OR deadline_at IS NOT NULL",
        ),
        (
            "tasks",
            "ck_task_completion_consistent",
            "(status = 'DONE' AND completed_at IS NOT NULL) OR "
            "(status <> 'DONE' AND completed_at IS NULL)",
        ),
        ("projects", "ck_project_name_not_blank", "length(trim(name)) > 0"),
        (
            "projects",
            "ck_project_parent_not_self",
            "parent_project_id IS NULL OR parent_project_id <> entity_id",
        ),
        (
            "projects",
            "ck_project_target_type_requires_date",
            "target_date_type IS NULL OR target_date IS NOT NULL",
        ),
        (
            "projects",
            "ck_project_completion_consistent",
            "(status = 'COMPLETED' AND completed_at IS NOT NULL) OR "
            "(status <> 'COMPLETED' AND completed_at IS NULL)",
        ),
        (
            "projects",
            "ck_project_summary_cache_consistent",
            "(current_summary IS NULL AND current_summary_updated_at IS NULL) OR "
            "(current_summary IS NOT NULL AND current_summary_updated_at IS NOT NULL)",
        ),
        ("goals", "ck_goal_title_not_blank", "length(trim(title)) > 0"),
        (
            "goals",
            "ck_goal_parent_not_self",
            "parent_goal_id IS NULL OR parent_goal_id <> entity_id",
        ),
        (
            "goals",
            "ck_goal_target_type_requires_date",
            "target_date_type IS NULL OR target_date IS NOT NULL",
        ),
        (
            "goals",
            "ck_goal_achievement_consistent",
            "(status = 'ACHIEVED' AND achieved_at IS NOT NULL) OR "
            "(status <> 'ACHIEVED' AND achieved_at IS NULL)",
        ),
        (
            "success_criteria",
            "ck_success_criterion_description_not_blank",
            "length(trim(description)) > 0",
        ),
        ("decisions", "ck_decision_title_not_blank", "length(trim(title)) > 0"),
        (
            "decisions",
            "ck_decision_chosen_option_not_blank",
            "length(trim(chosen_option)) > 0",
        ),
        (
            "decisions",
            "ck_decision_not_supersede_self",
            "supersedes_decision_id IS NULL OR supersedes_decision_id <> entity_id",
        ),
        (
            "decision_alternatives",
            "ck_decision_alternative_not_blank",
            "length(trim(option_text)) > 0",
        ),
    ]
    for table, name, condition in checks:
        op.create_check_constraint(name, table, condition)
    op.create_unique_constraint(
        "uq_decision_supersedes_once", "decisions", ["supersedes_decision_id"]
    )
    op.create_unique_constraint("uq_event_reversed_once", "events", ["reverses_event_id"])
    op.create_unique_constraint("uq_source_message", "sources", ["message_id"])


def downgrade() -> None:
    op.drop_constraint("uq_source_message", "sources", type_="unique")
    op.drop_constraint("uq_event_reversed_once", "events", type_="unique")
    op.drop_constraint("uq_decision_supersedes_once", "decisions", type_="unique")
    checks = [
        ("decision_alternatives", "ck_decision_alternative_not_blank"),
        ("decisions", "ck_decision_not_supersede_self"),
        ("decisions", "ck_decision_chosen_option_not_blank"),
        ("decisions", "ck_decision_title_not_blank"),
        ("success_criteria", "ck_success_criterion_description_not_blank"),
        ("goals", "ck_goal_achievement_consistent"),
        ("goals", "ck_goal_target_type_requires_date"),
        ("goals", "ck_goal_parent_not_self"),
        ("goals", "ck_goal_title_not_blank"),
        ("projects", "ck_project_summary_cache_consistent"),
        ("projects", "ck_project_completion_consistent"),
        ("projects", "ck_project_target_type_requires_date"),
        ("projects", "ck_project_parent_not_self"),
        ("projects", "ck_project_name_not_blank"),
        ("tasks", "ck_task_completion_consistent"),
        ("tasks", "ck_task_deadline_type_requires_date"),
        ("tasks", "ck_task_parent_not_self"),
        ("tasks", "ck_task_title_not_blank"),
        ("events", "ck_event_not_reverse_self"),
        ("sources", "ck_assistant_inference_has_assessment"),
        ("sources", "ck_user_message_source_has_message"),
        ("messages", "ck_message_has_content"),
        ("messages", "ck_message_external_id_not_blank"),
        ("conversations", "ck_conversation_external_id_not_blank"),
        ("entities", "ck_entity_not_superseded_by_self"),
    ]
    for table, name in checks:
        op.drop_constraint(name, table, type_="check")
