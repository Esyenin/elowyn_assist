from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import Message, PlanVersion, PlanVersionItem, PlanVersionPresentation
from elowyn.domain.enums import MessageAuthor, PlanVersionStatus
from elowyn.domain.errors import DomainValidationError


class CandidateResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    NO_TARGET = "NO_TARGET"
    AMBIGUOUS = "AMBIGUOUS"
    STALE = "STALE"


@dataclass(frozen=True)
class PresentedCandidateResolution:
    status: CandidateResolutionStatus
    plan_version_id: UUID | None = None


class ApprovedTargetStatus(StrEnum):
    RESOLVED = "RESOLVED"
    NO_APPROVED_PLAN = "NO_APPROVED_PLAN"
    AMBIGUOUS_PLAN = "AMBIGUOUS_PLAN"
    NO_ITEM = "NO_ITEM"
    AMBIGUOUS_ITEM = "AMBIGUOUS_ITEM"
    STALE_ITEM = "STALE_ITEM"


@dataclass(frozen=True)
class ApprovedPlanResolution:
    status: ApprovedTargetStatus
    plan_id: UUID | None = None
    plan_version_id: UUID | None = None


@dataclass(frozen=True)
class ApprovedPlanItemResolution(ApprovedPlanResolution):
    plan_version_item_id: UUID | None = None


class PresentedCandidateResolver:
    """Resolve approval/rejection targets from canonical conversation records only."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_immediate(
        self,
        *,
        conversation_id: UUID,
        user_message_id: UUID,
    ) -> PresentedCandidateResolution:
        user_message = await self._user_message(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
        )
        previous_assistant = (
            await self.session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.author == MessageAuthor.ASSISTANT,
                    Message.sent_at <= user_message.sent_at,
                )
                .order_by(Message.sent_at.desc(), Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if previous_assistant is None:
            return PresentedCandidateResolution(CandidateResolutionStatus.NO_TARGET)

        version_ids = set(
            (
                await self.session.execute(
                    select(PlanVersion.id)
                    .join(
                        PlanVersionPresentation,
                        PlanVersionPresentation.plan_version_id == PlanVersion.id,
                    )
                    .where(
                        PlanVersionPresentation.message_id == previous_assistant.id,
                        PlanVersion.status == PlanVersionStatus.CANDIDATE,
                    )
                )
            ).scalars()
        )
        if not version_ids:
            return PresentedCandidateResolution(CandidateResolutionStatus.NO_TARGET)
        if len(version_ids) != 1:
            return PresentedCandidateResolution(CandidateResolutionStatus.AMBIGUOUS)
        return PresentedCandidateResolution(
            CandidateResolutionStatus.RESOLVED,
            next(iter(version_ids)),
        )

    async def resolve_explicit(
        self,
        *,
        conversation_id: UUID,
        user_message_id: UUID,
        plan_version_id: UUID,
    ) -> PresentedCandidateResolution:
        user_message = await self._user_message(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
        )
        version = await self.session.get(PlanVersion, plan_version_id)
        if version is None:
            return PresentedCandidateResolution(CandidateResolutionStatus.NO_TARGET)
        if version.status != PlanVersionStatus.CANDIDATE:
            return PresentedCandidateResolution(CandidateResolutionStatus.STALE)
        presented = (
            await self.session.execute(
                select(PlanVersionPresentation.id)
                .join(Message, Message.id == PlanVersionPresentation.message_id)
                .where(
                    PlanVersionPresentation.plan_version_id == version.id,
                    Message.author == MessageAuthor.ASSISTANT,
                    Message.conversation_id == conversation_id,
                    PlanVersionPresentation.presented_at <= user_message.sent_at,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if presented is None:
            return PresentedCandidateResolution(CandidateResolutionStatus.NO_TARGET)
        return PresentedCandidateResolution(CandidateResolutionStatus.RESOLVED, version.id)

    async def _user_message(self, *, conversation_id: UUID, user_message_id: UUID) -> Message:
        message = await self.session.get(Message, user_message_id)
        if (
            message is None
            or message.author != MessageAuthor.USER
            or message.conversation_id != conversation_id
        ):
            raise DomainValidationError(
                "approval target resolution requires the current user Message"
            )
        return message


class ApprovedPlanItemResolver:
    """Resolve only items belonging to a current Approved PlanVersion."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_plan(self, plan_id: UUID | None = None) -> ApprovedPlanResolution:
        statement = select(PlanVersion).where(
            PlanVersion.status == PlanVersionStatus.APPROVED
        )
        if plan_id is not None:
            statement = statement.where(PlanVersion.plan_id == plan_id)
        versions = list((await self.session.execute(statement)).scalars())
        if not versions:
            return ApprovedPlanResolution(ApprovedTargetStatus.NO_APPROVED_PLAN)
        if len(versions) != 1:
            return ApprovedPlanResolution(ApprovedTargetStatus.AMBIGUOUS_PLAN)
        version = versions[0]
        return ApprovedPlanResolution(
            ApprovedTargetStatus.RESOLVED,
            plan_id=version.plan_id,
            plan_version_id=version.id,
        )

    async def resolve_item(
        self,
        *,
        plan_id: UUID | None = None,
        plan_version_item_id: UUID | None = None,
        ordinal: int | None = None,
        title: str | None = None,
    ) -> ApprovedPlanItemResolution:
        selectors = sum(
            value is not None for value in (plan_version_item_id, ordinal, title)
        )
        if selectors != 1:
            raise DomainValidationError("exactly one Plan item selector is required")
        plan = await self.resolve_plan(plan_id)
        if plan.status != ApprovedTargetStatus.RESOLVED:
            return ApprovedPlanItemResolution(plan.status)
        assert plan.plan_id is not None and plan.plan_version_id is not None

        if plan_version_item_id is not None:
            item = await self.session.get(PlanVersionItem, plan_version_item_id)
            if item is None:
                return ApprovedPlanItemResolution(ApprovedTargetStatus.NO_ITEM)
            if item.plan_version_id != plan.plan_version_id:
                return ApprovedPlanItemResolution(ApprovedTargetStatus.STALE_ITEM)
            matches = [item]
        else:
            statement = select(PlanVersionItem).where(
                PlanVersionItem.plan_version_id == plan.plan_version_id
            )
            if ordinal is not None:
                statement = statement.where(PlanVersionItem.ordinal == ordinal)
            matches = list((await self.session.execute(statement)).scalars())
            if title is not None:
                normalized = title.strip().casefold()
                matches = [item for item in matches if item.title.strip().casefold() == normalized]
        if not matches:
            return ApprovedPlanItemResolution(ApprovedTargetStatus.NO_ITEM)
        if len(matches) != 1:
            return ApprovedPlanItemResolution(ApprovedTargetStatus.AMBIGUOUS_ITEM)
        return ApprovedPlanItemResolution(
            ApprovedTargetStatus.RESOLVED,
            plan_id=plan.plan_id,
            plan_version_id=plan.plan_version_id,
            plan_version_item_id=matches[0].id,
        )
