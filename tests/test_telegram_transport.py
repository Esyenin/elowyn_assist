from __future__ import annotations

import pytest

from elowyn.transport.telegram import (
    TELEGRAM_CHUNK_LIMIT,
    clean_telegram_text,
    send_telegram_text,
    split_telegram_text,
)


class RecordingMessage:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail_at = fail_at

    async def answer(self, text: str, *, parse_mode=None) -> None:
        self.calls.append((text, parse_mode))
        if self.fail_at == len(self.calls):
            raise RuntimeError("synthetic Telegram delivery failure")


def _units(text: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


@pytest.mark.asyncio
async def test_short_response_is_sent_once_as_plain_text() -> None:
    message = RecordingMessage()

    await send_telegram_text(message, "Короткий ответ")

    assert message.calls == [("Короткий ответ", None)]


def test_long_response_is_bounded_and_reconstructs_exactly() -> None:
    text = "a" * (TELEGRAM_CHUNK_LIMIT + 37)

    chunks = split_telegram_text(text)

    assert len(chunks) == 2
    assert all(_units(chunk) <= TELEGRAM_CHUNK_LIMIT for chunk in chunks)
    assert "".join(chunks) == text


def test_multiple_paragraphs_split_at_existing_boundaries() -> None:
    paragraphs = ("Первый " * 210, "Второй " * 210, "Третий " * 210)
    text = "\n\n".join(paragraphs)

    chunks = split_telegram_text(text, max_units=2000)

    assert len(chunks) >= 2
    assert all(chunk.endswith("\n\n") for chunk in chunks[:-1])
    assert "".join(chunks) == text


def test_single_very_long_unicode_paragraph_uses_safe_character_boundaries() -> None:
    text = "🧠" * 2500 + " завершение"

    chunks = split_telegram_text(text)

    assert len(chunks) >= 2
    assert all(_units(chunk) <= TELEGRAM_CHUNK_LIMIT for chunk in chunks)
    assert all(chunk.encode("utf-8").decode("utf-8") == chunk for chunk in chunks)
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_delivery_failure_stops_without_claiming_later_chunks() -> None:
    text = "x" * (TELEGRAM_CHUNK_LIMIT * 3)
    message = RecordingMessage(fail_at=2)

    with pytest.raises(RuntimeError, match="synthetic Telegram delivery failure"):
        await send_telegram_text(message, text)

    assert len(message.calls) == 2


def test_plain_text_cleanup_removes_entities_escaped_markdown_and_table_syntax() -> None:
    dirty = (
        r"\*\*План\*\*&#x20;готов" "\n\n"
        "| Этап | Дни |\n"
        "| --- | --- |\n"
        "| Чтение | 1–7 |\n"
    )

    cleaned = clean_telegram_text(dirty)

    assert "&#x20;" not in cleaned
    assert r"\*\*" not in cleaned
    assert "**" not in cleaned
    assert "|" not in cleaned
    assert "План готов" in cleaned
    assert "• Чтение — 1–7" in cleaned


@pytest.mark.asyncio
async def test_cleaned_long_plan_remains_utf16_safe_and_reconstructable() -> None:
    dirty = r"\*\*План\*\*&#x20;" + ("🧠 пункт\n\n" * 900)
    expected = clean_telegram_text(dirty)
    message = RecordingMessage()

    await send_telegram_text(message, dirty)

    delivered = [text for text, parse_mode in message.calls]
    assert len(delivered) > 1
    assert all(parse_mode is None for _, parse_mode in message.calls)
    assert all(_units(chunk) <= TELEGRAM_CHUNK_LIMIT for chunk in delivered)
    assert "".join(delivered) == expected
