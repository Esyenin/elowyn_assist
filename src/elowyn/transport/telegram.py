from __future__ import annotations

import html
import logging
import re

from elowyn.domain.enums import TransportType
from elowyn.domain.messages import IncomingMessage
from elowyn.support.model_errors import classify_transient_model_error

TELEGRAM_MESSAGE_LIMIT = 4096
# Keep headroom below Telegram's documented limit and count astral characters
# conservatively as two UTF-16 units, matching Telegram entity offsets.
TELEGRAM_CHUNK_LIMIT = 4000
TEMPORARY_MODEL_ERROR_MESSAGE = (
    "Сервис модели временно недоступен. Попробуй ещё раз через минуту."
)
_MARKDOWN_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+.!>|-])")
_MARKDOWN_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
logger = logging.getLogger(__name__)


def clean_telegram_text(text: str) -> str:
    """Normalize common model markup leaks for the transport's plain-text mode."""

    cleaned = html.unescape(text).replace("\u00a0", " ")
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = _MARKDOWN_ESCAPE.sub(r"\1", cleaned)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"`([^`\n]+)`", r"\1", cleaned)
    return _plain_text_tables(cleaned)


def _plain_text_tables(text: str) -> str:
    lines = text.splitlines(keepends=True)
    rendered: list[str] = []
    in_table = False
    for line in lines:
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line.removesuffix(ending)
        cells = [cell.strip() for cell in body.strip().strip("|").split("|")]
        is_table_row = "|" in body and len(cells) >= 2 and all(cells)
        if is_table_row and all(_MARKDOWN_TABLE_SEPARATOR.fullmatch(cell) for cell in cells):
            in_table = True
            continue
        if is_table_row:
            prefix = "" if not in_table else "• "
            rendered.append(prefix + " — ".join(cells) + ending)
            in_table = True
            continue
        in_table = False
        rendered.append(line)
    return "".join(rendered)


def split_telegram_text(
    text: str, *, max_units: int = TELEGRAM_CHUNK_LIMIT
) -> tuple[str, ...]:
    """Split plain text losslessly, preferring paragraph and line boundaries."""

    if max_units < 2:
        raise ValueError("Telegram chunk limit must be at least two units")
    if _telegram_units(text) <= max_units:
        return (text,)

    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = _bounded_end(text, start=start, max_units=max_units)
        if hard_end == len(text):
            chunks.append(text[start:])
            break

        paragraph = text.rfind("\n\n", start, hard_end)
        if paragraph >= start:
            end = paragraph + 2
        else:
            line = text.rfind("\n", start, hard_end)
            end = line + 1 if line >= start else hard_end
        chunks.append(text[start:end])
        start = end
    return tuple(chunks)


async def send_telegram_text(message, text: str) -> None:
    """Deliver every plain-text chunk in order; propagate the first send failure."""

    for chunk in split_telegram_text(clean_telegram_text(text)):
        # Explicit None preserves the current plain-text behavior even if a bot
        # default parse mode is introduced elsewhere later.
        await message.answer(chunk, parse_mode=None)


def _bounded_end(text: str, *, start: int, max_units: int) -> int:
    units = 0
    end = start
    while end < len(text):
        char_units = 2 if ord(text[end]) > 0xFFFF else 1
        if units + char_units > max_units:
            break
        units += char_units
        end += 1
    if end == start:
        raise ValueError("Telegram chunk limit cannot contain the next character")
    # Keep a Windows CRLF pair in the following chunk rather than splitting it.
    if end < len(text) and text[end - 1] == "\r" and text[end] == "\n":
        end -= 1
    return end


def _telegram_units(text: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


class TelegramAdapter:
    """Translate Telegram-specific updates into the transport-independent message model."""

    def __init__(self, allowed_user_id: int | None = None):
        self.allowed_user_id = allowed_user_id

    def check_user(self, telegram_user_id: int) -> bool:
        return self.allowed_user_id is not None and telegram_user_id == self.allowed_user_id

    def to_incoming(self, message) -> IncomingMessage:
        return IncomingMessage(
            transport=TransportType.TELEGRAM,
            external_conversation_id=str(message.chat.id),
            external_message_id=str(message.message_id),
            text=message.text,
            sent_at=message.date,
            raw_payload=message.model_dump(mode="json", exclude_none=True),
        )


def build_router(message_handler, *, adapter: TelegramAdapter | None = None):
    from aiogram import Router
    from aiogram.types import Message as TelegramMessage

    adapter = adapter or TelegramAdapter()
    router = Router(name="elowyn")

    @router.message()
    async def on_message(message: TelegramMessage) -> None:
        if message.from_user is None or not adapter.check_user(message.from_user.id):
            return
        if not message.text:
            await message.answer("В v0.1 я пока принимаю только текстовые сообщения.")
            return
        try:
            response = await message_handler(adapter.to_incoming(message))
        except Exception as error:
            transient = classify_transient_model_error(error)
            if transient is None:
                raise
            logger.warning(
                "Transient external model failure type=%s status_code=%s model=%s",
                transient.error_type,
                transient.status_code,
                transient.model_name,
            )
            try:
                await send_telegram_text(message, TEMPORARY_MODEL_ERROR_MESSAGE)
            except Exception:
                logger.exception("Failed to deliver temporary model-error response to Telegram")
            return
        if response is not None:
            await send_telegram_text(message, response)

    return router
