from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from elowyn.domain.enums import TransportType


@dataclass(frozen=True)
class IncomingMessage:
    """Transport-neutral input passed from an adapter into Elowyn Core."""

    transport: TransportType
    external_conversation_id: str
    external_message_id: str
    text: str
    sent_at: datetime
    raw_payload: dict[str, Any] | None = None
