from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import (
    Decision,
    Event,
    Goal,
    Message,
    Plan,
    PlanGoalLink,
    PlanItemProgress,
    PlanVersion,
    PlanVersionBasis,
    PlanVersionItem,
    PlanVersionItemDependency,
    Project,
    Source,
    SourceDependency,
    Strategy,
    Task,
)
from elowyn.domain.enums import (
    EventType,
    MessageAuthor,
    PlanItemProgressStatus,
    PlanVersionBasisRole,
    PlanVersionStatus,
    SourceType,
)
from elowyn.domain.errors import DomainValidationError, EntityNotFoundError


@dataclass(frozen=True)
class ChangedPlanBasis:
    entity_id: uuid.UUID
    recorded_event_id: uuid.UUID | None
    latest_event_id: uuid.UUID


@dataclass(frozen=True)
class PlanStalenessAssessment:
    plan_version_id: uuid.UUID
    is_stale: bool
    changed_basis: tuple[ChangedPlanBasis, ...]


class PlanningQueryService:
    """Bounded read-only access to Planning state; it never performs domain mutations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def render_for_agent(self, *, max_plans: int = 8, max_items_per_version: int = 20) -> str:
        """Render only current bounded Planning state needed for Candidate routing."""

        if not 1 <= max_plans <= 20 or not 1 <= max_items_per_version <= 50:
            raise DomainValidationError("planning context bounds are invalid")
        plans = list(
            (
                await self.session.execute(
                    select(Plan).order_by(Plan.updated_at.desc(), Plan.entity_id).limit(max_plans)
                )
            )
            .scalars()
            .all()
        )
        payload: list[dict[str, object]] = []
        for plan in plans:
            versions: dict[str, object] = {}
            for label, status in (
                ("current_candidate", PlanVersionStatus.CANDIDATE),
                ("current_approved", PlanVersionStatus.APPROVED),
            ):
                version = await self._current(plan.entity_id, status)
                if version is None:
                    versions[label] = None
                    continue
                items = list(
                    (
                        await self.session.execute(
                            select(PlanVersionItem)
                            .where(PlanVersionItem.plan_version_id == version.id)
                            .order_by(PlanVersionItem.ordinal, PlanVersionItem.id)
                            .limit(max_items_per_version)
                        )
                    )
                    .scalars()
                    .all()
                )
                progress_by_item: dict[uuid.UUID, PlanItemProgress] = {}
                if status == PlanVersionStatus.APPROVED:
                    progress_by_item = {
                        progress.plan_version_item_id: progress
                        for progress in await self.get_item_progress(version.id)
                    }
                staleness = await self.assess_plan_staleness(version.id)
                versions[label] = {
                    "internal_version_id": str(version.id),
                    "summary": version.summary,
                    "proposed_strategy": version.proposed_strategy_snapshot,
                    "items": [
                        {
                            "internal_item_id": str(item.id),
                            "ordinal": item.ordinal,
                            "title": item.title,
                            "progress": (
                                progress_by_item[item.id].status.value
                                if item.id in progress_by_item
                                else None
                            ),
                            "progress_note": (
                                progress_by_item[item.id].note
                                if item.id in progress_by_item
                                else None
                            ),
                        }
                        for item in items
                    ],
                    "is_basis_stale": staleness.is_stale,
                }
            strategy = await self.get_strategy(plan.entity_id)
            goal_links = list(
                (
                    await self.session.execute(
                        select(PlanGoalLink).where(PlanGoalLink.plan_id == plan.entity_id)
                    )
                )
                .scalars()
                .all()
            )
            recent_history = await self.get_plan_history(plan.entity_id, limit=8)
            payload.append(
                {
                    "internal_plan_id": str(plan.entity_id),
                    "title": plan.title,
                    "title_semantics": "LINEAGE_LABEL_NOT_VERSION_DURATION",
                    "recent_version_history": [
                        {
                            "version_number": version.version_number,
                            "status": version.status.value,
                        }
                        for version in recent_history
                    ],
                    "goal_links": [
                        {"internal_goal_id": str(link.goal_id), "role": link.role.value}
                        for link in goal_links
                    ],
                    "current_strategy": strategy.approach if strategy is not None else None,
                    **versions,
                }
            )
        return json.dumps({"plans": payload}, ensure_ascii=False)

    async def get_plan(self, plan_id: uuid.UUID) -> Plan:
        plan = await self.session.get(Plan, plan_id)
        if plan is None:
            raise EntityNotFoundError(f"Plan {plan_id} was not found")
        return plan

    async def list_plans(self, *, limit: int = 20) -> list[Plan]:
        if not 1 <= limit <= 50:
            raise DomainValidationError("Plan list limit must be between 1 and 50")
        return list(
            (
                await self.session.execute(
                    select(Plan).order_by(Plan.updated_at.desc(), Plan.entity_id).limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def get_version(self, version_id: uuid.UUID) -> PlanVersion:
        version = await self.session.get(PlanVersion, version_id)
        if version is None:
            raise EntityNotFoundError(f"PlanVersion {version_id} was not found")
        return version

    async def _current(self, plan_id: uuid.UUID, status: PlanVersionStatus) -> PlanVersion | None:
        await self.get_plan(plan_id)
        return (
            await self.session.execute(
                select(PlanVersion).where(
                    PlanVersion.plan_id == plan_id,
                    PlanVersion.status == status,
                )
            )
        ).scalar_one_or_none()

    async def get_current_candidate(self, plan_id: uuid.UUID) -> PlanVersion | None:
        return await self._current(plan_id, PlanVersionStatus.CANDIDATE)

    async def get_current_approved(self, plan_id: uuid.UUID) -> PlanVersion | None:
        return await self._current(plan_id, PlanVersionStatus.APPROVED)

    async def get_plan_goal_links(self, plan_id: uuid.UUID) -> list[PlanGoalLink]:
        await self.get_plan(plan_id)
        return list(
            (
                await self.session.execute(
                    select(PlanGoalLink)
                    .where(PlanGoalLink.plan_id == plan_id)
                    .order_by(PlanGoalLink.role, PlanGoalLink.goal_id)
                )
            ).scalars()
        )

    async def get_version_basis(self, version_id: uuid.UUID) -> list[PlanVersionBasis]:
        await self.get_version(version_id)
        return list(
            (
                await self.session.execute(
                    select(PlanVersionBasis)
                    .where(PlanVersionBasis.plan_version_id == version_id)
                    .order_by(PlanVersionBasis.role, PlanVersionBasis.entity_id)
                )
            ).scalars()
        )

    async def get_plan_history(self, plan_id: uuid.UUID, *, limit: int = 100) -> list[PlanVersion]:
        if not 1 <= limit <= 500:
            raise DomainValidationError("history limit must be between 1 and 500")
        await self.get_plan(plan_id)
        return list(
            (
                await self.session.execute(
                    select(PlanVersion)
                    .where(PlanVersion.plan_id == plan_id)
                    .order_by(PlanVersion.version_number.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def get_rejected_versions(
        self,
        plan_id: uuid.UUID,
        *,
        duration_days: int | None = None,
        limit: int = 100,
    ) -> list[PlanVersion]:
        """Return canonical rejected history, optionally filtered by stored duration content."""

        rejected = [
            version
            for version in await self.get_plan_history(plan_id, limit=limit)
            if version.status == PlanVersionStatus.REJECTED
        ]
        if duration_days is None:
            return rejected
        matched: list[PlanVersion] = []
        for version in rejected:
            items = await self.get_version_items(version.id)
            content = "\n".join(
                value
                for value in (
                    version.summary,
                    version.rationale,
                    version.proposed_strategy_snapshot,
                    version.strategy_rationale_snapshot,
                    *(item.title for item in items),
                    *(item.description for item in items),
                    *(item.expected_outcome for item in items),
                )
                if value
            )
            if self._content_mentions_duration(content, duration_days):
                matched.append(version)
        return matched

    @staticmethod
    def _content_mentions_duration(content: str, duration_days: int) -> bool:
        normalized = content.casefold().replace("‑", "-").replace("–", "-").replace("—", "-")
        return bool(
            re.search(
                rf"(?<!\d){duration_days}\s*(?:-\s*)?(?:днев\w*|дн(?:я|ей)?)\b",
                normalized,
            )
        )

    async def get_plan_snapshot(self, plan_id: uuid.UUID) -> dict[str, object]:
        plan = await self.get_plan(plan_id)
        strategy = await self.get_strategy(plan_id)
        approved = await self.get_current_approved(plan_id)
        candidate = await self.get_current_candidate(plan_id)
        return {
            "title": plan.title,
            "strategy": (
                {"approach": strategy.approach, "rationale": strategy.rationale}
                if strategy is not None
                else None
            ),
            "approved": (
                await self.get_version_details(approved.id) if approved is not None else None
            ),
            "candidate": (
                await self.get_version_details(candidate.id) if candidate is not None else None
            ),
            "goal_links": [
                {"role": link.role.value, "internal_goal_id": str(link.goal_id)}
                for link in (
                    (
                        await self.session.execute(
                            select(PlanGoalLink).where(PlanGoalLink.plan_id == plan_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            ],
        }

    async def get_version_details(
        self, version_id: uuid.UUID, *, max_items: int = 50, max_evidence: int = 5
    ) -> dict[str, object]:
        if not 1 <= max_items <= 100 or not 1 <= max_evidence <= 20:
            raise DomainValidationError("version detail bounds are invalid")
        version = await self.get_version(version_id)
        items = await self.get_version_items(version_id, limit=max_items)
        dependencies = await self.get_version_dependencies(version_id)
        ordinal_by_id = {item.id: item.ordinal for item in items}
        progress_by_item = {
            progress.plan_version_item_id: progress
            for progress in await self.get_item_progress(version_id)
        }
        evidence = await self._source_evidence(version.created_source_id, limit=max_evidence)
        change_reason = self._change_reason(version, evidence)
        rejection_evidence: list[dict[str, object]] = []
        if version.status == PlanVersionStatus.REJECTED:
            events = (
                await self.session.execute(
                    select(Event).where(
                        Event.entity_id == version.plan_id,
                        Event.event_type == EventType.PLAN_VERSION_REJECTED,
                    )
                )
            ).scalars()
            for event in events:
                if str(version.id) in str(event.changes) and event.source_id is not None:
                    rejection_evidence.extend(
                        await self._source_evidence(event.source_id, limit=max_evidence)
                    )
        staleness = await self.assess_plan_staleness(version.id)
        return {
            "internal_version_id": str(version.id),
            "internal_plan_id": str(version.plan_id),
            "version_number": version.version_number,
            "status": version.status.value,
            "summary": version.summary,
            "rationale": version.rationale,
            "proposed_strategy": version.proposed_strategy_snapshot,
            "strategy_rationale": version.strategy_rationale_snapshot,
            "internal_based_on_version_id": (
                str(version.based_on_version_id)
                if version.based_on_version_id is not None
                else None
            ),
            "items": [
                {
                    "internal_item_id": str(item.id),
                    "ordinal": item.ordinal,
                    "title": item.title,
                    "description": item.description,
                    "expected_outcome": item.expected_outcome,
                    "deadline_at": item.deadline_at.isoformat() if item.deadline_at else None,
                    "estimated_duration_minutes": item.estimated_duration_minutes,
                    "progress": (
                        progress_by_item[item.id].status.value
                        if item.id in progress_by_item
                        else None
                    ),
                    "progress_note": (
                        progress_by_item[item.id].note if item.id in progress_by_item else None
                    ),
                }
                for item in items
            ],
            "dependencies": [
                {
                    "prerequisite_ordinal": ordinal_by_id.get(edge.prerequisite_item_id),
                    "dependent_ordinal": ordinal_by_id.get(edge.dependent_item_id),
                }
                for edge in dependencies
            ],
            "creation_evidence": evidence,
            "change_reason": change_reason,
            "rejection_evidence": rejection_evidence[:max_evidence],
            "is_basis_stale": staleness.is_stale,
        }

    async def get_bounded_history(
        self, plan_id: uuid.UUID, *, limit: int = 5
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 20:
            raise DomainValidationError("read history limit must be between 1 and 20")
        versions = await self.get_plan_history(plan_id, limit=limit)
        return [
            await self.get_version_details(version.id, max_items=20, max_evidence=3)
            for version in versions
        ]

    async def get_approval_activity(
        self, plan_id: uuid.UUID, *, limit: int = 20
    ) -> list[dict[str, object]]:
        """Return chronological activation history without rewriting immutable versions."""

        if not 1 <= limit <= 100:
            raise DomainValidationError("approval activity limit must be between 1 and 100")
        versions = {
            version.id: version for version in await self.get_plan_history(plan_id, limit=500)
        }
        events = list(
            (
                await self.session.execute(
                    select(Event)
                    .where(
                        Event.entity_id == plan_id,
                        Event.event_type == EventType.PLAN_VERSION_APPROVED,
                    )
                    .order_by(Event.created_at, Event.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        result: list[dict[str, object]] = []
        for event in events:
            version_id = self._event_version_id(event.changes)
            version = versions.get(version_id) if version_id is not None else None
            if version is None:
                continue
            evidence = (
                await self._source_evidence(event.source_id, limit=1)
                if event.source_id is not None
                else []
            )
            result.append(
                {
                    "version_number": version.version_number,
                    "activated_at": event.created_at.isoformat(),
                    "user_confirmation": evidence,
                    "reactivated": any(
                        change.get("field") == "reactivated" and change.get("new") is True
                        for change in event.changes
                    ),
                }
            )
        return result

    async def compare_plan_versions(
        self, older_version_id: uuid.UUID, newer_version_id: uuid.UUID
    ) -> dict[str, object]:
        older = await self.get_version(older_version_id)
        newer = await self.get_version(newer_version_id)
        if older.plan_id != newer.plan_id:
            raise DomainValidationError("PlanVersion comparison requires one Plan lineage")
        older_items = await self.get_version_items(older.id)
        newer_items = await self.get_version_items(newer.id)
        old_by_title = {item.title.strip().casefold(): item for item in older_items}
        new_by_title = {item.title.strip().casefold(): item for item in newer_items}
        common = old_by_title.keys() & new_by_title.keys()
        changed_items: list[dict[str, object]] = []
        order_changes: list[dict[str, object]] = []
        for title in sorted(common):
            old_item, new_item = old_by_title[title], new_by_title[title]
            fields = {}
            for field in (
                "description",
                "expected_outcome",
                "deadline_at",
                "estimated_duration_minutes",
            ):
                old_value, new_value = getattr(old_item, field), getattr(new_item, field)
                if old_value != new_value:
                    fields[field] = {"before": old_value, "after": new_value}
            if fields:
                changed_items.append({"title": new_item.title, "fields": fields})
            if old_item.ordinal != new_item.ordinal:
                order_changes.append(
                    {
                        "title": new_item.title,
                        "before": old_item.ordinal,
                        "after": new_item.ordinal,
                    }
                )
        old_dependencies = await self._dependency_labels(older.id, older_items)
        new_dependencies = await self._dependency_labels(newer.id, newer_items)
        newer_evidence = await self._source_evidence(newer.created_source_id, limit=5)
        return {
            "strategy_changed": (
                older.proposed_strategy_snapshot != newer.proposed_strategy_snapshot
                or older.strategy_rationale_snapshot != newer.strategy_rationale_snapshot
            ),
            "strategy_before": older.proposed_strategy_snapshot,
            "strategy_after": newer.proposed_strategy_snapshot,
            "added_items": [
                new_by_title[key].title for key in sorted(new_by_title.keys() - old_by_title.keys())
            ],
            "removed_items": [
                old_by_title[key].title for key in sorted(old_by_title.keys() - new_by_title.keys())
            ],
            "changed_items": changed_items,
            "order_changes": order_changes,
            "dependencies_added": sorted(new_dependencies - old_dependencies),
            "dependencies_removed": sorted(old_dependencies - new_dependencies),
            "newer_change_reason": self._change_reason(newer, newer_evidence),
        }

    async def get_staleness_details(self, version_id: uuid.UUID) -> dict[str, object]:
        assessment = await self.assess_plan_staleness(version_id)
        if not assessment.is_stale:
            return {"is_basis_stale": False, "changed_basis": []}
        basis_by_entity = {
            basis.entity_id: basis
            for basis in (
                await self.session.execute(
                    select(PlanVersionBasis).where(PlanVersionBasis.plan_version_id == version_id)
                )
            ).scalars()
        }
        changed_basis: list[dict[str, object]] = []
        for changed in assessment.changed_basis:
            basis = basis_by_entity.get(changed.entity_id)
            if basis is None:
                changed_basis.append(
                    {
                        "role": PlanVersionBasisRole.GOAL.value,
                        "label": await self._basis_label(
                            changed.entity_id,
                            PlanVersionBasisRole.GOAL,
                        ),
                    }
                )
                continue
            changed_basis.append(
                {
                    "role": basis.role.value,
                    "label": await self._basis_label(basis.entity_id, basis.role),
                }
            )
        return {"is_basis_stale": True, "changed_basis": changed_basis}

    async def _basis_label(self, entity_id: uuid.UUID, role: PlanVersionBasisRole) -> str | None:
        if role == PlanVersionBasisRole.GOAL:
            goal = await self.session.get(Goal, entity_id)
            return goal.title if goal is not None else None
        if role == PlanVersionBasisRole.TASK:
            task = await self.session.get(Task, entity_id)
            return task.title if task is not None else None
        if role == PlanVersionBasisRole.PROJECT:
            project = await self.session.get(Project, entity_id)
            return project.name if project is not None else None
        if role == PlanVersionBasisRole.DECISION:
            decision = await self.session.get(Decision, entity_id)
            return decision.title if decision is not None else None
        if role == PlanVersionBasisRole.STRATEGY:
            strategy = await self.session.get(Strategy, entity_id)
            return strategy.approach if strategy is not None else None
        return None

    async def _dependency_labels(
        self, version_id: uuid.UUID, items: list[PlanVersionItem]
    ) -> set[tuple[str, str]]:
        title_by_id = {item.id: item.title for item in items}
        dependencies = await self.get_version_dependencies(version_id)
        return {
            (title_by_id[edge.prerequisite_item_id], title_by_id[edge.dependent_item_id])
            for edge in dependencies
        }

    async def _source_evidence(
        self, source_id: uuid.UUID, *, limit: int
    ) -> list[dict[str, object]]:
        source = await self.session.get(Source, source_id)
        if source is None:
            return []
        sources = [source]
        if source.source_type != SourceType.USER_MESSAGE:
            dependency_ids = list(
                (
                    await self.session.execute(
                        select(SourceDependency.evidence_source_id).where(
                            SourceDependency.source_id == source.id
                        )
                    )
                ).scalars()
            )
            sources = [
                evidence
                for dependency_id in dependency_ids[:limit]
                if (evidence := await self.session.get(Source, dependency_id)) is not None
            ]
        result: list[dict[str, object]] = []
        for evidence in sources:
            if evidence.message_id is None:
                continue
            message = await self.session.get(Message, evidence.message_id)
            if message is None:
                continue
            result.append(
                {
                    "text": message.text,
                    "occurred_at": message.sent_at.isoformat(),
                    "author": message.author.value,
                }
            )
        return result[:limit]

    @staticmethod
    def _change_reason(
        version: PlanVersion, evidence: list[dict[str, object]]
    ) -> dict[str, object]:
        user_evidence = [
            item for item in evidence if item.get("author") == MessageAuthor.USER.value
        ]
        return {
            "user_trigger": {
                "status": "RECORDED" if user_evidence else "NOT_RECORDED",
                "evidence": user_evidence,
            },
            "assistant_rationale": {
                "plan": version.rationale,
                "strategy": version.strategy_rationale_snapshot,
                "classification": "ASSISTANT_RATIONALE_NOT_USER_MOTIVE",
            },
        }

    @staticmethod
    def _event_version_id(changes: list[dict[str, object]]) -> uuid.UUID | None:
        for item in changes:
            if item.get("field") != "version_id" or item.get("new") is None:
                continue
            try:
                return uuid.UUID(str(item["new"]))
            except ValueError:
                return None
        return None

    async def get_strategy(self, plan_id: uuid.UUID) -> Strategy | None:
        plan = await self.get_plan(plan_id)
        if plan.strategy_id is None:
            return None
        strategy = await self.session.get(Strategy, plan.strategy_id)
        if strategy is None:
            raise DomainValidationError("Plan references a missing Strategy")
        return strategy

    async def get_item_progress(self, version_id: uuid.UUID) -> list[PlanItemProgress]:
        await self.get_version(version_id)
        return list(
            (
                await self.session.execute(
                    select(PlanItemProgress)
                    .join(
                        PlanVersionItem,
                        PlanVersionItem.id == PlanItemProgress.plan_version_item_id,
                    )
                    .where(PlanVersionItem.plan_version_id == version_id)
                    .order_by(PlanVersionItem.ordinal, PlanVersionItem.id)
                )
            )
            .scalars()
            .all()
        )

    async def get_version_items(
        self, version_id: uuid.UUID, *, limit: int = 50
    ) -> list[PlanVersionItem]:
        if not 1 <= limit <= 100:
            raise DomainValidationError("item limit must be between 1 and 100")
        await self.get_version(version_id)
        return list(
            (
                await self.session.execute(
                    select(PlanVersionItem)
                    .where(PlanVersionItem.plan_version_id == version_id)
                    .order_by(PlanVersionItem.ordinal, PlanVersionItem.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def get_version_dependencies(
        self, version_id: uuid.UUID
    ) -> list[PlanVersionItemDependency]:
        await self.get_version(version_id)
        return list(
            (
                await self.session.execute(
                    select(PlanVersionItemDependency).where(
                        PlanVersionItemDependency.plan_version_id == version_id
                    )
                )
            )
            .scalars()
            .all()
        )

    async def get_next_action(self, plan_id: uuid.UUID) -> PlanVersionItem | None:
        approved = await self.get_current_approved(plan_id)
        if approved is None:
            return None
        rows = (
            await self.session.execute(
                select(PlanVersionItem, PlanItemProgress)
                .join(
                    PlanItemProgress,
                    PlanItemProgress.plan_version_item_id == PlanVersionItem.id,
                )
                .where(PlanVersionItem.plan_version_id == approved.id)
                .order_by(PlanVersionItem.ordinal, PlanVersionItem.id)
            )
        ).all()
        in_progress = [
            item for item, progress in rows if progress.status == PlanItemProgressStatus.IN_PROGRESS
        ]
        if in_progress:
            return in_progress[0]

        status_by_item = {item.id: progress.status for item, progress in rows}
        dependencies = (
            await self.session.execute(
                select(PlanVersionItemDependency).where(
                    PlanVersionItemDependency.plan_version_id == approved.id
                )
            )
        ).scalars()
        prerequisites: dict[uuid.UUID, set[uuid.UUID]] = {}
        for dependency in dependencies:
            prerequisites.setdefault(dependency.dependent_item_id, set()).add(
                dependency.prerequisite_item_id
            )
        for item, progress in rows:
            if progress.status != PlanItemProgressStatus.NOT_STARTED:
                continue
            if all(
                status_by_item.get(prerequisite) == PlanItemProgressStatus.DONE
                for prerequisite in prerequisites.get(item.id, set())
            ):
                return item
        return None

    async def assess_plan_staleness(self, version_id: uuid.UUID) -> PlanStalenessAssessment:
        """Compare basis Events using the existing local `(created_at, id)` Event order."""

        version = await self.get_version(version_id)
        basis_rows = list(
            (
                await self.session.execute(
                    select(PlanVersionBasis).where(PlanVersionBasis.plan_version_id == version_id)
                )
            ).scalars()
        )
        changed: list[ChangedPlanBasis] = []
        explicit_entities = {basis.entity_id for basis in basis_rows}
        for basis in basis_rows:
            recorded = await self.session.get(Event, basis.event_id)
            if recorded is None:
                raise DomainValidationError("PlanVersion basis Event is missing")
            newer = (
                await self.session.execute(
                    select(Event)
                    .where(
                        Event.entity_id == basis.entity_id,
                        or_(
                            Event.created_at > recorded.created_at,
                            and_(
                                Event.created_at == recorded.created_at,
                                Event.id > recorded.id,
                            ),
                        ),
                    )
                    .order_by(Event.created_at.desc(), Event.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if newer is not None:
                changed.append(
                    ChangedPlanBasis(
                        entity_id=basis.entity_id,
                        recorded_event_id=basis.event_id,
                        latest_event_id=newer.id,
                    )
                )
        linked_goals = list(
            (
                await self.session.execute(
                    select(PlanGoalLink).where(PlanGoalLink.plan_id == version.plan_id)
                )
            ).scalars()
        )
        for link in linked_goals:
            if link.goal_id in explicit_entities:
                continue
            baseline = (
                await self.session.execute(
                    select(Event)
                    .where(
                        Event.entity_id == link.goal_id,
                        Event.created_at <= version.created_at,
                    )
                    .order_by(Event.created_at.desc(), Event.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            latest = (
                await self.session.execute(
                    select(Event)
                    .where(Event.entity_id == link.goal_id)
                    .order_by(Event.created_at.desc(), Event.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            link_added_after_version = link.created_at > version.created_at
            if latest is not None and (
                link_added_after_version
                or baseline is None
                or (latest.created_at, latest.id) > (baseline.created_at, baseline.id)
            ):
                changed.append(
                    ChangedPlanBasis(
                        entity_id=link.goal_id,
                        recorded_event_id=None if baseline is None else baseline.id,
                        latest_event_id=latest.id,
                    )
                )
        return PlanStalenessAssessment(
            plan_version_id=version_id,
            is_stale=bool(changed),
            changed_basis=tuple(changed),
        )
