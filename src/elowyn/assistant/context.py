from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from elowyn.db.models import Message
from elowyn.domain.enums import MessageAuthor


@dataclass(frozen=True)
class BoundedMemoryContext:
    text: str
    token_upper_bound: int
    item_count: int
    authoritative: Literal[False] = False


def build_turn_prompt(
    *,
    user_text: str,
    world_state: str,
    history: list[Message],
    memory_context: BoundedMemoryContext | None = None,
    current_time: datetime | None = None,
) -> str:
    history_lines: list[str] = []
    for message in history:
        if not message.text:
            continue
        role = "USER" if message.author == MessageAuthor.USER else "ELOWYN"
        history_lines.append(f"{role}: {message.text}")
    history_text = "\n".join(history_lines[-12:]) or "(нет предыдущих сообщений)"
    memory_text = ""
    if memory_context is not None and memory_context.text:
        memory_text = f"\n{memory_context.text}\n"

    now = current_time or datetime.now(UTC)
    return f"""ТЕКУЩАЯ ДАТА И ВРЕМЯ (для относительных дат):
{now.isoformat()}

ТЕКУЩИЙ WORLD STATE (authoritative; внутренние entity_id не показывай пользователю):
{world_state}

ПОСЛЕДНИЙ КОНТЕКСТ РАЗГОВОРА:
{history_text}
{memory_text}

НОВОЕ СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ:
{user_text}

Текущий WORLD STATE и новое явное утверждение пользователя всегда приоритетнее памяти.
Разрешай разговорные ссылки по World State и контексту. Если ссылка реально неоднозначна — уточни.
Если изменение однозначно — примени его через domain tool и затем ответь по смыслу,
без CRUD/ID отчёта.
"""
