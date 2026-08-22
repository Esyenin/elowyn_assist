from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.base import Base


def canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, datetime, date)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if isinstance(value, dict):
        return {str(key): canonical_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    return value


async def state_history_digest(session: AsyncSession) -> str:
    """Hash every v0.1 table deterministically without becoming a source of truth."""

    payload: dict[str, list[dict[str, Any]]] = {}
    for table in sorted(Base.metadata.sorted_tables, key=lambda item: item.name):
        primary_key = list(table.primary_key.columns)
        statement = select(table)
        if primary_key:
            statement = statement.order_by(*primary_key)
        rows = (await session.execute(statement)).mappings().all()
        payload[table.name] = [
            {str(key): canonical_value(value) for key, value in sorted(row.items())} for row in rows
        ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
