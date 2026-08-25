from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.assistant.planning_presentation import PlanningTurnState, render_plan_version
from elowyn.db.base import Base
from elowyn.db.models import (
    Entity,
    Plan,
    PlanVersion,
    PlanVersionItem,
    PlanVersionItemDependency,
    Source,
)
from elowyn.domain.enums import EntityType, PlanVersionStatus, SourceType
from elowyn.domain.errors import DomainValidationError


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


async def seed_renderable_version(session):
    plan_id = uuid4()
    version_id = uuid4()
    source_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    session.add_all(
        [
            Entity(id=plan_id, entity_type=EntityType.PLAN),
            Plan(entity_id=plan_id, title="Переезд без внутренних ID"),
            Source(
                id=source_id,
                source_type=SourceType.ASSISTANT_INFERENCE,
                confidence=1.0,
                reason_summary="synthetic render fixture",
            ),
        ]
    )
    await session.flush()
    session.add(
        PlanVersion(
            id=version_id,
            plan_id=plan_id,
            version_number=1,
            status=PlanVersionStatus.CANDIDATE,
            summary="Конкретный план переезда",
            rationale="Сначала уменьшаем неопределённость",
            proposed_strategy_snapshot="Проверить варианты до необратимых решений",
            strategy_rationale_snapshot="Так сохраняется свобода выбора",
            created_source_id=source_id,
        )
    )
    await session.flush()
    session.add_all(
        [
            PlanVersionItem(
                id=second_id,
                plan_version_id=version_id,
                ordinal=2,
                title="Сравнить предложения",
                expected_outcome="Короткий список",
                estimated_duration_minutes=45,
            ),
            PlanVersionItem(
                id=first_id,
                plan_version_id=version_id,
                ordinal=1,
                title="Собрать требования",
                description="Записать обязательные условия",
                deadline_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            ),
        ]
    )
    await session.flush()
    session.add(
        PlanVersionItemDependency(
            plan_version_id=version_id,
            prerequisite_item_id=first_id,
            dependent_item_id=second_id,
        )
    )
    await session.flush()
    return version_id, (plan_id, source_id, first_id, second_id)


async def test_renderer_uses_exact_content_order_and_hides_internal_ids(session) -> None:
    version_id, internal_ids = await seed_renderable_version(session)
    first = await render_plan_version(session, version_id)
    second = await render_plan_version(session, version_id)
    assert first == second
    assert "Проверить варианты до необратимых решений" in first
    assert "Так сохраняется свобода выбора" in first
    assert first.index("1. Собрать требования") < first.index("2. Сравнить предложения")
    assert "2026-09-01T12:00" in first
    assert "45 мин." in first
    assert "После пункта 1" in first
    assert "может не отражать срок этой версии" in first
    assert "План (2 пункта):" in first
    for internal_id in (version_id, *internal_ids):
        assert str(internal_id) not in first


async def test_compact_renderer_uses_same_version_without_full_details(session) -> None:
    version_id, _ = await seed_renderable_version(session)

    compact = await render_plan_version(session, version_id, compact=True)

    assert "План — версия 1 (предложенная)" in compact
    assert "Пункты (2):" in compact
    assert "1. Собрать требования" in compact
    assert "2. Сравнить предложения" in compact
    assert "Записать обязательные условия" not in compact
    assert "Обоснование плана:" not in compact


async def test_renderer_never_synthesizes_phase_ranges_or_duration_from_lineage_title(
    session,
) -> None:
    version_id, _ = await seed_renderable_version(session)
    items = list(
        (
            await session.execute(
                select(PlanVersionItem).where(PlanVersionItem.plan_version_id == version_id)
            )
        ).scalars()
    )
    items[0].title = "Этап: дни 1–7"
    items[1].title = "Этап: дни 8–21"
    stored_plan = (await session.execute(select(Plan))).scalar_one()
    stored_plan.title = "Прочитать книгу за 2 недели"
    await session.flush()

    rendered = await render_plan_version(session, version_id)

    assert (
        "Название линии (может не отражать срок этой версии): "
        "Прочитать книгу за 2 недели"
    ) in rendered
    assert "Этап: дни 1–7" in rendered
    assert "Этап: дни 8–21" in rendered
    assert "дни 1–21" not in rendered


def test_turn_state_resolves_registered_placeholder_exactly_once() -> None:
    state = PlanningTurnState()
    version_id = uuid4()
    token = state.register(plan_version_id=version_id, canonical_render="Стратегия:\nПлан")
    resolved = state.resolve(f"Предлагаю:\n{token}\nОбсудим детали.")
    assert resolved.text == "Предлагаю:\nСтратегия:\nПлан\nОбсудим детали."
    assert resolved.plan_version_ids == (version_id,)
    assert "ELOWYN_PLAN_PRESENTATION" not in resolved.text


@pytest.mark.parametrize(
    "response",
    [
        "placeholder отсутствует",
        "{token}\n{token}",
        "[[ELOWYN_PLAN_PRESENTATION:invented]]",
    ],
)
def test_turn_state_rejects_missing_duplicate_or_unknown_placeholder(response: str) -> None:
    state = PlanningTurnState()
    token = state.register(plan_version_id=uuid4(), canonical_render="План")
    with pytest.raises(DomainValidationError):
        state.resolve(response.format(token=token))
