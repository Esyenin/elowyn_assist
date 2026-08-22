from __future__ import annotations

from elowyn.db.models import Message
from elowyn.domain.enums import MessageAuthor


def build_turn_prompt(*, user_text: str, world_state: str, history: list[Message]) -> str:
    history_lines: list[str] = []
    for message in history:
        if not message.text:
            continue
        role = "USER" if message.author == MessageAuthor.USER else "ELOWYN"
        history_lines.append(f"{role}: {message.text}")
    history_text = "\n".join(history_lines[-12:]) or "(нет предыдущих сообщений)"

    return f"""ТЕКУЩИЙ WORLD STATE (authoritative; внутренние entity_id не показывай пользователю):
{world_state}

ПОСЛЕДНИЙ КОНТЕКСТ РАЗГОВОРА:
{history_text}

НОВОЕ СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ:
{user_text}

Разрешай разговорные ссылки по World State и контексту. Если ссылка реально неоднозначна — уточни.
Если изменение однозначно — примени его через domain tool и затем ответь по смыслу, без CRUD/ID отчёта.
"""
