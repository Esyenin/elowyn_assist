from __future__ import annotations

import re
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


class CurrentCandidateRejectIntent(StrEnum):
    NONE = "NONE"
    AMBIGUOUS = "AMBIGUOUS"
    EXPLICIT = "EXPLICIT"


_EXPLICIT_CURRENT_REJECT = (
    re.compile(
        r"\b(?:отмен\w*|отклон\w*)\b.{0,80}\b(?:текущ\w*|предлож\w*|вариант\w*|"
        r"кандидат\w*|candidate\w*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bне\s+хоч\w*\b.{0,80}\b(?:его|её|это|вариант\w*|кандидат\w*)\b"
        r".{0,80}\bутвержд\w*\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:этот|текущ\w*|предлож\w*)\b.{0,40}\bвариант\w*\b"
        r".{0,40}\bне\s+(?:подход\w*|нуж\w*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:этот|текущ\w*|предлож\w*)\b.{0,40}\bвариант\w*\b"
        r".{0,40}\bне\s+использ\w*\b",
        re.IGNORECASE,
    ),
)
_AMBIGUOUS_REJECT = re.compile(
    r"(?:\bне\s+утвержд\w*\b|"
    r"\b(?:отмен\w*|отклон\w*)\b.{0,40}\b(?:это|его|её)\b|"
    r"^\s*(?:отмени|отклони)\s*[.!?]?\s*$)",
    re.IGNORECASE,
)
_RELATIVE_DEADLINE = re.compile(
    r"\b(?:нужно|надо|должн\w*|срок\w*)\b"
    r".{0,100}\b(?:законч\w*|заверш\w*|прочит\w*|сда\w*)\b"
    r".{0,100}\b(?:через|за)\s+"
    r"(?P<days>\d{1,3}|один|два|три|четыре|пять|шесть|семь)\s+д(?:ень|ня|ней)\b",
    re.IGNORECASE,
)
_DAY_WORDS = {
    "один": 1,
    "два": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
}
_PLAN_STALENESS_QUESTION = re.compile(
    r"(?:\bплан\w*\b.{0,100}\b(?:актуал\w*|устар\w*|соответств\w*)\b|"
    r"\b(?:актуал\w*|устар\w*)\b.{0,100}\bплан\w*\b|"
    r"\bплан\w*\b.{0,100}\bактуальн\w*\s+данн\w*\b)",
    re.IGNORECASE,
)
_COMPACT_PLAN_REQUEST = re.compile(
    r"(?=.*\b(?:план\w*|вариант\w*|его|этот|этот\s+же)\b)"
    r"(?=.*\b(?:короч\w*|кратк\w*|сжато|tldr|tl;dr)\b)",
    re.IGNORECASE,
)
_COLLABORATE_ON_FIRST_ITEM = re.compile(
    r"(?=.*\b(?:сдела\w*|пройд\w*|выполн\w*|разбер\w*)\b)"
    r"(?=.*\b(?:перв\w*|1(?:-й|ый)?)\s+(?:пункт\w*|шаг\w*)\b)"
    r"(?=.*\b(?:вместе|со\s+мной)\b)",
    re.IGNORECASE,
)
_COLLABORATE_ON_NEXT_ITEM = re.compile(
    r"(?=.*\b(?:сдела\w*|пройд\w*|выполн\w*|разбер\w*)\b)"
    r"(?=.*\b(?:следующ\w*|дальш\w*)\s+(?:пункт\w*|шаг\w*)\b)"
    r"(?=.*\b(?:вместе|со\s+мной)\b)",
    re.IGNORECASE,
)
_HISTORICAL_REJECTED_CANDIDATE = re.compile(
    r"(?=.*\b(?:предыдущ\w*|прошл\w*|историческ\w*|отклонён\w*|отклонен\w*|"
    r"что\s+стал\w*)\b)"
    r"(?=.*\b(?:вариант\w*|кандидат\w*|candidate\w*)\b)",
    re.IGNORECASE,
)
_HISTORICAL_DURATION = re.compile(
    r"\b(?:на|за)\s+(?P<days>\d{1,3}|один|два|три|четыре|пять|шесть|семь)\s+"
    r"д(?:ень|ня|ней)\b",
    re.IGNORECASE,
)
_PRESENCE_SMALL_TALK = {
    "ты тут",
    "ты здесь",
    "ты на месте",
    "ты со мной",
    "слышишь меня",
    "are you there",
}


def current_candidate_reject_intent(text: str) -> CurrentCandidateRejectIntent:
    """Classify only deterministic reject-current wording from the current user turn."""

    normalized = " ".join(text.split())
    if any(pattern.search(normalized) for pattern in _EXPLICIT_CURRENT_REJECT):
        return CurrentCandidateRejectIntent.EXPLICIT
    if _AMBIGUOUS_REJECT.search(normalized):
        return CurrentCandidateRejectIntent.AMBIGUOUS
    return CurrentCandidateRejectIntent.NONE


def relative_deadline_basis_days(text: str) -> int | None:
    """Parse an explicit relative deadline fact, not a replanning request."""

    match = _RELATIVE_DEADLINE.search(" ".join(text.split()))
    if match is None:
        return None
    raw = match.group("days").casefold()
    days = int(raw) if raw.isdigit() else _DAY_WORDS[raw]
    return days if 1 <= days <= 365 else None


def is_plan_staleness_question(text: str) -> bool:
    return bool(_PLAN_STALENESS_QUESTION.search(" ".join(text.split())))


def is_compact_plan_request(text: str) -> bool:
    """Recognize a request to re-render, not revise, the current Plan."""

    return bool(_COMPACT_PLAN_REQUEST.search(" ".join(text.split())))


def is_collaborative_first_item_request(text: str) -> bool:
    """Recognize execution help without inferring approval or Progress mutation."""

    return bool(_COLLABORATE_ON_FIRST_ITEM.search(" ".join(text.split())))


def is_collaborative_next_item_request(text: str) -> bool:
    """Recognize a request to work on the canonical next available action."""

    return bool(_COLLABORATE_ON_NEXT_ITEM.search(" ".join(text.split())))


def is_historical_rejected_candidate_question(text: str) -> bool:
    """Distinguish rejected-version history from a command to reject current Candidate."""

    return bool(_HISTORICAL_REJECTED_CANDIDATE.search(" ".join(text.split())))


def historical_candidate_duration_days(text: str) -> int | None:
    """Extract an optional natural-language duration qualifier from a history question."""

    match = _HISTORICAL_DURATION.search(" ".join(text.split()))
    if match is None:
        return None
    raw = match.group("days").casefold()
    days = int(raw) if raw.isdigit() else _DAY_WORDS[raw]
    return days if 1 <= days <= 365 else None


def is_presence_small_talk(text: str) -> bool:
    """Recognize a self-contained presence check that requires no domain context."""

    normalized = re.sub(r"[.!?,;:]+", "", " ".join(text.casefold().split())).strip()
    return normalized in _PRESENCE_SMALL_TALK


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

    async def resolve_current(self) -> PresentedCandidateResolution:
        """Resolve a single canonical current Candidate without conversation history."""

        version_ids = list(
            (
                await self.session.execute(
                    select(PlanVersion.id)
                    .where(PlanVersion.status == PlanVersionStatus.CANDIDATE)
                    .order_by(PlanVersion.created_at.desc(), PlanVersion.id)
                    .limit(2)
                )
            ).scalars()
        )
        if not version_ids:
            return PresentedCandidateResolution(CandidateResolutionStatus.NO_TARGET)
        if len(version_ids) != 1:
            return PresentedCandidateResolution(CandidateResolutionStatus.AMBIGUOUS)
        return PresentedCandidateResolution(
            CandidateResolutionStatus.RESOLVED,
            version_ids[0],
        )

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


class PresentedHistoricalApprovedResolver(PresentedCandidateResolver):
    """Resolve an explicitly shown formerly Approved version without copying it."""

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
                        PlanVersion.status == PlanVersionStatus.SUPERSEDED,
                        PlanVersion.approval_source_id.is_not(None),
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
        if version.status != PlanVersionStatus.SUPERSEDED or version.approval_source_id is None:
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


class ApprovedPlanItemResolver:
    """Resolve only items belonging to a current Approved PlanVersion."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_plan(self, plan_id: UUID | None = None) -> ApprovedPlanResolution:
        statement = select(PlanVersion).where(PlanVersion.status == PlanVersionStatus.APPROVED)
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
        selectors = sum(value is not None for value in (plan_version_item_id, ordinal, title))
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
