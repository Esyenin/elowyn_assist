from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from elowyn.memory.hindsight import (
    METADATA_SCHEMA_VERSION,
    HindsightAdapter,
    HindsightConfig,
    document_id_for,
    operation_id_for,
)
from elowyn.memory.service import (
    MemoryBackendError,
    MemorySource,
    RecallQuery,
    ReflectQuery,
    RetainMessage,
)


class FakeMonitoring:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def get_readiness(self, **kwargs: Any) -> object:
        if self.error:
            raise self.error
        return {"status": "healthy"}


class FakeHindsight:
    def __init__(
        self,
        *,
        version: str = "0.9.1",
        health_error: Exception | None = None,
        operation_status: str = "completed",
    ) -> None:
        self.version = version
        self.operation_status = operation_status
        self.monitoring = FakeMonitoring(error=health_error)
        self.operations = SimpleNamespace(get_operation_status=self._operation_status)
        self.retain_calls: list[dict[str, Any]] = []
        self.recall_calls: list[dict[str, Any]] = []
        self.reflect_calls: list[dict[str, Any]] = []
        self.closed = False

    async def aget_version(self) -> object:
        return SimpleNamespace(api_version=self.version)

    async def aretain_batch(self, **kwargs: Any) -> object:
        self.retain_calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            items_count=len(kwargs["items"]),
            operation_id=kwargs["operation_id"],
        )

    async def _operation_status(self, *args: Any, **kwargs: Any) -> object:
        return SimpleNamespace(status=self.operation_status)

    async def arecall(self, **kwargs: Any) -> object:
        self.recall_calls.append(kwargs)
        metadata = self.retain_calls[0]["items"][0]["metadata"]
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    id="fact-1",
                    text="Synthetic preference",
                    type="world",
                    document_id=self.retain_calls[0]["document_id"],
                    metadata=metadata,
                    tags=["elowyn"],
                )
            ]
        )

    async def areflect(self, **kwargs: Any) -> object:
        self.reflect_calls.append(kwargs)
        fact = SimpleNamespace(id="fact-1")
        return SimpleNamespace(
            text="Synthetic synthesis", based_on=SimpleNamespace(memories=[fact])
        )

    async def aclose(self) -> None:
        self.closed = True


def _message() -> RetainMessage:
    return RetainMessage(
        source=MemorySource(
            conversation_id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
            message_id=uuid.UUID("20000000-0000-0000-0000-000000000002"),
            role="USER",
            occurred_at=datetime(2026, 8, 22, 8, 30, tzinfo=UTC),
        ),
        text="Synthetic preference",
        topic_tags=("Preferences",),
    )


@pytest.mark.asyncio
async def test_adapter_maps_stable_source_metadata_and_retry_identity() -> None:
    client = FakeHindsight()
    adapter = HindsightAdapter(
        HindsightConfig(base_url="http://memory.invalid", bank_id="elowyn-test"),
        client=client,
    )
    messages = (_message(),)

    first = await adapter.retain(messages)
    retry = await adapter.retain(messages)

    assert first.operation_id == retry.operation_id == operation_id_for("elowyn-test", messages)
    assert len(client.retain_calls) == 2
    call = client.retain_calls[0]
    assert call["retain_async"] is True
    assert call["operation_id"] == str(first.operation_id)
    assert call["document_id"] == document_id_for(messages[0].source.conversation_id)
    item = call["items"][0]
    assert item["update_mode"] == "append"
    assert item["timestamp"] == datetime(2026, 8, 22, 8, 30, tzinfo=UTC)
    assert item["tags"] == [
        "conversation:10000000-0000-0000-0000-000000000001",
        "elowyn",
        "role:user",
        "topic:preferences",
    ]
    assert item["metadata"] == {
        "conversation_id": "10000000-0000-0000-0000-000000000001",
        "message_id": "20000000-0000-0000-0000-000000000002",
        "role": "USER",
        "timestamp": "2026-08-22T08:30:00Z",
        "extraction_schema_version": METADATA_SCHEMA_VERSION,
        "source_type": "conversation_message",
    }


@pytest.mark.asyncio
async def test_recall_and_reflect_are_explicitly_non_authoritative() -> None:
    client = FakeHindsight()
    adapter = HindsightAdapter(
        HindsightConfig(base_url="http://memory.invalid", bank_id="elowyn-test"),
        client=client,
    )
    await adapter.retain((_message(),))

    recalled = await adapter.recall(RecallQuery(text="What is preferred?", max_tokens=256))
    reflected = await adapter.reflect(ReflectQuery(text="Summarize preferences", max_tokens=256))

    assert recalled.authoritative is False
    assert recalled.memories[0].authoritative is False
    assert recalled.memories[0].source == _message().source
    assert client.recall_calls[0]["prefer_observations"] is False
    assert client.recall_calls[0]["include_source_facts"] is False
    assert reflected.authoritative is False
    assert reflected.evidence_backend_ids == ("fact-1",)
    assert client.reflect_calls[0]["include_facts"] is True


@pytest.mark.asyncio
async def test_health_surfaces_failure_without_backend_exception_or_secret() -> None:
    client = FakeHindsight(health_error=RuntimeError("secret-token"))
    adapter = HindsightAdapter(
        HindsightConfig(
            base_url="http://memory.invalid",
            bank_id="elowyn-test",
            api_key="secret-token",
        ),
        client=client,
    )

    health = await adapter.health()

    assert health.ready is False
    assert health.detail == "RuntimeError: backend unavailable"
    assert "secret-token" not in health.detail


@pytest.mark.asyncio
async def test_wrong_server_version_is_not_ready_and_blocks_operations() -> None:
    adapter = HindsightAdapter(
        HindsightConfig(base_url="http://memory.invalid", bank_id="elowyn-test"),
        client=FakeHindsight(version="0.9.2"),
    )

    health = await adapter.health()

    assert health.ready is False
    assert health.api_version == "0.9.2"
    with pytest.raises(MemoryBackendError, match="expected 0.9.1"):
        await adapter.retain((_message(),))


@pytest.mark.asyncio
async def test_failed_async_retain_is_not_reported_as_success() -> None:
    adapter = HindsightAdapter(
        HindsightConfig(base_url="http://memory.invalid", bank_id="elowyn-test"),
        client=FakeHindsight(operation_status="failed"),
    )

    with pytest.raises(MemoryBackendError, match="operation failed"):
        await adapter.retain((_message(),))
