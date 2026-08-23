from __future__ import annotations

import argparse
import asyncio
import hashlib
import os


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Explicit Elowyn memory maintenance")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--confirm-rebuild", action="store_true")
    action.add_argument("--confirm-orphan-cleanup", action="store_true")
    args = parser.parse_args()
    if args.confirm_rebuild and os.environ.get("ELOWYN_ALLOW_MEMORY_REBUILD") != "YES":
        raise SystemExit(
            "rebuild requires --confirm-rebuild and ELOWYN_ALLOW_MEMORY_REBUILD=YES"
        )
    if args.confirm_orphan_cleanup and os.environ.get("ELOWYN_ALLOW_MEMORY_CLEANUP") != "YES":
        raise SystemExit(
            "cleanup requires --confirm-orphan-cleanup and ELOWYN_ALLOW_MEMORY_CLEANUP=YES"
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
    if args.confirm_rebuild:
        result = await manager.rebuild(explicit=True)
        print(f"active generation {result.generation_id}; replayed {result.messages_replayed}")
        return
    candidates = await manager.cleanup_candidates()
    cleaned = await manager.cleanup_orphans(
        tuple(item.generation_id for item in candidates),
        explicit=True,
    )
    print(f"removed {len(cleaned)} failed/superseded memory generation(s)")


if __name__ == "__main__":
    asyncio.run(_run())
