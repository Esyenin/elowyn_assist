from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import suppress


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
    from dotenv import load_dotenv

    # Load local development configuration before importing modules with process-level resources.
    load_dotenv(override=False)

    from aiogram import Bot, Dispatcher

    from elowyn.db.session import SessionFactory
    from elowyn.memory.generation import ActiveGenerationMemoryService
    from elowyn.memory.hindsight import BACKEND_NAME, HindsightBackendFactory
    from elowyn.provider import build_runtime_model
    from elowyn.runtime import ElowynRuntime
    from elowyn.services.memory_pipeline import (
        MemoryIngestionProcessor,
        MemoryIngestionWorker,
        MemoryPipelineConfig,
    )
    from elowyn.services.memory_rebuild import MemoryGenerationManager, MemoryRebuildConfig
    from elowyn.transport.telegram import TelegramAdapter, build_router

    token = _required_env("TELEGRAM_BOT_TOKEN")
    allowed_user_id = _required_int("TELEGRAM_ALLOWED_USER_ID")

    bot = Bot(token=token)
    dp = Dispatcher()
    runtime_model = build_runtime_model()
    memory_url = os.environ.get("HINDSIGHT_API_URL", "").strip()
    memory = None
    memory_worker = None
    memory_task = None
    if memory_url:
        bank_id = _required_env("HINDSIGHT_BANK_ID")
        factory = HindsightBackendFactory(
            base_url=memory_url,
            api_key=os.environ.get("HINDSIGHT_API_KEY") or None,
        )
        bank_generation = hashlib.sha256(bank_id.encode("utf-8")).hexdigest()[:16]
        pipeline_backend = f"{BACKEND_NAME}:{bank_generation}"
        await MemoryGenerationManager(
            SessionFactory,
            factory,
            MemoryRebuildConfig(backend=pipeline_backend, bank_prefix=bank_id),
        ).bootstrap_existing(bank_id)
        memory = ActiveGenerationMemoryService(
            SessionFactory,
            backend=pipeline_backend,
            factory=factory,
        )
        pipeline_config = MemoryPipelineConfig(backend=pipeline_backend)
        memory_worker = MemoryIngestionWorker(
            MemoryIngestionProcessor(SessionFactory, memory, pipeline_config)
        )
        memory_task = asyncio.create_task(memory_worker.run(), name="memory-catch-up")

    runtime = ElowynRuntime(
        session_factory=SessionFactory,
        model=runtime_model,
        memory_ingestion_wakeup=memory_worker.wake if memory_worker else None,
        memory_service=memory,
    )
    adapter = TelegramAdapter(allowed_user_id=allowed_user_id)
    dp.include_router(build_router(runtime.handle_message, adapter=adapter))

    # v0.1 is single-user; serial turn handling also protects conversation ordering.
    try:
        await dp.start_polling(bot, handle_as_tasks=False)
    finally:
        if memory_worker is not None:
            memory_worker.stop()
        if memory_task is not None:
            memory_task.cancel()
            with suppress(asyncio.CancelledError):
                await memory_task
        if memory is not None:
            await memory.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
