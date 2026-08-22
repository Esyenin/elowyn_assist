from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import Conversation, Message, Source
from elowyn.domain.enums import MessageAuthor, SourceType, TransportType
from elowyn.domain.messages import IncomingMessage


@dataclass(frozen=True)
class IngestedUserMessage:
    conversation: Conversation
    message: Message
    source: Source
    is_new: bool


class ConversationService:
    """Persistence boundary for transport-independent conversations and provenance."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create_conversation(
        self, *, transport: TransportType, external_conversation_id: str
    ) -> Conversation:
        conversation = (
            await self.session.execute(
                select(Conversation).where(
                    Conversation.transport == transport,
                    Conversation.external_conversation_id == external_conversation_id,
                )
            )
        ).scalar_one_or_none()
        if conversation is not None:
            return conversation

        conversation = Conversation(
            transport=transport,
            external_conversation_id=external_conversation_id,
        )
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def ingest_user_message(self, incoming: IncomingMessage) -> IngestedUserMessage:
        conversation = await self.get_or_create_conversation(
            transport=incoming.transport,
            external_conversation_id=incoming.external_conversation_id,
        )

        existing = (
            await self.session.execute(
                select(Message).where(
                    Message.conversation_id == conversation.id,
                    Message.external_message_id == incoming.external_message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            source = (
                await self.session.execute(select(Source).where(Source.message_id == existing.id))
            ).scalar_one_or_none()
            if source is None:
                source = Source(source_type=SourceType.USER_MESSAGE, message_id=existing.id)
                self.session.add(source)
                await self.session.flush()
            return IngestedUserMessage(conversation, existing, source, False)

        sent_at = incoming.sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        message = Message(
            conversation_id=conversation.id,
            author=MessageAuthor.USER,
            external_message_id=incoming.external_message_id,
            text=incoming.text,
            raw_payload=incoming.raw_payload,
            sent_at=sent_at,
        )
        self.session.add(message)
        await self.session.flush()

        source = Source(source_type=SourceType.USER_MESSAGE, message_id=message.id)
        self.session.add(source)
        await self.session.flush()
        return IngestedUserMessage(conversation, message, source, True)

    async def record_assistant_message(
        self,
        *,
        conversation_id,
        text: str,
        sent_at: datetime | None = None,
        in_reply_to_message_id=None,
    ) -> Message:
        raw_payload = None
        if in_reply_to_message_id is not None:
            raw_payload = {
                "elowyn_internal": {
                    "in_reply_to_message_id": str(in_reply_to_message_id),
                }
            }
        message = Message(
            conversation_id=conversation_id,
            author=MessageAuthor.ASSISTANT,
            text=text,
            raw_payload=raw_payload,
            sent_at=sent_at or datetime.now(UTC),
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def has_assistant_reply(self, *, conversation_id, user_message_id) -> bool:
        token = str(user_message_id)
        messages = (
            (
                await self.session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.author == MessageAuthor.ASSISTANT,
                    )
                    .order_by(Message.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        for message in messages:
            payload = message.raw_payload
            if not isinstance(payload, dict):
                continue
            metadata = payload.get("elowyn_internal")
            if isinstance(metadata, dict) and metadata.get("in_reply_to_message_id") == token:
                return True
        return False

    async def recent_messages(self, conversation_id, *, limit: int = 12) -> list[Message]:
        messages = (
            (
                await self.session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.sent_at.desc(), Message.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(reversed(messages))
