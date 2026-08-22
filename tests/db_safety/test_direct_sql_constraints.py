from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres


async def assert_rejected(connection, statement: str, params: dict, constraint: str) -> None:
    savepoint = await connection.begin_nested()
    with pytest.raises(IntegrityError) as caught:
        await connection.execute(text(statement), params)
    assert constraint in str(caught.value.orig)
    await savepoint.rollback()


async def add_entity(connection, entity_type: str) -> str:
    entity_id = str(uuid4())
    await connection.execute(
        text("INSERT INTO entities (id, entity_type) VALUES (:id, :entity_type)"),
        {"id": entity_id, "entity_type": entity_type},
    )
    return entity_id


async def test_identity_message_source_and_event_corruption_is_rejected(safety_engine) -> None:
    async with safety_engine.begin() as connection:
        entity_id = str(uuid4())
        await assert_rejected(
            connection,
            "INSERT INTO entities (id, entity_type, superseded_by_entity_id) "
            "VALUES (:id, 'TASK', :id)",
            {"id": entity_id},
            "ck_entity_not_superseded_by_self",
        )

        conversation_id = str(uuid4())
        await connection.execute(
            text(
                "INSERT INTO conversations (id, transport, external_conversation_id) "
                "VALUES (:id, 'TELEGRAM', 'direct-sql')"
            ),
            {"id": conversation_id},
        )
        await assert_rejected(
            connection,
            "INSERT INTO messages "
            "(id, conversation_id, author, sent_at, text, raw_payload) "
            "VALUES (:id, :conversation_id, 'USER', now(), NULL, NULL)",
            {"id": str(uuid4()), "conversation_id": conversation_id},
            "ck_message_has_content",
        )
        await assert_rejected(
            connection,
            "INSERT INTO sources (id, source_type) VALUES (:id, 'USER_MESSAGE')",
            {"id": str(uuid4())},
            "ck_user_message_source_has_message",
        )
        message_id = str(uuid4())
        await connection.execute(
            text(
                "INSERT INTO messages (id, conversation_id, author, text, sent_at) "
                "VALUES (:id, :conversation_id, 'USER', 'one source', now())"
            ),
            {"id": message_id, "conversation_id": conversation_id},
        )
        await connection.execute(
            text(
                "INSERT INTO sources (id, source_type, message_id) "
                "VALUES (:id, 'USER_MESSAGE', :message_id)"
            ),
            {"id": str(uuid4()), "message_id": message_id},
        )
        await assert_rejected(
            connection,
            "INSERT INTO sources (id, source_type, message_id) "
            "VALUES (:id, 'USER_MESSAGE', :message_id)",
            {"id": str(uuid4()), "message_id": message_id},
            "uq_source_message",
        )
        await assert_rejected(
            connection,
            "INSERT INTO sources (id, source_type, confidence) "
            "VALUES (:id, 'ASSISTANT_INFERENCE', 0.8)",
            {"id": str(uuid4())},
            "ck_assistant_inference_has_assessment",
        )
        await assert_rejected(
            connection,
            "INSERT INTO events (id, operation_id, event_type, changes) "
            "VALUES (:id, :operation_id, 'TASK_UPDATED', '[]'::jsonb)",
            {"id": str(uuid4()), "operation_id": str(uuid4())},
            "events_operation_id_fkey",
        )
        operation_id = str(uuid4())
        target_event_id = str(uuid4())
        await connection.execute(
            text("INSERT INTO operations (id, actor_type) VALUES (:id, 'SYSTEM')"),
            {"id": operation_id},
        )
        await connection.execute(
            text(
                "INSERT INTO events (id, operation_id, event_type, changes) "
                "VALUES (:event_id, :operation_id, 'TASK_UPDATED', '[]'::jsonb)"
            ),
            {"event_id": target_event_id, "operation_id": operation_id},
        )
        await connection.execute(
            text(
                "INSERT INTO events "
                "(id, operation_id, event_type, reverses_event_id, changes) "
                "VALUES (:id, :operation_id, 'UNDO_APPLIED', :target, '[]'::jsonb)"
            ),
            {"id": str(uuid4()), "operation_id": operation_id, "target": target_event_id},
        )
        await assert_rejected(
            connection,
            "INSERT INTO events "
            "(id, operation_id, event_type, reverses_event_id, changes) "
            "VALUES (:id, :operation_id, 'UNDO_APPLIED', :target, '[]'::jsonb)",
            {"id": str(uuid4()), "operation_id": operation_id, "target": target_event_id},
            "uq_event_reversed_once",
        )


async def test_typed_entity_row_constraints_reject_local_corruption(safety_engine) -> None:
    async with safety_engine.begin() as connection:
        task_id = await add_entity(connection, "TASK")
        await assert_rejected(
            connection,
            "INSERT INTO tasks (entity_id, title, status, auto_complete_from_children) "
            "VALUES (:id, '   ', 'TODO', false)",
            {"id": task_id},
            "ck_task_title_not_blank",
        )
        await assert_rejected(
            connection,
            "INSERT INTO tasks "
            "(entity_id, title, status, deadline_type, auto_complete_from_children) "
            "VALUES (:id, 'Task', 'TODO', 'HARD', false)",
            {"id": task_id},
            "ck_task_deadline_type_requires_date",
        )
        await assert_rejected(
            connection,
            "INSERT INTO tasks "
            "(entity_id, title, status, parent_task_id, auto_complete_from_children) "
            "VALUES (:id, 'Task', 'TODO', :id, false)",
            {"id": task_id},
            "ck_task_parent_not_self",
        )
        await assert_rejected(
            connection,
            "INSERT INTO tasks (entity_id, title, status, auto_complete_from_children) "
            "VALUES (:id, 'Task', 'DONE', false)",
            {"id": task_id},
            "ck_task_completion_consistent",
        )

        project_id = await add_entity(connection, "PROJECT")
        await assert_rejected(
            connection,
            "INSERT INTO projects (entity_id, name, status, current_summary) "
            "VALUES (:id, 'Project', 'ACTIVE', 'stale')",
            {"id": project_id},
            "ck_project_summary_cache_consistent",
        )
        await assert_rejected(
            connection,
            "INSERT INTO projects (entity_id, name, status, parent_project_id) "
            "VALUES (:id, 'Project', 'ACTIVE', :id)",
            {"id": project_id},
            "ck_project_parent_not_self",
        )

        goal_id = await add_entity(connection, "GOAL")
        await assert_rejected(
            connection,
            "INSERT INTO goals (entity_id, title, status, achieved_at) "
            "VALUES (:id, 'Goal', 'ACTIVE', now())",
            {"id": goal_id},
            "ck_goal_achievement_consistent",
        )
        await connection.execute(
            text("INSERT INTO goals (entity_id, title, status) VALUES (:id, 'Goal', 'ACTIVE')"),
            {"id": goal_id},
        )
        await assert_rejected(
            connection,
            "INSERT INTO success_criteria (id, goal_id, description, status) "
            "VALUES (:id, :goal_id, '  ', 'UNKNOWN')",
            {"id": str(uuid4()), "goal_id": goal_id},
            "ck_success_criterion_description_not_blank",
        )


async def test_decision_relation_and_dependency_corruption_is_rejected(safety_engine) -> None:
    async with safety_engine.begin() as connection:
        old_id = await add_entity(connection, "DECISION")
        await connection.execute(
            text(
                "INSERT INTO decisions (entity_id, title, chosen_option, status) "
                "VALUES (:id, 'Storage', 'PostgreSQL', 'ACTIVE')"
            ),
            {"id": old_id},
        )
        first_id = await add_entity(connection, "DECISION")
        await connection.execute(
            text(
                "INSERT INTO decisions "
                "(entity_id, title, chosen_option, status, supersedes_decision_id) "
                "VALUES (:id, 'Storage v2', 'PostgreSQL 18', 'ACTIVE', :old_id)"
            ),
            {"id": first_id, "old_id": old_id},
        )
        second_id = await add_entity(connection, "DECISION")
        await assert_rejected(
            connection,
            "INSERT INTO decisions "
            "(entity_id, title, chosen_option, status, supersedes_decision_id) "
            "VALUES (:id, 'Storage v3', 'Other', 'ACTIVE', :old_id)",
            {"id": second_id, "old_id": old_id},
            "uq_decision_supersedes_once",
        )
        await assert_rejected(
            connection,
            "INSERT INTO decision_alternatives (id, decision_id, option_text) "
            "VALUES (:id, :decision_id, '  ')",
            {"id": str(uuid4()), "decision_id": old_id},
            "ck_decision_alternative_not_blank",
        )

        task_a = await add_entity(connection, "TASK")
        task_b = await add_entity(connection, "TASK")
        for task_id in (task_a, task_b):
            await connection.execute(
                text(
                    "INSERT INTO tasks "
                    "(entity_id, title, status, auto_complete_from_children) "
                    "VALUES (:id, :title, 'TODO', false)"
                ),
                {"id": task_id, "title": task_id},
            )
        await assert_rejected(
            connection,
            "INSERT INTO task_dependencies (prerequisite_task_id, dependent_task_id) "
            "VALUES (:id, :id)",
            {"id": task_a},
            "ck_task_dependency_not_self",
        )

        await connection.execute(
            text(
                "INSERT INTO entity_relations "
                "(id, source_entity_id, target_entity_id, relation_type) "
                "VALUES (:id, :source, :target, 'SUPPORTS')"
            ),
            {"id": str(uuid4()), "source": task_a, "target": task_b},
        )
        await assert_rejected(
            connection,
            "INSERT INTO entity_relations "
            "(id, source_entity_id, target_entity_id, relation_type) "
            "VALUES (:id, :source, :target, 'SUPPORTS')",
            {"id": str(uuid4()), "source": task_a, "target": task_b},
            "uq_entity_relation",
        )
        await assert_rejected(
            connection,
            "INSERT INTO entity_relations "
            "(id, source_entity_id, target_entity_id, relation_type) "
            "VALUES (:id, :source, :target, 'MADE_UP')",
            {"id": str(uuid4()), "source": task_b, "target": task_a},
            "relation_type",
        )
