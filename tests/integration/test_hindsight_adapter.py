from __future__ import annotations

import importlib
import os
import uuid
from datetime import UTC, datetime

import pytest

from elowyn.memory.hindsight import HindsightAdapter, HindsightConfig, document_id_for
from elowyn.memory.service import MemorySource, RecallQuery, ReflectQuery, RetainMessage

pytestmark = pytest.mark.hindsight


@pytest.mark.asyncio
async def test_real_hindsight_091_retain_retry_recall_reflect() -> None:
    base_url = os.getenv("ELOWYN_TEST_HINDSIGHT_URL")
    if not base_url:
        pytest.skip("ELOWYN_TEST_HINDSIGHT_URL is not configured")
    module = importlib.import_module("hindsight_client")
    client = module.Hindsight(base_url=base_url, timeout=120.0)
    bank_id = f"elowyn-adapter-{uuid.uuid4()}"
    await client.acreate_bank(bank_id, name="Elowyn synthetic adapter test")
    adapter = HindsightAdapter(
        HindsightConfig(base_url=base_url, bank_id=bank_id, timeout_seconds=120.0),
        client=client,
    )
    conversation_id = uuid.uuid4()
    message = RetainMessage(
        source=MemorySource(
            conversation_id=conversation_id,
            message_id=uuid.uuid4(),
            role="USER",
            occurred_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
        ),
        text="Synthetic user prefers cedar tea during morning planning.",
        topic_tags=("preferences",),
    )

    try:
        health = await adapter.health()
        assert health.ready is True
        assert health.api_version == "0.9.1"

        first = await adapter.retain((message,))
        retry = await adapter.retain((message,))
        assert retry.operation_id == first.operation_id

        recalled = await adapter.recall(
            RecallQuery(text="Which tea is preferred for morning planning?", max_tokens=1024)
        )
        matching = [item for item in recalled.memories if item.source == message.source]
        assert matching
        assert matching[0].document_id == document_id_for(conversation_id)
        assert matching[0].metadata["extraction_schema_version"] == "elowyn-memory-source-v1"
        assert matching[0].authoritative is False

        reflected = await adapter.reflect(
            ReflectQuery(text="What is the synthetic morning planning preference?", max_tokens=512)
        )
        assert reflected.text.strip()
        assert reflected.authoritative is False
    finally:
        await adapter.close()
