from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import Message
from elowyn.memory.service import MemoryProvenance


class MemoryProvenanceService:
    """Resolve semantic memory back to the canonical Core raw archive."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_message(self, provenance: MemoryProvenance) -> Message:
        message = await self.session.get(Message, provenance.message_id)
        if message is None:
            raise LookupError("memory provenance message was not found")
        if message.conversation_id != provenance.conversation_id:
            raise LookupError("memory provenance conversation does not match the message")
        if message.author.value != provenance.role:
            raise LookupError("memory provenance role does not match the message")
        if _as_utc(message.sent_at) != _as_utc(provenance.occurred_at):
            raise LookupError("memory provenance timestamp does not match the message")
        return message


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
