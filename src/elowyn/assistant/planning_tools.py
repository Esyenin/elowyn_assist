from __future__ import annotations

import json
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from elowyn.assistant.planning_presentation import PlanningTurnState, render_plan_version
from elowyn.assistant.planning_resolution import (
    ApprovedPlanItemResolver,
    ApprovedTargetStatus,
    CandidateResolutionStatus,
    PresentedCandidateResolver,
)
from elowyn.db.models import Event
from elowyn.domain.enums import PlanItemProgressStatus
from elowyn.domain.planning_commands import (
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


def planning_policy() -> str:
    return """

PLANNING v0.3 — CANDIDATE И ЕСТЕСТВЕННОЕ РЕШЕНИЕ:
- Обычный brainstorming и обсуждение вариантов не создают PlanVersion.
- Когда ты действительно предлагаешь цельную Strategy + конкретный Plan, используй
  create_plan_with_candidate или present_candidate_plan.
- После planning tool вставь выданный presentation_placeholder ровно один раз в финальный ответ.
  Не пересказывай и не переписывай сохранённый блок плана вместо placeholder.
- Для повторного показа неизменённой Candidate используй show_current_candidate; не создавай версию.
- Никогда не показывай internal IDs или внутренние placeholder-токены пользователю.
- Короткое полное согласие сразу после одного представленного Candidate обрабатывай через
  approve_presented_candidate без plan_version_id. Partial agreement, интерес, сомнение,
  обсуждение пункта или несогласие со Strategy НЕ являются approval.
- Если согласие неоднозначно, ничего не утверждай и естественно уточни выбор.
- Явный полный отказ от одного Candidate обрабатывай через reject_presented_candidate.
  Просьба переработать вариант — revision, а не rejection.
- plan_version_id передавай approve/reject tool только при явной содержательной ссылке пользователя
  на ранее представленный вариант; historical/SUPERSEDED версия напрямую не реактивируется.
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
- Причины версии объясняй прежде всего по creation/rejection evidence из read tools.
- Basis передавай только с точными canonical entity_id + event_id, уже полученными из Core.
  Никогда не придумывай event_id; если точного Event нет в контексте, оставь basis пустым.
- assess_plan_staleness_read сообщает только изменение basis: stale не означает invalid.
  Не создавай Candidate и не пересматривай Strategy автоматически из-за staleness.
- «Вернуться к старому варианту» означает создать и canonical-present новую Candidate через
  present_candidate_plan с based_on historical version; старую версию не реактивируй.
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

    @agent.tool_plain(sequential=True)
    async def create_plan_with_candidate(
        command: CreatePlanWithCandidateProposal,
    ) -> dict[str, str]:
        """Create a Plan lineage and a complete Candidate only when presenting a concrete plan."""

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
    async def reject_presented_candidate(
        command: PresentedCandidateTarget,
    ) -> dict[str, str]:
        """Reject the deterministically resolved, previously presented current Candidate."""

        resolution = await resolve_target(command)
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
        dependencies = await query_service.get_version_dependencies(
            plan_resolution.plan_version_id
        )
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
        return {"result": "current_plan", "plan": await query_service.get_plan_snapshot(plan_id)}

    @agent.tool_plain(sequential=True)
    async def read_plan_history(command: PlanHistoryRead) -> dict[str, object]:
        """Read a bounded newest-first PlanVersion history only on explicit user request."""

        plan_id, reason = await resolve_read_plan_id(command.plan_id)
        if plan_id is None:
            return {"result": "plan_not_resolved", "reason": reason}
        return {
            "result": "plan_history",
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
            "version": await query_service.get_version_details(command.plan_version_id),
        }

    @agent.tool_plain(sequential=True)
    async def compare_plan_versions(command: PlanVersionCompareRead) -> dict[str, object]:
        """Return a deterministic structured comparison within one Plan lineage."""

        return {
            "result": "plan_version_comparison",
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
