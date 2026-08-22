"""Real Hindsight 0.9.1 API smoke using synthetic conversation data only.

Run against an isolated Hindsight instance. The script intentionally accepts only
an HTTP endpoint: it has no Elowyn database configuration or SQL access.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from hindsight_client import Hindsight

EXPECTED_API_VERSION = "0.9.1"
BANK_ID = f"elowyn-slice1-synthetic-{uuid4()}"
DOCUMENT_ID = "elowyn:conversation:00000000-0000-0000-0000-000000000201"
TAG = "elowyn-slice1"


def _text(item: object) -> str:
    return str(getattr(item, "text", getattr(item, "content", item)))


async def _wait_for_observation(client: Hindsight, *, timeout_seconds: float) -> list[object]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        recalled = await client.arecall(
            BANK_ID,
            "How does the user prefer technical answers?",
            types=["observation"],
            tags=[TAG],
            tags_match="all_strict",
            include_source_facts=True,
            max_tokens=1024,
        )
        if recalled.results:
            return list(recalled.results)
        await asyncio.sleep(1)
    raise AssertionError("observation consolidation did not finish before timeout")


async def smoke(base_url: str, *, timeout_seconds: float) -> None:
    client = Hindsight(base_url=base_url, timeout=timeout_seconds)
    version = await client.aget_version()
    api_version = str(version.api_version)
    assert api_version == EXPECTED_API_VERSION, (api_version, EXPECTED_API_VERSION)

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds) as http:
        health = await http.get("/health/ready")
        health.raise_for_status()

    await client.acreate_bank(
        BANK_ID,
        name="Elowyn Slice 1 synthetic smoke",
        mission="Retain synthetic conversation facts and consolidate durable preferences.",
    )
    await client.aretain_batch(
        BANK_ID,
        document_id=DOCUMENT_ID,
        document_tags=[TAG, "conversation"],
        items=[
            {
                "content": "USER: I prefer concise technical answers with concrete evidence.",
                "timestamp": datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
                "context": "Synthetic Elowyn conversation message",
                "metadata": {
                    "elowyn_conversation_id": "00000000-0000-0000-0000-000000000201",
                    "elowyn_message_id": "00000000-0000-0000-0000-000000000301",
                    "role": "user",
                },
                "tags": [TAG, "role:user"],
            },
            {
                "content": "ASSISTANT: I will keep technical answers concise and evidence-based.",
                "timestamp": datetime(2026, 8, 20, 9, 1, tzinfo=UTC),
                "context": "Synthetic Elowyn conversation message",
                "metadata": {
                    "elowyn_conversation_id": "00000000-0000-0000-0000-000000000201",
                    "elowyn_message_id": "00000000-0000-0000-0000-000000000302",
                    "role": "assistant",
                },
                "tags": [TAG, "role:assistant"],
            },
            {
                "content": "USER: Again, please lead with the result and cite the evidence.",
                "timestamp": datetime(2026, 8, 21, 10, 0, tzinfo=UTC),
                "context": "Synthetic Elowyn conversation message",
                "metadata": {
                    "elowyn_conversation_id": "00000000-0000-0000-0000-000000000201",
                    "elowyn_message_id": "00000000-0000-0000-0000-000000000303",
                    "role": "user",
                },
                "tags": [TAG, "role:user"],
            },
        ],
    )

    recalled = await client.arecall(
        BANK_ID,
        "What answer style does the user prefer?",
        types=["world", "experience"],
        tags=[TAG],
        tags_match="all_strict",
        include_chunks=True,
        max_tokens=1024,
    )
    recalled_text = "\n".join(_text(item) for item in recalled.results).casefold()
    assert recalled.results and ("concise" in recalled_text or "evidence" in recalled_text)

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds) as http:
        consolidation = await http.post(f"/v1/default/banks/{BANK_ID}/consolidate", json={})
        consolidation.raise_for_status()
        assert consolidation.json().get("operation_id")

    observations = await _wait_for_observation(client, timeout_seconds=timeout_seconds)
    reflected = await client.areflect(
        BANK_ID,
        "Summarize the user's technical communication preference.",
        budget="low",
        tags=[TAG],
        tags_match="all_strict",
        include_facts=True,
    )
    assert str(reflected.text).strip()

    print(
        "PASS",
        {
            "api_version": api_version,
            "health": health.json().get("status"),
            "recalled": len(recalled.results),
            "observations": len(observations),
            "reflect_answer_present": True,
            "document_id": DOCUMENT_ID,
        },
    )
    await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8888")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args()
    asyncio.run(smoke(args.base_url.rstrip("/"), timeout_seconds=args.timeout_seconds))


if __name__ == "__main__":
    main()
