from __future__ import annotations

import uuid

from elowyn.assistant.context import build_turn_prompt
from elowyn.assistant.tools import build_agent
from elowyn.domain.enums import ActorType
from elowyn.domain.messages import IncomingMessage
from elowyn.services.conversation import ConversationService
from elowyn.services.query import WorldStateQueryService
from elowyn.services.world_state import ActionContext, WorldStateService


class ElowynRuntime:
    """One transport-independent conversational turn over persistent World State."""

    def __init__(self, *, session_factory, model):
        self.session_factory = session_factory
        self.model = model

    async def handle_message(self, incoming: IncomingMessage) -> str | None:
        async with self.session_factory() as session:
            conversation_service = ConversationService(session)
            ingested = await conversation_service.ingest_user_message(incoming)
            # Preserve the original Message/Source even if the model provider fails later.
            await session.commit()
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
                )
                prompt = build_turn_prompt(
                    user_text=incoming.text,
                    world_state=world_state,
                    history=history,
                )
                result = await agent.run(prompt)
                response = str(result.output)
                await conversation_service.record_assistant_message(
                    conversation_id=ingested.conversation.id,
                    text=response,
                    in_reply_to_message_id=ingested.message.id,
                )
                await session.commit()
                return response
            except Exception:
                # Domain writes from a failed agent turn must not become canonical state.
                await session.rollback()
                raise
