from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import Conversation, ConversationSummary, Message


class ConversationSummaryService:
    """Persistence boundary for disposable summaries derived from raw Message rows."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def save(
        self,
        *,
        conversation_id: uuid.UUID,
        short_summary: str,
        topics: list[str],
        related_entity_ids: list[uuid.UUID],
        last_processed_message_id: uuid.UUID | None,
        derivation_version: str,
    ) -> ConversationSummary:
        if await self.session.get(Conversation, conversation_id) is None:
            raise ValueError("conversation was not found")
        if last_processed_message_id is not None:
            message = await self.session.get(Message, last_processed_message_id)
            if message is None or message.conversation_id != conversation_id:
                raise ValueError("summary cursor must belong to the conversation")
        summary_text = short_summary.strip()
        version = derivation_version.strip()
        if not summary_text or not version:
            raise ValueError("summary and derivation version must not be blank")

        summary = await self.session.get(ConversationSummary, conversation_id)
        if summary is None:
            summary = ConversationSummary(
                conversation_id=conversation_id,
                short_summary=summary_text,
                topics=[],
                related_entity_ids=[],
                derivation_version=version,
            )
            self.session.add(summary)
        summary.short_summary = summary_text
        summary.topics = list(dict.fromkeys(topic.strip() for topic in topics if topic.strip()))
        summary.related_entity_ids = list(dict.fromkeys(str(item) for item in related_entity_ids))
        summary.last_processed_message_id = last_processed_message_id
        summary.derivation_version = version
        await self.session.flush()
        return summary
