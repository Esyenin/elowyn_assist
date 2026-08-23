import pytest
from sqlalchemy import create_engine, inspect

from elowyn.db import models  # noqa: F401
from elowyn.db.base import Base
from elowyn.domain.commands import TaskCreate
from elowyn.domain.enums import DeadlineType

EXPECTED_TABLES = {
    "entities",
    "tasks",
    "projects",
    "goals",
    "success_criteria",
    "decisions",
    "decision_alternatives",
    "task_goal_links",
    "project_goal_links",
    "task_dependencies",
    "entity_relations",
    "conversations",
    "messages",
    "sources",
    "source_dependencies",
    "operations",
    "events",
    "conversation_summaries",
    "memory_ingestion_states",
    "memory_ingestion_receipts",
    "memory_backend_registries",
    "memory_generations",
    "memory_observations",
    "memory_observation_evidence",
    "memory_pages",
    "memory_page_observations",
}


def test_metadata_contains_reviewed_v01_tables() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_schema_can_be_created_on_sqlite_for_contract_validation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())


def test_task_deadline_type_requires_deadline() -> None:
    with pytest.raises(ValueError):
        TaskCreate(title="bad", deadline_type=DeadlineType.HARD)


def test_task_importance_range_is_validated() -> None:
    with pytest.raises(ValueError):
        TaskCreate(title="bad", importance=6)
