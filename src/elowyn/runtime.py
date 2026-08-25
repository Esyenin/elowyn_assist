from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta

from elowyn.assistant.context import build_turn_prompt
from elowyn.assistant.planning_presentation import PlanningTurnState, render_plan_version
from elowyn.assistant.planning_resolution import (
    ApprovedPlanItemResolver,
    ApprovedTargetStatus,
    CandidateResolutionStatus,
    CurrentCandidateRejectIntent,
    PresentedCandidateResolver,
    current_candidate_reject_intent,
    historical_candidate_duration_days,
    is_collaborative_first_item_request,
    is_collaborative_next_item_request,
    is_compact_plan_request,
    is_historical_rejected_candidate_question,
    is_plan_staleness_question,
    is_presence_small_talk,
    relative_deadline_basis_days,
)
from elowyn.assistant.planning_tools import has_explicit_replanning_intent
from elowyn.assistant.tools import build_agent
from elowyn.db.models import Goal, Source
from elowyn.domain.commands import GoalCreate, GoalUpdate
from elowyn.domain.enums import (
    ActorType,
    DeadlineType,
    PlanGoalRole,
    PlanItemProgressStatus,
    PlanVersionBasisRole,
    PlanVersionStatus,
)
from elowyn.domain.messages import IncomingMessage
from elowyn.domain.planning_commands import (
    PlanCandidateReject,
    PlanGoalLinkCreate,
    PlanVersionPresentationCreate,
)
from elowyn.memory.deep import DeepMemoryRoute
from elowyn.memory.service import MemoryService
from elowyn.services.context_composer import ContextComposer, ContextComposerConfig
from elowyn.services.conversation import ConversationService
from elowyn.services.deep_memory import DeepMemoryService, route_deep_memory
from elowyn.services.planning import PlanningService
from elowyn.services.planning_query import PlanningQueryService
from elowyn.services.query import WorldStateQueryService
from elowyn.services.world_state import ActionContext, WorldStateService

logger = logging.getLogger(__name__)


class ElowynRuntime:
    """One transport-independent conversational turn over persistent World State."""

    def __init__(
        self,
        *,
        session_factory,
        model,
        memory_ingestion_wakeup: Callable[[], None] | None = None,
        memory_service: MemoryService | None = None,
        context_composer_config: ContextComposerConfig | None = None,
    ):
        self.session_factory = session_factory
        self.model = model
        self.memory_ingestion_wakeup = memory_ingestion_wakeup
        self.memory_service = memory_service
        self.context_composer_config = context_composer_config

    async def handle_message(self, incoming: IncomingMessage) -> str | None:
        async with self.session_factory() as session:
            conversation_service = ConversationService(session)
            ingested = await conversation_service.ingest_user_message(incoming)
            conversation_id = ingested.conversation.id
            user_message_id = ingested.message.id
            source_id = ingested.source.id
            # Preserve the original Message/Source even if the model provider fails later.
            await session.commit()
            self._wake_memory_ingestion()
            if not ingested.is_new:
                replied = await conversation_service.has_assistant_reply(
                    conversation_id=conversation_id,
                    user_message_id=user_message_id,
                )
                if replied:
                    return None
                # Close the read transaction before opening the explicit post-user unit of work.
                await session.rollback()

            try:
                async with session.begin():
                    turn_source = await session.get(Source, source_id)
                    if turn_source is None:
                        raise RuntimeError("persisted user Source disappeared before agent turn")
                    history = await conversation_service.recent_messages(conversation_id, limit=13)
                    history = [message for message in history if message.id != user_message_id]
                    if is_presence_small_talk(incoming.text):
                        response = "Да, я здесь."
                        await conversation_service.record_assistant_message(
                            conversation_id=conversation_id,
                            text=response,
                            in_reply_to_message_id=user_message_id,
                        )
                        self._wake_memory_ingestion()
                        return response
                    query_service = WorldStateQueryService(session)
                    world_state = await query_service.render_for_llm()
                    memory_context = None
                    deep_memory_route = DeepMemoryRoute.NONE
                    deep_memory_service = None
                    if self.memory_service is not None:
                        memory_context = await ContextComposer(
                            session,
                            self.context_composer_config,
                        ).memory_context(
                            user_text=incoming.text,
                            world_state=world_state,
                            history=history,
                        )
                        deep_memory_route = route_deep_memory(incoming.text)
                        if deep_memory_route != DeepMemoryRoute.NONE:
                            deep_memory_service = DeepMemoryService(session, self.memory_service)
                    service = WorldStateService(session)
                    planning_service = PlanningService(session)
                    planning_query_service = PlanningQueryService(session)
                    planning_turn_state = PlanningTurnState()
                    action_context = ActionContext(
                        actor_type=ActorType.USER,
                        source=turn_source,
                        description="Natural-language user turn",
                        operation_id=uuid.uuid4(),
                    )
                    approved_resolver = ApprovedPlanItemResolver(session)
                    if is_compact_plan_request(incoming.text):
                        plans = await planning_query_service.list_plans(limit=2)
                        if len(plans) != 1:
                            response = (
                                "Уточни, какой план показать короче."
                                if plans
                                else "Сейчас нет сохранённого плана."
                            )
                            compact_version = None
                        else:
                            compact_version = await planning_query_service.get_current_candidate(
                                plans[0].entity_id
                            )
                            if compact_version is None:
                                compact_version = await planning_query_service.get_current_approved(
                                    plans[0].entity_id
                                )
                            response = (
                                "Сейчас нет текущей версии плана."
                                if compact_version is None
                                else await render_plan_version(
                                    session,
                                    compact_version.id,
                                    compact=True,
                                )
                            )
                        assistant_message = await conversation_service.record_assistant_message(
                            conversation_id=conversation_id,
                            text=response,
                            in_reply_to_message_id=user_message_id,
                        )
                        if (
                            compact_version is not None
                            and compact_version.status == PlanVersionStatus.CANDIDATE
                        ):
                            await planning_service.record_version_presentation(
                                PlanVersionPresentationCreate(
                                    plan_version_id=compact_version.id,
                                    message_id=assistant_message.id,
                                ),
                                ActionContext(
                                    actor_type=ActorType.ASSISTANT,
                                    description=(
                                        "Compact canonical Candidate included in persisted "
                                        "assistant Message"
                                    ),
                                    operation_id=uuid.uuid4(),
                                ),
                            )
                        self._wake_memory_ingestion()
                        return response
                    collaborative_first = is_collaborative_first_item_request(incoming.text)
                    collaborative_next = is_collaborative_next_item_request(incoming.text)
                    if collaborative_first or collaborative_next:
                        plans = await planning_query_service.list_plans(limit=2)
                        if len(plans) != 1:
                            response = (
                                "Уточни, с каким планом и пунктом поработать вместе."
                                if plans
                                else "Сейчас нет сохранённого плана, из которого можно взять пункт."
                            )
                        else:
                            if collaborative_next:
                                target_version = await planning_query_service.get_current_approved(
                                    plans[0].entity_id
                                )
                                selected_item = await planning_query_service.get_next_action(
                                    plans[0].entity_id
                                )
                            else:
                                target_version = (
                                    await planning_query_service.get_current_candidate(
                                        plans[0].entity_id
                                    )
                                    or await planning_query_service.get_current_approved(
                                        plans[0].entity_id
                                    )
                                )
                                items = (
                                    []
                                    if target_version is None
                                    else await planning_query_service.get_version_items(
                                        target_version.id
                                    )
                                )
                                selected_item = next(
                                    (item for item in items if item.ordinal == 1),
                                    items[0] if items else None,
                                )
                            if target_version is None or selected_item is None:
                                response = (
                                    "В текущем утверждённом плане нет доступного следующего "
                                    "действия."
                                    if collaborative_next
                                    else "В текущей версии плана нет первого пункта."
                                )
                            else:
                                progress = (
                                    await planning_query_service.get_item_progress(
                                        target_version.id
                                    )
                                    if target_version.status == PlanVersionStatus.APPROVED
                                    else []
                                )
                                progress_by_item = {
                                    entry.plan_version_item_id: entry for entry in progress
                                }
                                if collaborative_next:
                                    status_trace = [
                                        (item.ordinal, progress_by_item[item.id].status.value)
                                        for item in await planning_query_service.get_version_items(
                                            target_version.id
                                        )
                                        if item.id in progress_by_item
                                    ]
                                    selected_status = progress_by_item[selected_item.id].status
                                    logger.debug(
                                        "Collaborative next trace: approved=v%s progress=%s "
                                        "selected_ordinal=%s selected_status=%s",
                                        target_version.version_number,
                                        status_trace,
                                        selected_item.ordinal,
                                        selected_status.value,
                                    )
                                    if selected_status in {
                                        PlanItemProgressStatus.DONE,
                                        PlanItemProgressStatus.SKIPPED,
                                    }:
                                        raise RuntimeError(
                                            "canonical next-action resolver selected completed item"
                                        )
                                is_done = (
                                    selected_item.id in progress_by_item
                                    and progress_by_item[selected_item.id].status
                                    == PlanItemProgressStatus.DONE
                                )
                                if is_done:
                                    response = (
                                        f"Пункт {selected_item.ordinal} «{selected_item.title}» "
                                        "уже отмечен выполненным. "
                                        "Можем пройти его ещё раз или применить результат на "
                                        "практике — с чего начнём?"
                                    )
                                else:
                                    starting_point = (
                                        selected_item.description
                                        or selected_item.expected_outcome
                                        or "Сначала уточним исходные данные и сделаем первый шаг."
                                    )
                                    response = (
                                        "Давай начнём вместе. "
                                        f"Пункт {selected_item.ordinal} — «{selected_item.title}»."
                                        "\n\n"
                                        f"{starting_point}\n\n"
                                        "Напиши, что у тебя уже есть по этому пункту, и продолжим."
                                    )
                        await conversation_service.record_assistant_message(
                            conversation_id=conversation_id,
                            text=response,
                            in_reply_to_message_id=user_message_id,
                        )
                        self._wake_memory_ingestion()
                        return response
                    deadline_days = relative_deadline_basis_days(incoming.text)
                    if deadline_days is not None and not has_explicit_replanning_intent(
                        incoming.text
                    ):
                        approved_resolution = await approved_resolver.resolve_plan()
                        if approved_resolution.status == ApprovedTargetStatus.RESOLVED:
                            assert approved_resolution.plan_id is not None
                            assert approved_resolution.plan_version_id is not None
                            plan = await planning_query_service.get_plan(
                                approved_resolution.plan_id
                            )
                            links = await planning_query_service.get_plan_goal_links(plan.entity_id)
                            version_basis = await planning_query_service.get_version_basis(
                                approved_resolution.plan_version_id
                            )
                            basis_goal_ids = [
                                basis.entity_id
                                for basis in version_basis
                                if basis.role == PlanVersionBasisRole.GOAL
                            ]
                            selected_goal_id = (
                                basis_goal_ids[0] if len(basis_goal_ids) == 1 else None
                            )
                            primary = [link for link in links if link.role == PlanGoalRole.PRIMARY]
                            if selected_goal_id is None and len(primary) == 1:
                                selected_goal_id = primary[0].goal_id
                            if selected_goal_id is None and len(links) == 1:
                                selected_goal_id = links[0].goal_id
                            ambiguous_basis = len(basis_goal_ids) > 1 or (
                                selected_goal_id is None and bool(links)
                            )
                            if ambiguous_basis:
                                response = (
                                    "У плана несколько связанных целей. Уточни, к какой из них "
                                    "относится новый срок; Planning state пока не изменён."
                                )
                            else:
                                target_date = incoming.sent_at + timedelta(days=deadline_days)
                                if selected_goal_id is None:
                                    goal = await service.create_goal(
                                        GoalCreate(
                                            title=f"Срок для: {plan.title}"[:500],
                                            target_date=target_date,
                                            target_date_type=DeadlineType.HARD,
                                        ),
                                        action_context,
                                    )
                                    selected_goal_id = goal.entity_id
                                    await planning_service.link_goal(
                                        plan.entity_id,
                                        PlanGoalLinkCreate(
                                            goal_id=goal.entity_id,
                                            role=PlanGoalRole.PRIMARY,
                                        ),
                                        action_context,
                                    )
                                else:
                                    goal = await session.get(Goal, selected_goal_id)
                                    if goal is None:
                                        raise RuntimeError("linked Goal disappeared")
                                    if (
                                        goal.target_date != target_date
                                        or goal.target_date_type != DeadlineType.HARD
                                    ):
                                        await service.update_goal(
                                            GoalUpdate(
                                                entity_id=goal.entity_id,
                                                target_date=target_date,
                                                target_date_type=DeadlineType.HARD,
                                            ),
                                            action_context,
                                        )
                                    if all(link.goal_id != selected_goal_id for link in links):
                                        await planning_service.link_goal(
                                            plan.entity_id,
                                            PlanGoalLinkCreate(
                                                goal_id=selected_goal_id,
                                                role=(
                                                    PlanGoalRole.PRIMARY
                                                    if not primary
                                                    else PlanGoalRole.SUPPORTING
                                                ),
                                            ),
                                            action_context,
                                        )
                                response = (
                                    "Новый срок сохранён как canonical основание. Текущий "
                                    "утверждённый план теперь устарел относительно него, но не "
                                    "изменён. Хочешь, я предложу обновлённый вариант?"
                                )
                            await conversation_service.record_assistant_message(
                                conversation_id=conversation_id,
                                text=response,
                                in_reply_to_message_id=user_message_id,
                            )
                            self._wake_memory_ingestion()
                            return response
                    if is_historical_rejected_candidate_question(incoming.text):
                        plans = await planning_query_service.list_plans(limit=8)
                        duration_days = historical_candidate_duration_days(incoming.text)
                        rejected_versions = [
                            version
                            for plan in plans
                            for version in await planning_query_service.get_rejected_versions(
                                plan.entity_id,
                                duration_days=duration_days,
                                limit=100,
                            )
                        ]
                        logger.debug(
                            "Rejected history trace: duration_days=%s matched_versions=%s",
                            duration_days,
                            [version.version_number for version in rejected_versions],
                        )
                        if len(rejected_versions) == 1:
                            rejected = rejected_versions[0]
                            response = (
                                f"Версия v{rejected.version_number} существовала и была "
                                "отклонена. Она сохранена в истории; текущего предложенного "
                                "варианта сейчас нет."
                            )
                        elif rejected_versions:
                            response = (
                                "В истории несколько отклонённых вариантов. Уточни номер версии "
                                "или её содержание, чтобы я выбрала точно."
                            )
                        else:
                            response = "В истории нет отклонённых вариантов плана."
                        await conversation_service.record_assistant_message(
                            conversation_id=conversation_id,
                            text=response,
                            in_reply_to_message_id=user_message_id,
                        )
                        self._wake_memory_ingestion()
                        return response
                    reject_intent = current_candidate_reject_intent(incoming.text)
                    if reject_intent != CurrentCandidateRejectIntent.NONE:
                        resolution = await PresentedCandidateResolver(session).resolve_current()
                        if reject_intent == CurrentCandidateRejectIntent.AMBIGUOUS:
                            response = (
                                "Уточни, пожалуйста: ты хочешь отклонить текущий предложенный "
                                "вариант или только пока не утверждать его?"
                            )
                        elif resolution.status == CandidateResolutionStatus.RESOLVED:
                            assert resolution.plan_version_id is not None
                            await planning_service.reject_candidate_version(
                                PlanCandidateReject(plan_version_id=resolution.plan_version_id),
                                action_context,
                            )
                            response = (
                                "Текущий предложенный вариант отклонён. "
                                "Действующий утверждённый план не изменён."
                            )
                        elif resolution.status == CandidateResolutionStatus.NO_TARGET:
                            response = "Сейчас нет текущего предложенного варианта для отклонения."
                        else:
                            response = (
                                "Есть несколько текущих предложенных вариантов. "
                                "Уточни, какой именно нужно отклонить."
                            )
                        await conversation_service.record_assistant_message(
                            conversation_id=conversation_id,
                            text=response,
                            in_reply_to_message_id=user_message_id,
                        )
                        self._wake_memory_ingestion()
                        return response
                    if is_plan_staleness_question(incoming.text):
                        approved_resolution = await approved_resolver.resolve_plan()
                        if approved_resolution.status == ApprovedTargetStatus.RESOLVED:
                            assert approved_resolution.plan_version_id is not None
                            details = await planning_query_service.get_staleness_details(
                                approved_resolution.plan_version_id
                            )
                            if details["is_basis_stale"]:
                                response = (
                                    "Нет. Canonical Planning assessment показывает, что "
                                    "основание текущего утверждённого плана изменилось; сам план "
                                    "остаётся действующим, пока ты явно не утвердишь замену."
                                )
                            else:
                                response = (
                                    "Canonical Planning assessment не обнаруживает изменений "
                                    "зафиксированных оснований текущего утверждённого плана."
                                )
                        elif approved_resolution.status == ApprovedTargetStatus.NO_APPROVED_PLAN:
                            response = "Сейчас нет текущего утверждённого плана для проверки."
                        else:
                            response = (
                                "Есть несколько действующих планов. Уточни, какой нужно "
                                "проверить на актуальность."
                            )
                        await conversation_service.record_assistant_message(
                            conversation_id=conversation_id,
                            text=response,
                            in_reply_to_message_id=user_message_id,
                        )
                        self._wake_memory_ingestion()
                        return response
                    planning_context = await planning_query_service.render_for_agent()
                    agent = build_agent(
                        model=self.model,
                        service=service,
                        query_service=query_service,
                        action_context=action_context,
                        deep_memory_service=deep_memory_service,
                        deep_memory_route=deep_memory_route,
                        planning_service=planning_service,
                        planning_query_service=planning_query_service,
                        planning_turn_state=planning_turn_state,
                        conversation_id=conversation_id,
                        user_message_id=user_message_id,
                    )
                    prompt = build_turn_prompt(
                        user_text=incoming.text,
                        world_state=world_state,
                        history=history,
                        memory_context=memory_context,
                        planning_context=planning_context,
                    )
                    result = await agent.run(prompt)
                    resolved = planning_turn_state.resolve(str(result.output))
                    response = resolved.text
                    assistant_message = await conversation_service.record_assistant_message(
                        conversation_id=conversation_id,
                        text=response,
                        in_reply_to_message_id=user_message_id,
                    )
                    presentation_context = ActionContext(
                        actor_type=ActorType.ASSISTANT,
                        description="Canonical Candidate included in persisted assistant Message",
                        operation_id=uuid.uuid4(),
                    )
                    for version_id in resolved.plan_version_ids:
                        await planning_service.record_version_presentation(
                            PlanVersionPresentationCreate(
                                plan_version_id=version_id,
                                message_id=assistant_message.id,
                            ),
                            presentation_context,
                        )
                self._wake_memory_ingestion()
                return response
            except Exception:
                # Domain writes from a failed agent turn must not become canonical state.
                await session.rollback()
                raise

    def _wake_memory_ingestion(self) -> None:
        if self.memory_ingestion_wakeup is None:
            return
        try:
            self.memory_ingestion_wakeup()
        except Exception:
            # Scheduling is advisory: the periodic raw-archive scan remains the
            # durable catch-up mechanism and the user turn must stay successful.
            return
