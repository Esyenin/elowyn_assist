from __future__ import annotations

import argparse
import asyncio
import hashlib
import os


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Explicit non-destructive Elowyn memory rebuild")
    parser.add_argument("--confirm-rebuild", action="store_true")
    args = parser.parse_args()
    if not args.confirm_rebuild or os.environ.get("ELOWYN_ALLOW_MEMORY_REBUILD") != "YES":
        raise SystemExit(
            "rebuild requires --confirm-rebuild and ELOWYN_ALLOW_MEMORY_REBUILD=YES"
        )

    from dotenv import load_dotenv

    load_dotenv(override=False)
    base_url = os.environ.get("HINDSIGHT_API_URL", "").strip()
    bank_id = os.environ.get("HINDSIGHT_BANK_ID", "").strip()
    if not base_url or not bank_id:
        raise SystemExit("HINDSIGHT_API_URL and HINDSIGHT_BANK_ID must be configured")

    from elowyn.db.session import SessionFactory
    from elowyn.memory.hindsight import BACKEND_NAME, HindsightBackendFactory
    from elowyn.services.memory_rebuild import MemoryGenerationManager, MemoryRebuildConfig

    backend = f"{BACKEND_NAME}:{hashlib.sha256(bank_id.encode('utf-8')).hexdigest()[:16]}"
    manager = MemoryGenerationManager(
        SessionFactory,
        HindsightBackendFactory(
            base_url=base_url,
            api_key=os.environ.get("HINDSIGHT_API_KEY") or None,
        ),
        MemoryRebuildConfig(backend=backend, bank_prefix=bank_id),
    )
    await manager.bootstrap_existing(bank_id)
    result = await manager.rebuild(explicit=True)
    print(f"active generation {result.generation_id}; replayed {result.messages_replayed}")


if __name__ == "__main__":
    asyncio.run(_run())
