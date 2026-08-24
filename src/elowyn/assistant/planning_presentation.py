from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import (
    Plan,
    PlanVersion,
    PlanVersionItem,
    PlanVersionItemDependency,
)
from elowyn.domain.errors import DomainValidationError, EntityNotFoundError

_TOKEN_PREFIX = "ELOWYN_PLAN_PRESENTATION"
_TOKEN_PATTERN = re.compile(r"\[\[ELOWYN_PLAN_PRESENTATION:[^\]]+\]\]")


@dataclass(frozen=True)
class PendingPlanPresentation:
    token: str
    plan_version_id: uuid.UUID
    canonical_render: str


@dataclass(frozen=True)
class ResolvedPlanPresentations:
    text: str
    plan_version_ids: tuple[uuid.UUID, ...]


class PlanningTurnState:
    """Ephemeral registry proving which canonical Candidate blocks entered one response."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingPlanPresentation] = {}

    @property
    def pending(self) -> tuple[PendingPlanPresentation, ...]:
        return tuple(self._pending.values())

    def register(self, *, plan_version_id: uuid.UUID, canonical_render: str) -> str:
        if not canonical_render.strip():
            raise DomainValidationError("canonical Plan presentation cannot be blank")
        token = f"[[{_TOKEN_PREFIX}:{uuid.uuid4().hex}]]"
        self._pending[token] = PendingPlanPresentation(
            token=token,
            plan_version_id=plan_version_id,
            canonical_render=canonical_render.strip(),
        )
        return token

    def resolve(self, raw_text: str) -> ResolvedPlanPresentations:
        discovered = _TOKEN_PATTERN.findall(raw_text)
        unknown = [token for token in discovered if token not in self._pending]
        if unknown:
            raise DomainValidationError("assistant response contains an unknown Plan placeholder")
        resolved = raw_text
        version_ids: list[uuid.UUID] = []
        for token, pending in self._pending.items():
            if raw_text.count(token) != 1:
                raise DomainValidationError(
                    "every registered Plan placeholder must appear exactly once in the response"
                )
            resolved = resolved.replace(token, pending.canonical_render)
            version_ids.append(pending.plan_version_id)
        if _TOKEN_PREFIX in resolved:
            raise DomainValidationError("internal Plan placeholder was not fully resolved")
        return ResolvedPlanPresentations(resolved, tuple(version_ids))


def _format_datetime(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


async def render_plan_version(session: AsyncSession, version_id: uuid.UUID) -> str:
    """Render exact immutable Candidate content without exposing internal identifiers."""

    version = await session.get(PlanVersion, version_id)
    if version is None:
        raise EntityNotFoundError(f"PlanVersion {version_id} was not found")
    plan = await session.get(Plan, version.plan_id)
    if plan is None:
        raise EntityNotFoundError(f"Plan {version.plan_id} was not found")
    items = list(
        (
            await session.execute(
                select(PlanVersionItem)
                .where(PlanVersionItem.plan_version_id == version.id)
                .order_by(PlanVersionItem.ordinal, PlanVersionItem.id)
            )
        )
        .scalars()
        .all()
    )
    dependencies = list(
        (
            await session.execute(
                select(PlanVersionItemDependency).where(
                    PlanVersionItemDependency.plan_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    ordinal_by_id = {item.id: item.ordinal for item in items}
    prerequisites: dict[uuid.UUID, list[int]] = {}
    for dependency in dependencies:
        prerequisites.setdefault(dependency.dependent_item_id, []).append(
            ordinal_by_id[dependency.prerequisite_item_id]
        )

    lines = [plan.title, "", "Стратегия:", version.proposed_strategy_snapshot]
    if version.strategy_rationale_snapshot:
        lines.extend(["", "Почему:", version.strategy_rationale_snapshot])
    if version.rationale:
        lines.extend(["", "Обоснование плана:", version.rationale])
    lines.extend(["", "План:"])
    for item in items:
        line = f"{item.ordinal}. {item.title}"
        details: list[str] = []
        if item.description:
            details.append(item.description)
        if item.expected_outcome:
            details.append(f"Результат: {item.expected_outcome}")
        if item.deadline_at:
            details.append(f"Срок: {_format_datetime(item.deadline_at)}")
        if item.estimated_duration_minutes:
            details.append(f"Оценка: {item.estimated_duration_minutes} мин.")
        required = sorted(prerequisites.get(item.id, []))
        if required:
            details.append("После пунктов: " + ", ".join(str(value) for value in required))
        if details:
            line += " — " + "; ".join(details)
        lines.append(line)
    return "\n".join(lines).strip()
