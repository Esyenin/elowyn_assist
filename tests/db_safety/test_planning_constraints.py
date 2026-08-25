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


async def add_source(connection) -> str:
    source_id = str(uuid4())
    await connection.execute(
        text(
            "INSERT INTO sources (id, source_type, reason_summary) "
            "VALUES (:id, 'SYSTEM', 'test')"
        ),
        {"id": source_id},
    )
    return source_id


async def add_plan(connection) -> str:
    plan_id = await add_entity(connection, "PLAN")
    await connection.execute(
        text("INSERT INTO plans (entity_id, title) VALUES (:id, 'Synthetic plan')"),
        {"id": plan_id},
    )
    return plan_id


async def add_version(
    connection,
    *,
    plan_id: str,
    source_id: str,
    number: int,
    status: str = "CANDIDATE",
    approved: bool = False,
) -> str:
    version_id = str(uuid4())
    await connection.execute(
        text(
            "INSERT INTO plan_versions "
            "(id, plan_id, version_number, status, summary, proposed_strategy_snapshot, "
            "created_source_id, approval_source_id, approved_at) "
            "VALUES (:id, :plan_id, :number, :status, 'Summary', 'Strategy', :source_id, "
            ":approval_source_id, CASE WHEN :approved THEN now() ELSE NULL END)"
        ),
        {
            "id": version_id,
            "plan_id": plan_id,
            "number": number,
            "status": status,
            "source_id": source_id,
            "approval_source_id": source_id if approved else None,
            "approved": approved,
        },
    )
    return version_id


async def test_current_candidate_and_approved_cardinality(safety_engine) -> None:
    async with safety_engine.begin() as connection:
        plan_id = await add_plan(connection)
        source_id = await add_source(connection)
        await add_version(connection, plan_id=plan_id, source_id=source_id, number=1)
        await assert_rejected(
            connection,
            "INSERT INTO plan_versions "
            "(id, plan_id, version_number, status, summary, proposed_strategy_snapshot, "
            "created_source_id) VALUES (:id, :plan, 2, 'CANDIDATE', 'Next', 'Strategy', :source)",
            {"id": str(uuid4()), "plan": plan_id, "source": source_id},
            "uq_plan_version_current_candidate",
        )
        await add_version(
            connection,
            plan_id=plan_id,
            source_id=source_id,
            number=2,
            status="APPROVED",
            approved=True,
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_versions "
            "(id, plan_id, version_number, status, summary, proposed_strategy_snapshot, "
            "created_source_id, approval_source_id, approved_at) "
            "VALUES (:id, :plan, 3, 'APPROVED', 'Other', 'Strategy', :source, :source, now())",
            {"id": str(uuid4()), "plan": plan_id, "source": source_id},
            "uq_plan_version_current_approved",
        )


async def test_version_number_content_and_approval_constraints(safety_engine) -> None:
    async with safety_engine.begin() as connection:
        plan_id = await add_plan(connection)
        source_id = await add_source(connection)
        await add_version(
            connection,
            plan_id=plan_id,
            source_id=source_id,
            number=1,
            status="REJECTED",
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_versions "
            "(id, plan_id, version_number, status, summary, proposed_strategy_snapshot, "
            "created_source_id) VALUES (:id, :plan, 1, 'REJECTED', 'Duplicate', 'S', :source)",
            {"id": str(uuid4()), "plan": plan_id, "source": source_id},
            "uq_plan_version_number",
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_versions "
            "(id, plan_id, version_number, status, summary, proposed_strategy_snapshot, "
            "created_source_id) VALUES (:id, :plan, 0, 'REJECTED', 'Bad', 'S', :source)",
            {"id": str(uuid4()), "plan": plan_id, "source": source_id},
            "ck_plan_version_number_positive",
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_versions "
            "(id, plan_id, version_number, status, summary, proposed_strategy_snapshot, "
            "created_source_id) VALUES (:id, :plan, 2, 'APPROVED', 'Bad', 'S', :source)",
            {"id": str(uuid4()), "plan": plan_id, "source": source_id},
            "ck_plan_version_approved_metadata",
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_versions "
            "(id, plan_id, version_number, status, summary, proposed_strategy_snapshot, "
            "created_source_id, approval_source_id, approved_at) "
            "VALUES (:id, :plan, 2, 'CANDIDATE', 'Bad', 'S', :source, :source, now())",
            {"id": str(uuid4()), "plan": plan_id, "source": source_id},
            "ck_plan_version_unapproved_metadata",
        )


async def test_based_on_version_must_belong_to_same_plan(safety_engine) -> None:
    async with safety_engine.begin() as connection:
        source_id = await add_source(connection)
        first_plan = await add_plan(connection)
        second_plan = await add_plan(connection)
        basis_version = await add_version(
            connection,
            plan_id=first_plan,
            source_id=source_id,
            number=1,
            status="REJECTED",
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_versions "
            "(id, plan_id, version_number, status, summary, proposed_strategy_snapshot, "
            "based_on_version_id, created_source_id) "
            "VALUES (:id, :plan, 1, 'REJECTED', 'Cross-plan', 'Strategy', :basis, :source)",
            {
                "id": str(uuid4()),
                "plan": second_plan,
                "basis": basis_version,
                "source": source_id,
            },
            "fk_plan_version_based_on_same_plan",
        )


async def test_item_shape_and_same_version_dependencies(safety_engine) -> None:
    async with safety_engine.begin() as connection:
        plan_id = await add_plan(connection)
        source_id = await add_source(connection)
        first_version = await add_version(
            connection,
            plan_id=plan_id,
            source_id=source_id,
            number=1,
            status="REJECTED",
        )
        second_version = await add_version(
            connection,
            plan_id=plan_id,
            source_id=source_id,
            number=2,
            status="REJECTED",
        )
        first_item = str(uuid4())
        second_item = str(uuid4())
        other_version_item = str(uuid4())
        for item_id, version_id, ordinal in (
            (first_item, first_version, 1),
            (second_item, first_version, 2),
            (other_version_item, second_version, 1),
        ):
            await connection.execute(
                text(
                    "INSERT INTO plan_version_items "
                    "(id, plan_version_id, ordinal, title, estimated_duration_minutes) "
                    "VALUES (:id, :version, :ordinal, 'Item', 15)"
                ),
                {"id": item_id, "version": version_id, "ordinal": ordinal},
            )
        await assert_rejected(
            connection,
            "INSERT INTO plan_version_items (id, plan_version_id, ordinal, title) "
            "VALUES (:id, :version, 1, 'Duplicate')",
            {"id": str(uuid4()), "version": first_version},
            "uq_plan_version_item_ordinal",
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_version_items "
            "(id, plan_version_id, ordinal, title) "
            "VALUES (:id, :version, 0, 'Bad ordinal')",
            {"id": str(uuid4()), "version": first_version},
            "ck_plan_version_item_ordinal_positive",
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_version_items "
            "(id, plan_version_id, ordinal, title, estimated_duration_minutes) "
            "VALUES (:id, :version, 3, 'Bad duration', 0)",
            {"id": str(uuid4()), "version": first_version},
            "ck_plan_version_item_duration_positive",
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_version_item_dependencies "
            "(plan_version_id, prerequisite_item_id, dependent_item_id) "
            "VALUES (:version, :item, :item)",
            {"version": first_version, "item": first_item},
            "ck_plan_version_item_dependency_not_self",
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_version_item_dependencies "
            "(plan_version_id, prerequisite_item_id, dependent_item_id) "
            "VALUES (:version, :first, :other)",
            {"version": first_version, "first": first_item, "other": other_version_item},
            "fk_plan_item_dependency_dependent",
        )
        await connection.execute(
            text(
                "INSERT INTO plan_version_item_dependencies "
                "(plan_version_id, prerequisite_item_id, dependent_item_id) "
                "VALUES (:version, :first, :second)"
            ),
            {"version": first_version, "first": first_item, "second": second_item},
        )


async def test_only_one_primary_goal_per_plan(safety_engine) -> None:
    async with safety_engine.begin() as connection:
        plan_id = await add_plan(connection)
        source_id = await add_source(connection)
        first_goal = await add_entity(connection, "GOAL")
        second_goal = await add_entity(connection, "GOAL")
        for goal_id in (first_goal, second_goal):
            await connection.execute(
                text("INSERT INTO goals (entity_id, title, status) VALUES (:id, 'Goal', 'ACTIVE')"),
                {"id": goal_id},
            )
        await connection.execute(
            text(
                "INSERT INTO plan_goal_links (plan_id, goal_id, role, source_id) "
                "VALUES (:plan, :goal, 'PRIMARY', :source)"
            ),
            {"plan": plan_id, "goal": first_goal, "source": source_id},
        )
        await assert_rejected(
            connection,
            "INSERT INTO plan_goal_links (plan_id, goal_id, role, source_id) "
            "VALUES (:plan, :goal, 'PRIMARY', :source)",
            {"plan": plan_id, "goal": second_goal, "source": source_id},
            "uq_plan_goal_primary",
        )
        await connection.execute(
            text(
                "INSERT INTO plan_goal_links (plan_id, goal_id, role, source_id) "
                "VALUES (:plan, :goal, 'SUPPORTING', :source)"
            ),
            {"plan": plan_id, "goal": second_goal, "source": source_id},
        )
