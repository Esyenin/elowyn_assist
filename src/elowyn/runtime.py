from __future__ import annotations

import uuid
from collections.abc import Callable

from elowyn.assistant.context import build_turn_prompt
from elowyn.assistant.tools import build_agent
from elowyn.domain.enums import ActorType
from elowyn.domain.messages import IncomingMessage
from elowyn.memory.deep import DeepMemoryRoute
from elowyn.memory.service import MemoryService
from elowyn.services.context_composer import ContextComposer, ContextComposerConfig
from elowyn.services.conversation import ConversationService
from elowyn.services.deep_memory import DeepMemoryService, route_deep_memory
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
            # Preserve the original Message/Source even if the model provider fails later.
            await session.commit()
            self._wake_memory_ingestion()
            if not ingested.is_new and await conversation_service.has_assistant_reply(
                conversation_id=ingested.conversation.id,
                user_message_id=ingested.message.id,
            ):
                return None

            try:
                history = await conversation_service.recent_messages(
                    ingested.conversation.id, limit=13
                )
                history = [message for message in history if message.id != ingested.message.id]
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
                action_context = ActionContext(
                    actor_type=ActorType.USER,
                    source=ingested.source,
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
                )
                prompt = build_turn_prompt(
                    user_text=incoming.text,
                    world_state=world_state,
                    history=history,
                    memory_context=memory_context,
                )
                result = await agent.run(prompt)
                response = str(result.output)
                await conversation_service.record_assistant_message(
                    conversation_id=ingested.conversation.id,
                    text=response,
                    in_reply_to_message_id=ingested.message.id,
                )
                await session.commit()
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
