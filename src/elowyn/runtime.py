from __future__ import annotations

import uuid
from collections.abc import Callable

from elowyn.assistant.context import build_turn_prompt
from elowyn.assistant.planning_presentation import PlanningTurnState
from elowyn.assistant.tools import build_agent
from elowyn.db.models import Source
from elowyn.domain.enums import ActorType
from elowyn.domain.messages import IncomingMessage
from elowyn.domain.planning_commands import PlanVersionPresentationCreate
from elowyn.memory.deep import DeepMemoryRoute
from elowyn.memory.service import MemoryService
from elowyn.services.context_composer import ContextComposer, ContextComposerConfig
from elowyn.services.conversation import ConversationService
from elowyn.services.deep_memory import DeepMemoryService, route_deep_memory
from elowyn.services.planning import PlanningService
from elowyn.services.planning_query import PlanningQueryService
from elowyn.services.query import WorldStateQueryService
from elowyn.services.world_state import ActionContext, WorldStateService


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
                    history = await conversation_service.recent_messages(
                        conversation_id, limit=13
                    )
                    history = [message for message in history if message.id != user_message_id]
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
                    planning_context = await planning_query_service.render_for_agent()
                    action_context = ActionContext(
                        actor_type=ActorType.USER,
                        source=turn_source,
                        description="Natural-language user turn",
                        operation_id=uuid.uuid4(),
                    )
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
