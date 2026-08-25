from __future__ import annotations

import json
import re
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from elowyn.assistant.planning_presentation import PlanningTurnState, render_plan_version
from elowyn.assistant.planning_resolution import (
    ApprovedPlanItemResolver,
    ApprovedTargetStatus,
    CandidateResolutionStatus,
    CurrentCandidateRejectIntent,
    PresentedCandidateResolver,
    PresentedHistoricalApprovedResolver,
    current_candidate_reject_intent,
)
from elowyn.db.models import Event, Message
from elowyn.domain.enums import MessageAuthor, PlanItemProgressStatus, PlanVersionStatus
from elowyn.domain.planning_commands import (
    HistoricalPlanVersionReactivate,
    PlanCandidateCreate,
    PlanCandidateReject,
    PlanCreate,
    PlanItemProgressUpdate,
    PlanVersionApprove,
    PlanVersionBasisCreate,
    PlanVersionItemCreate,
    PlanVersionItemDependencyCreate,
)
from elowyn.services.domain_mutation import ActionContext
from elowyn.services.planning import PlanningService
from elowyn.services.planning_query import PlanningQueryService


class CandidateProposal(BaseModel):
    summary: str
    rationale: str | None = None
    proposed_strategy_snapshot: str
    strategy_rationale_snapshot: str | None = None
    based_on_version_id: UUID | None = None
    items: list[PlanVersionItemCreate] = Field(default_factory=list)
    dependencies: list[PlanVersionItemDependencyCreate] = Field(default_factory=list)
    basis: list[PlanVersionBasisCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_domain_shape(self) -> CandidateProposal:
        PlanCandidateCreate(plan_id=uuid4(), **self.model_dump(exclude={"plan_id"}))
        return self

    def to_domain(self, plan_id: UUID) -> PlanCandidateCreate:
        return PlanCandidateCreate(plan_id=plan_id, **self.model_dump(exclude={"plan_id"}))


class CreatePlanWithCandidateProposal(BaseModel):
    plan: PlanCreate
    candidate: CandidateProposal

    @field_validator("plan", "candidate", mode="before")
    @classmethod
    def decode_stringified_object(cls, value: object) -> object:
        """Normalize an OpenAI-compatible provider quirk before strict validation."""

        if isinstance(value, str):
            value = json.loads(value)
            if not isinstance(value, dict):
                raise ValueError("nested planning tool arguments must be JSON objects")
        return value

    @model_validator(mode="after")
    def new_plan_cannot_have_basis_version(self) -> CreatePlanWithCandidateProposal:
        if self.candidate.based_on_version_id is not None:
            raise ValueError("a new Plan Candidate cannot be based on another Plan's version")
        return self


class PresentCandidatePlanProposal(CandidateProposal):
    plan_id: UUID


class PresentedCandidateTarget(BaseModel):
    plan_version_id: UUID | None = None


class PresentedHistoricalPlanTarget(BaseModel):
    plan_version_id: UUID | None = None


class ApprovedPlanItemTarget(BaseModel):
    plan_id: UUID | None = None
    plan_version_item_id: UUID | None = None
    ordinal: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def exactly_one_item_selector(self) -> ApprovedPlanItemTarget:
        selectors = (self.plan_version_item_id, self.ordinal, self.title)
        if sum(value is not None for value in selectors) != 1:
            raise ValueError("exactly one item selector is required")
        return self


class ProgressUpdateProposal(ApprovedPlanItemTarget):
    status: PlanItemProgressStatus
    note: str | None = Field(default=None, max_length=500)


class NextActionProposal(BaseModel):
    plan_id: UUID | None = None


class CurrentPlanRead(BaseModel):
    plan_id: UUID | None = None


class PlanHistoryRead(CurrentPlanRead):
    limit: int = Field(default=5, ge=1, le=20)


class PlanVersionRead(BaseModel):
    plan_version_id: UUID


class PlanVersionCompareRead(BaseModel):
    older_plan_version_id: UUID
    newer_plan_version_id: UUID


EXPLAINABILITY_ANSWER_CONTRACT = (
    "For why-changed answers, state recorded user_trigger evidence first and quote/paraphrase "
    "only that evidence as the factual reason. Label assistant_rationale separately as Elowyn's "
    "proposed reasoning, never as the user's motive. If user_trigger.status is NOT_RECORDED, "
    "say that the historical reason was not recorded; do not infer one."
)


_REPLANNING_ACTION = re.compile(
    r"\b(?:"
    r"перестро\w*|перепланир\w*|пересобер\w*|пересобир\w*|"
    r"предлож\w*|состав\w*|подготов\w*|созда\w*|сдела\w*|дай|дайте|"
    r"обнов\w*|измен\w*|исправ\w*|передела\w*|переработ\w*|скорректир\w*|"
    r"убер\w*|убир\w*|удал\w*|добав\w*|замен\w*|остав\w*|"
    r"replan\w*|revise\w*|update\w*|propose\w*|draft\w*|create\w*|change\w*"
    r")\b",
    re.IGNORECASE,
)
_REPLANNING_TARGET = re.compile(
    r"\b(?:план\w*|вариант\w*|верси\w*|пункт\w*|шаг\w*|этап\w*|график\w*|расписан\w*|"
    r"plan\w*|version\w*|variant\w*|item\w*|step\w*|schedule\w*)\b",
    re.IGNORECASE,
)
_UNAMBIGUOUS_REPLANNING_ACTION = re.compile(
    r"\b(?:перестро\w*|перепланир\w*|пересобер\w*|пересобир\w*|replan\w*)\b",
    re.IGNORECASE,
)
_DIRECT_PLAN_CONTENT_MUTATION = re.compile(
    r"\b(?:убер\w*|убир\w*|удал\w*|добав\w*|замен\w*|остав\w*|"
    r"сократ\w*|продл\w*|растян\w*)\b",
    re.IGNORECASE,
)
_DIRECTIVE_PLAN_ACTIVITY = re.compile(
    r"\bдавай\b.{0,80}\b(?:читать|заниматься|делать|работать|готовиться)\b",
    re.IGNORECASE,
)
_SHOW_ACTION = re.compile(r"\b(?:покаж\w*|show\w*)\b", re.IGNORECASE)
_MULTIPLE_VARIANTS = re.compile(
    r"\b(?:два|две|три|нескольк\w*|разн\w*|two|three|multiple|different)\b",
    re.IGNORECASE,
)
_VARIANT_TARGET = re.compile(r"\b(?:вариант\w*|variant\w*)\b", re.IGNORECASE)


def has_explicit_replanning_intent(text: str) -> bool:
    """Return whether this utterance itself explicitly requests a Plan revision."""

    normalized = " ".join(text.split())
    return bool(
        _UNAMBIGUOUS_REPLANNING_ACTION.search(normalized)
        or _DIRECT_PLAN_CONTENT_MUTATION.search(normalized)
        or _DIRECTIVE_PLAN_ACTIVITY.search(normalized)
        or (_REPLANNING_ACTION.search(normalized) and _REPLANNING_TARGET.search(normalized))
        or (
            _SHOW_ACTION.search(normalized)
            and _MULTIPLE_VARIANTS.search(normalized)
            and _VARIANT_TARGET.search(normalized)
        )
    )


def planning_policy() -> str:
    return """

PLANNING v0.3 — CANDIDATE И ЕСТЕСТВЕННОЕ РЕШЕНИЕ:
- Обычный brainstorming и обсуждение вариантов не создают PlanVersion.
- Когда ты действительно предлагаешь цельную Strategy + конкретный Plan, используй
  create_plan_with_candidate или present_candidate_plan.
- После planning tool вставь выданный presentation_placeholder ровно один раз в финальный ответ.
  Не пересказывай и не переписывай сохранённый блок плана вместо placeholder.
- Для повторного показа неизменённой Candidate используй show_current_candidate; не создавай версию.
- Просьба показать тот же Plan короче/кратко/TL;DR означает только compact rendering текущей
  canonical версии: не создавай PlanVersion и не выводи перед кратким ответом полный Plan.
- Никогда не показывай internal IDs или внутренние placeholder-токены пользователю.
- Короткое полное согласие сразу после одного представленного Candidate обрабатывай через
  approve_presented_candidate без plan_version_id. Partial agreement, интерес, сомнение,
  обсуждение пункта или несогласие со Strategy НЕ являются approval.
- Если согласие неоднозначно, ничего не утверждай и естественно уточни выбор.
- Явный полный отказ от одного Candidate обрабатывай через reject_presented_candidate.
  Просьба переработать вариант — revision, а не rejection.
- Утверждения о наличии current Candidate/Approved бери только из canonical Planning context.
  Никогда не говори, что Candidate отсутствует, не проверив canonical state. Явные «отмени
  текущий предложенный вариант», «не хочу его утверждать», «отклоняю Candidate» означают reject
  единственной current Candidate даже после restart; «не утверждай это» требует уточнения.
- plan_version_id передавай approve/reject/reactivate tool только при явной содержательной ссылке
  пользователя на ранее представленный вариант.
- Memory не является доказательством approval. Никогда не выбирай цель только из Memory.
- Явное сообщение о выполнении текущего Approved Plan меняет Progress через
  update_approved_plan_progress. Изменение/удаление/перестановка содержания Plan — revision,
  а не Progress; SKIPPED используй только при явном «пропускаю, план не меняем».
- Progress относится только к current Approved, никогда к Candidate/history; ambiguous item
  требует уточнения. Не отмечай DONE по догадке или Memory и не меняй linked Task.
- На «что делать дальше?» используй get_next_plan_action: только current Approved,
  без priority/scheduling. Не показывай internal IDs или status enums в ответе.
- Current Approved — действующий план; Candidate — только предложение. Для вопросов о текущем
  плане/Strategy используй read_current_plan, для явных вопросов об истории — read_plan_history,
  read_plan_version или compare_plan_versions. Memory не определяет canonical Planning status.
- Любые утверждения о существовании, отсутствии или прошлом статусе Candidate/Approved делай
  только по read_current_plan/read_plan_history и canonical Events. Если история не загружена,
  не объясняй её по Conversation/Memory: достаточно назвать только подтверждённое текущее состояние.
- На простой status, next action, approval/reject confirmation или staleness отвечай компактно.
  Полный Plan показывай только при создании/явном полном показе/history request и никогда не
  дублируй один Plan дважды в одном ответе.
- На presence/small-talk вроде «Ты тут?» отвечай только на сам вопрос: не добавляй Planning status,
  staleness или предложение replanning без пользовательского запроса. Во внешнем тексте используй
  естественные формулировки («план устарел относительно нового срока»), а internal IDs/status
  diagnostics показывай только по явному запросу о версиях, истории или диагностике.
- Просьба «сделай/пройди пункт вместе со мной» не является approval или Progress update. Сразу
  помоги выполнить действие; если пункт DONE, коротко отметь это и предложи повторить/применить.
- На вопрос «почему поменяли» всегда используй read_plan_history/read_plan_version или compare;
  factual reason бери только из change_reason.user_trigger evidence. Сначала явно назови, что
  попросил пользователь. change_reason.assistant_rationale можно добавить только отдельно как
  обоснование Elowyn, никогда не как мотив пользователя. При NOT_RECORDED прямо скажи, что причина
  в истории не зафиксирована; не додумывай её из свойств плана, Memory или rationale.
- Basis передавай только с точными canonical entity_id + event_id, уже полученными из Core.
  Никогда не придумывай event_id; если точного Event нет в контексте, оставь basis пустым.
- assess_plan_staleness_read сообщает только изменение basis: stale не означает invalid.
  Basis change сам по себе НЕ является просьбой перепланировать. Не создавай Candidate и не
  пересматривай Strategy автоматически из-за нового факта, срока, ограничения, доступности
  ресурса или staleness. Сохрани canonical basis, сообщи о mismatch и спроси, предложить ли
  обновлённый вариант. Только явная просьба в ТЕКУЩЕМ user message (например «перестрой план»,
  «предложи новый вариант») разрешает revision; прежний planning context разрешением не является.
- Historical Approved для возможного возврата показывай через show_historical_plan_for_return:
  это не создаёт Candidate. После отдельного явного подтверждения используй
  reactivate_presented_historical_plan: current Approved становится тот же PlanVersion с прежним
  identity/content. Новую Candidate based_on него создавай только при последующей просьбе изменить
  содержание.
- Plan approval не создаёт и не изменяет Task, Project, Goal или Decision автоматически.
"""


async def _register_render(
    *,
    service: PlanningService,
    state: PlanningTurnState,
    version_id: UUID,
) -> dict[str, str]:
    rendering = await render_plan_version(service.session, version_id)
    token = state.register(plan_version_id=version_id, canonical_render=rendering)
    return {
        "presentation_placeholder": token,
        "instruction": "Insert presentation_placeholder exactly once in the final response.",
    }


def register_planning_tools(
    agent,
    *,
    service: PlanningService,
    query_service: PlanningQueryService,
    action_context: ActionContext,
    turn_state: PlanningTurnState,
    conversation_id: UUID,
    user_message_id: UUID,
) -> None:
    resolver = PresentedCandidateResolver(service.session)
    historical_resolver = PresentedHistoricalApprovedResolver(service.session)
    item_resolver = ApprovedPlanItemResolver(service.session)

    async def invalid_basis_result(
        basis: list[PlanVersionBasisCreate],
    ) -> dict[str, str] | None:
        for reference in basis:
            event = await service.session.get(Event, reference.event_id)
            if event is None or event.entity_id != reference.entity_id:
                return {
                    "result": "candidate_not_created",
                    "reason": "invalid_basis",
                    "instruction": (
                        "Retry the Candidate without basis. Never invent event_id; include basis "
                        "only when an exact canonical Entity/Event pair is available."
                    ),
                }
        return None

    async def implicit_replanning_result(
        *, only_when_plan_exists: bool = False
    ) -> dict[str, str] | None:
        if only_when_plan_exists and not await query_service.list_plans(limit=1):
            return None
        message = await service.session.get(Message, user_message_id)
        if (
            message is not None
            and message.author == MessageAuthor.USER
            and message.text is not None
            and has_explicit_replanning_intent(message.text)
        ):
            return None
        return {
            "result": "candidate_not_created",
            "reason": "replanning_intent_not_explicit",
            "instruction": (
                "Do not create or revise a Candidate. Treat the basis change as possible "
                "staleness, explain that the current Approved Plan remains active, and ask "
                "whether the user wants an updated Candidate. Planning history is not consent."
            ),
        }

    async def resolve_read_plan_id(requested: UUID | None) -> tuple[UUID | None, str | None]:
        if requested is not None:
            await query_service.get_plan(requested)
            return requested, None
        plans = await query_service.list_plans(limit=2)
        if not plans:
            return None, "NO_PLAN"
        if len(plans) != 1:
            return None, "AMBIGUOUS_PLAN"
        return plans[0].entity_id, None

    async def resolve_target(command: PresentedCandidateTarget):
        if command.plan_version_id is None:
            return await resolver.resolve_immediate(
                conversation_id=conversation_id,
                user_message_id=user_message_id,
            )
        return await resolver.resolve_explicit(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            plan_version_id=command.plan_version_id,
        )

    async def resolve_historical_target(command: PresentedHistoricalPlanTarget):
        if command.plan_version_id is None:
            return await historical_resolver.resolve_immediate(
                conversation_id=conversation_id,
                user_message_id=user_message_id,
            )
        return await historical_resolver.resolve_explicit(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            plan_version_id=command.plan_version_id,
        )

    @agent.tool_plain(sequential=True)
    async def create_plan_with_candidate(
        command: CreatePlanWithCandidateProposal,
    ) -> dict[str, str]:
        """Create a Plan lineage and a complete Candidate only when presenting a concrete plan."""

        if implicit := await implicit_replanning_result(only_when_plan_exists=True):
            return implicit
        if invalid := await invalid_basis_result(command.candidate.basis):
            return invalid
        plan = await service.create_plan(command.plan, action_context)
        version = await service.create_candidate_version(
            command.candidate.to_domain(plan.entity_id),
            action_context,
        )
        result = await _register_render(service=service, state=turn_state, version_id=version.id)
        result["internal_plan_id"] = str(plan.entity_id)
        return result

    @agent.tool_plain(sequential=True)
    async def present_candidate_plan(command: PresentCandidatePlanProposal) -> dict[str, str]:
        """Create/revise and present one complete Candidate for an existing Plan lineage."""

        if implicit := await implicit_replanning_result():
            return implicit
        if invalid := await invalid_basis_result(command.basis):
            return invalid
        version = await service.create_candidate_version(
            command.to_domain(command.plan_id),
            action_context,
        )
        return await _register_render(
            service=service,
            state=turn_state,
            version_id=version.id,
        )

    @agent.tool_plain(sequential=True)
    async def show_current_candidate(plan_id: UUID) -> dict[str, str]:
        """Present the unchanged current Candidate again without creating a PlanVersion."""

        version = await query_service.get_current_candidate(plan_id)
        if version is None:
            raise ValueError("Plan has no current Candidate to present")
        return await _register_render(
            service=service,
            state=turn_state,
            version_id=version.id,
        )

    @agent.tool_plain(sequential=True)
    async def show_historical_plan_for_return(command: PlanVersionRead) -> dict[str, str]:
        """Canonically show one formerly Approved version without creating a copy."""

        version = await query_service.get_version(command.plan_version_id)
        if version.status != PlanVersionStatus.SUPERSEDED or version.approval_source_id is None:
            return {
                "result": "historical_return_not_presented",
                "reason": "NOT_FORMERLY_APPROVED",
            }
        result = await _register_render(
            service=service,
            state=turn_state,
            version_id=version.id,
        )
        result["result"] = "historical_approved_presented_for_confirmation"
        result["instruction"] = (
            "Present this exact historical version and ask for explicit confirmation; "
            "do not create a Candidate or claim it is active yet."
        )
        return result

    @agent.tool_plain(sequential=True)
    async def approve_presented_candidate(
        command: PresentedCandidateTarget,
    ) -> dict[str, str]:
        """Approve the deterministically resolved, previously presented current Candidate."""

        resolution = await resolve_target(command)
        if resolution.status != CandidateResolutionStatus.RESOLVED:
            return {
                "result": "approval_not_applied",
                "reason": resolution.status.value,
                "instruction": "Ask the user to clarify; do not claim that a Plan was approved.",
            }
        assert resolution.plan_version_id is not None
        await service.approve_plan_version(
            PlanVersionApprove(plan_version_id=resolution.plan_version_id),
            action_context,
        )
        return {"result": "plan_approved"}

    @agent.tool_plain(sequential=True)
    async def reactivate_presented_historical_plan(
        command: PresentedHistoricalPlanTarget,
    ) -> dict[str, str]:
        """Reactivate the exact formerly Approved version after explicit confirmation."""

        resolution = await resolve_historical_target(command)
        if resolution.status != CandidateResolutionStatus.RESOLVED:
            return {
                "result": "historical_plan_not_reactivated",
                "reason": resolution.status.value,
                "instruction": "Ask the user to clarify; do not claim the Plan was changed.",
            }
        assert resolution.plan_version_id is not None
        await service.reactivate_historical_plan_version(
            HistoricalPlanVersionReactivate(plan_version_id=resolution.plan_version_id),
            action_context,
        )
        return {
            "result": "historical_plan_reactivated",
            "instruction": (
                "Acknowledge that the exact historical version is current again; "
                "do not describe it as a new version."
            ),
        }

    @agent.tool_plain(sequential=True)
    async def reject_presented_candidate(
        command: PresentedCandidateTarget,
    ) -> dict[str, str]:
        """Reject the deterministically resolved, previously presented current Candidate."""

        resolution = await resolve_target(command)
        if (
            command.plan_version_id is None
            and resolution.status == CandidateResolutionStatus.NO_TARGET
        ):
            message = await service.session.get(Message, user_message_id)
            if (
                message is not None
                and message.text is not None
                and current_candidate_reject_intent(message.text)
                == CurrentCandidateRejectIntent.EXPLICIT
            ):
                resolution = await resolver.resolve_current()
        if resolution.status != CandidateResolutionStatus.RESOLVED:
            return {
                "result": "rejection_not_applied",
                "reason": resolution.status.value,
                "instruction": (
                    "Ask the user to clarify; do not claim that a Candidate was rejected."
                ),
            }
        assert resolution.plan_version_id is not None
        await service.reject_candidate_version(
            PlanCandidateReject(plan_version_id=resolution.plan_version_id),
            action_context,
        )
        return {"result": "candidate_rejected"}

    @agent.tool_plain(sequential=True)
    async def update_approved_plan_progress(
        command: ProgressUpdateProposal,
    ) -> dict[str, str]:
        """Update execution state of one unambiguous current Approved Plan item."""

        resolution = await item_resolver.resolve_item(
            plan_id=command.plan_id,
            plan_version_item_id=command.plan_version_item_id,
            ordinal=command.ordinal,
            title=command.title,
        )
        if resolution.status != ApprovedTargetStatus.RESOLVED:
            return {
                "result": "progress_not_applied",
                "reason": resolution.status.value,
                "instruction": "Ask the user to identify one current Approved Plan item.",
            }
        assert resolution.plan_version_item_id is not None
        await service.update_plan_item_progress(
            PlanItemProgressUpdate(
                plan_version_item_id=resolution.plan_version_item_id,
                status=command.status,
                note=command.note,
            ),
            action_context,
        )
        return {"result": "progress_updated"}

    @agent.tool_plain(sequential=True)
    async def get_next_plan_action(command: NextActionProposal) -> dict[str, object]:
        """Read the deterministic next action from one current Approved Plan."""

        plan_resolution = await item_resolver.resolve_plan(command.plan_id)
        if plan_resolution.status != ApprovedTargetStatus.RESOLVED:
            return {
                "result": "no_working_plan",
                "reason": plan_resolution.status.value,
                "instruction": "Explain naturally; never treat a Candidate as Approved.",
            }
        assert plan_resolution.plan_id is not None
        assert plan_resolution.plan_version_id is not None
        item = await query_service.get_next_action(plan_resolution.plan_id)
        if item is not None:
            return {
                "result": "next_action",
                "ordinal": item.ordinal,
                "title": item.title,
                "description": item.description,
            }

        items = await query_service.get_version_items(plan_resolution.plan_version_id)
        progress = await query_service.get_item_progress(plan_resolution.plan_version_id)
        dependencies = await query_service.get_version_dependencies(plan_resolution.plan_version_id)
        ordinal_by_id = {item.id: item.ordinal for item in items}
        progress_by_id = {entry.plan_version_item_id: entry for entry in progress}
        return {
            "result": "no_available_action",
            "items": [
                {
                    "ordinal": item.ordinal,
                    "title": item.title,
                    "status": progress_by_id[item.id].status.value,
                }
                for item in items
            ],
            "dependencies": [
                {
                    "prerequisite_ordinal": ordinal_by_id[edge.prerequisite_item_id],
                    "dependent_ordinal": ordinal_by_id[edge.dependent_item_id],
                }
                for edge in dependencies
            ],
            "instruction": "Explain naturally why no next action is currently available.",
        }

    @agent.tool_plain(sequential=True)
    async def read_current_plan(command: CurrentPlanRead) -> dict[str, object]:
        """Read current Strategy, Approved, Candidate and Approved Progress without history dump."""

        plan_id, reason = await resolve_read_plan_id(command.plan_id)
        if plan_id is None:
            return {"result": "plan_not_resolved", "reason": reason}
        return {
            "result": "current_plan",
            "plan": await query_service.get_plan_snapshot(plan_id),
            "explainability_answer_contract": EXPLAINABILITY_ANSWER_CONTRACT,
        }

    @agent.tool_plain(sequential=True)
    async def read_plan_history(command: PlanHistoryRead) -> dict[str, object]:
        """Read a bounded newest-first PlanVersion history only on explicit user request."""

        plan_id, reason = await resolve_read_plan_id(command.plan_id)
        if plan_id is None:
            return {"result": "plan_not_resolved", "reason": reason}
        return {
            "result": "plan_history",
            "explainability_answer_contract": EXPLAINABILITY_ANSWER_CONTRACT,
            "approval_activity": await query_service.get_approval_activity(plan_id),
            "versions": await query_service.get_bounded_history(
                plan_id,
                limit=command.limit,
            ),
        }

    @agent.tool_plain(sequential=True)
    async def read_plan_version(command: PlanVersionRead) -> dict[str, object]:
        """Read one immutable historical/current version with canonical provenance evidence."""

        return {
            "result": "plan_version",
            "explainability_answer_contract": EXPLAINABILITY_ANSWER_CONTRACT,
            "version": await query_service.get_version_details(command.plan_version_id),
        }

    @agent.tool_plain(sequential=True)
    async def compare_plan_versions(command: PlanVersionCompareRead) -> dict[str, object]:
        """Return a deterministic structured comparison within one Plan lineage."""

        return {
            "result": "plan_version_comparison",
            "explainability_answer_contract": EXPLAINABILITY_ANSWER_CONTRACT,
            "comparison": await query_service.compare_plan_versions(
                command.older_plan_version_id,
                command.newer_plan_version_id,
            ),
        }

    @agent.tool_plain(sequential=True)
    async def assess_plan_staleness_read(command: PlanVersionRead) -> dict[str, object]:
        """Read whether recorded canonical basis changed; never mutate or replan."""

        return {
            "result": "plan_staleness",
            **await query_service.get_staleness_details(command.plan_version_id),
            "meaning": "basis changed, not proof that the Plan is invalid",
        }
