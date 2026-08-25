from sqlalchemy import create_engine, inspect

from elowyn.db import models  # noqa: F401
from elowyn.db.base import Base
from elowyn.domain.enums import (
    EntityType,
    EventType,
    PlanGoalRole,
    PlanItemProgressStatus,
    PlanVersionBasisRole,
    PlanVersionStatus,
)

PLANNING_TABLES = {
    "strategies",
    "plans",
    "plan_goal_links",
    "plan_versions",
    "plan_version_items",
    "plan_version_item_dependencies",
    "plan_item_progress",
    "plan_version_presentations",
    "plan_version_basis",
}


def test_planning_metadata_and_enums_match_v03_contract() -> None:
    assert PLANNING_TABLES <= set(Base.metadata.tables)
    assert {EntityType.PLAN, EntityType.STRATEGY} <= set(EntityType)
    assert {item.value for item in PlanVersionStatus} == {
        "CANDIDATE",
        "APPROVED",
        "SUPERSEDED",
        "REJECTED",
    }
    assert {item.value for item in PlanGoalRole} == {"PRIMARY", "SUPPORTING"}
    assert {item.value for item in PlanItemProgressStatus} == {
        "NOT_STARTED",
        "IN_PROGRESS",
        "WAITING",
        "BLOCKED",
        "DONE",
        "SKIPPED",
    }
    assert {item.value for item in PlanVersionBasisRole} == {
        "GOAL",
        "TASK",
        "PROJECT",
        "DECISION",
        "STRATEGY",
    }
    event_type = Base.metadata.tables["events"].c.event_type.type
    assert event_type.length >= len(EventType.PLAN_ITEM_PROGRESS_UPDATED.value)


def test_portable_schema_contains_partial_current_version_indexes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    assert PLANNING_TABLES <= set(inspector.get_table_names())
    version_indexes = {index["name"] for index in inspector.get_indexes("plan_versions")}
    goal_indexes = {index["name"] for index in inspector.get_indexes("plan_goal_links")}
    assert "uq_plan_version_current_candidate" in version_indexes
    assert "uq_plan_version_current_approved" in version_indexes
    assert "uq_plan_goal_primary" in goal_indexes


def test_plan_strategy_is_nullable_but_strategy_acceptance_is_required() -> None:
    plans = Base.metadata.tables["plans"]
    strategies = Base.metadata.tables["strategies"]
    assert plans.c.strategy_id.nullable is True
    assert strategies.c.accepted_from_plan_version_id.nullable is False
    assert strategies.c.accepted_source_id.nullable is False
    assert strategies.c.accepted_at.nullable is False
