from __future__ import annotations

import asyncio
import os


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def _required_int(name: str) -> int:
    value = _required_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


async def main() -> None:
    from aiogram import Bot, Dispatcher

    from elowyn.db.session import SessionFactory
    from elowyn.runtime import ElowynRuntime
    from elowyn.transport.telegram import TelegramAdapter, build_router

    token = _required_env("TELEGRAM_BOT_TOKEN")
    model = _required_env("ELOWYN_MODEL")
    allowed_user_id = _required_int("TELEGRAM_ALLOWED_USER_ID")

    bot = Bot(token=token)
    dp = Dispatcher()
    runtime = ElowynRuntime(session_factory=SessionFactory, model=model)
    adapter = TelegramAdapter(allowed_user_id=allowed_user_id)
    dp.include_router(build_router(runtime.handle_message, adapter=adapter))

    # v0.1 is single-user; serial turn handling also protects conversation ordering.
    await dp.start_polling(bot, handle_as_tasks=False)


if __name__ == "__main__":
    asyncio.run(main())
