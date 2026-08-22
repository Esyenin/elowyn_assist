"""Verify security and idempotency around a completed synthetic Telegram smoke."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import Message as TelegramMessage
from aiogram.types import Update
from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.models import Conversation, Event, Message, Source, Task
from elowyn.domain.enums import EventType, MessageAuthor, TransportType
from elowyn.provider import build_runtime_model
from elowyn.runtime import ElowynRuntime
from elowyn.support.consistency import ConsistencyVerifier
from elowyn.transport.telegram import TelegramAdapter, build_router

FORBIDDEN_UX = re.compile(
    r"\b(?:entity_id|create_task|update_task|query_world_state|sql|crud|tool call)\b"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


async def count_rows(session, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def verify() -> None:
    load_dotenv(Path.cwd() / ".env", override=False)
    database_url = os.environ["DATABASE_URL"]
    allowed_user_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    engine = create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    adapter = TelegramAdapter(allowed_user_id=allowed_user_id)

    try:
        async with factory() as session:
            original = (
                await session.execute(
                    select(Message)
                    .join(Source, Source.message_id == Message.id)
                    .join(Event, Event.source_id == Source.id)
                    .where(
                        Message.author == MessageAuthor.USER,
                        Event.event_type == EventType.TASK_CREATED,
                    )
                    .limit(1)
                )
            ).scalar_one()
            before = (
                await count_rows(session, Message),
                await count_rows(session, Task),
                await count_rows(session, Event),
            )
            raw_payload = original.raw_payload
            if not isinstance(raw_payload, dict):
                raise RuntimeError("synthetic Telegram payload is unavailable")

        telegram_message = TelegramMessage.model_validate(raw_payload)
        runtime = ElowynRuntime(session_factory=factory, model=build_runtime_model())
        duplicate_result = await runtime.handle_message(adapter.to_incoming(telegram_message))

        async with factory() as session:
            after = (
                await count_rows(session, Message),
                await count_rows(session, Task),
                await count_rows(session, Event),
            )
            if duplicate_result is not None or after != before:
                raise RuntimeError("duplicate Telegram update changed persistent state")
        print("duplicate Telegram update verification passed")

        foreign_calls = 0

        async def forbidden_handler(_incoming) -> str:
            nonlocal foreign_calls
            foreign_calls += 1
            return "must not be sent"

        bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
        try:
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(forbidden_handler, adapter=adapter))
            foreign_update = Update.model_validate(
                {
                    "update_id": 2_000_000_001,
                    "message": {
                        "message_id": 2_000_000_001,
                        "date": int(datetime.now(UTC).timestamp()),
                        "chat": {"id": allowed_user_id + 1, "type": "private"},
                        "from": {
                            "id": allowed_user_id + 1,
                            "is_bot": False,
                            "first_name": "SyntheticForeignUser",
                        },
                        "text": "synthetic unauthorized smoke",
                    },
                }
            )
            await dispatcher.feed_update(bot, foreign_update)
        finally:
            await bot.session.close()
        if foreign_calls:
            raise RuntimeError("foreign Telegram user reached the message handler")
        print("Telegram allow-list verification passed")

        async with factory() as session:
            conversations = (
                await session.execute(
                    select(Conversation).where(Conversation.transport == TransportType.TELEGRAM)
                )
            ).scalars()
            if any(item.external_conversation_id != str(allowed_user_id) for item in conversations):
                raise RuntimeError("unauthorized Telegram conversation was persisted")

            assistant_texts = (
                await session.execute(
                    select(Message.text).where(Message.author == MessageAuthor.ASSISTANT)
                )
            ).scalars()
            secrets = [
                os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                os.environ.get("NVIDIA_API_KEY", ""),
                make_url(database_url).password or "",
            ]
            for text in assistant_texts:
                if text and FORBIDDEN_UX.search(text):
                    raise RuntimeError("assistant response exposed internal implementation details")
                if text and any(secret and secret in text for secret in secrets):
                    raise RuntimeError("assistant response exposed a configured credential")

            (await ConsistencyVerifier(session).verify()).require_ok()
        print("Telegram response privacy and DB consistency verification passed")
    finally:
        await engine.dispose()


async def main() -> int:
    try:
        await verify()
    except Exception as exc:
        print(f"Telegram smoke verification failed safely: {type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
