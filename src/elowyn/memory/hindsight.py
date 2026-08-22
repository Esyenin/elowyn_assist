from __future__ import annotations

import asyncio
import importlib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from elowyn.memory.service import (
    MemoryBackendError,
    MemoryHealth,
    MemorySource,
    RecalledMemory,
    RecallQuery,
    RecallResult,
    Reflection,
    ReflectQuery,
    RetainMessage,
    RetainResult,
)

HINDSIGHT_API_VERSION = "0.9.1"
HINDSIGHT_CLIENT_VERSION = "0.9.1"
METADATA_SCHEMA_VERSION = "elowyn-memory-source-v1"
BACKEND_NAME = f"hindsight-{HINDSIGHT_API_VERSION}"
_OPERATION_NAMESPACE = uuid.UUID("5a3b4e0f-53bd-47e9-b4d1-7aab32af6af6")


@dataclass(frozen=True)
class HindsightConfig:
    base_url: str
    bank_id: str
    api_key: str | None = None
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("Hindsight base URL must not be blank")
        if not self.bank_id.strip():
            raise ValueError("Hindsight bank ID must not be blank")
        if self.timeout_seconds <= 0:
            raise ValueError("Hindsight timeout must be positive")


class HindsightAdapter:
    """Translate Elowyn DTOs to the pinned, replaceable Hindsight API."""

    def __init__(self, config: HindsightConfig, *, client: Any | None = None) -> None:
        self.config = config
        self._client = client if client is not None else self._build_client(config)

    @staticmethod
    def _build_client(config: HindsightConfig) -> Any:
        try:
            module = importlib.import_module("hindsight_client")
        except ImportError as exc:
            raise RuntimeError(
                f"HindsightAdapter requires hindsight-client=={HINDSIGHT_CLIENT_VERSION}"
            ) from exc
        return module.Hindsight(
            base_url=config.base_url.rstrip("/"),
            api_key=config.api_key,
            timeout=config.timeout_seconds,
            user_agent="elowyn-memory/0.2",
        )

    async def health(self) -> MemoryHealth:
        try:
            await self._client.monitoring.get_readiness(
                _request_timeout=self.config.timeout_seconds
            )
            version = await self._client.aget_version()
            api_version = str(version.api_version)
            if api_version != HINDSIGHT_API_VERSION:
                return MemoryHealth(
                    backend=BACKEND_NAME,
                    ready=False,
                    api_version=api_version,
                    detail=f"expected Hindsight API {HINDSIGHT_API_VERSION}",
                )
            return MemoryHealth(
                backend=BACKEND_NAME,
                ready=True,
                api_version=api_version,
            )
        except Exception as exc:
            return MemoryHealth(
                backend=BACKEND_NAME,
                ready=False,
                detail=f"{type(exc).__name__}: backend unavailable",
            )

    async def retain(
        self,
        messages: tuple[RetainMessage, ...],
        *,
        operation_id: uuid.UUID | None = None,
    ) -> RetainResult:
        if not messages:
            raise ValueError("retain requires at least one message")
        conversation_id = messages[0].source.conversation_id
        if any(message.source.conversation_id != conversation_id for message in messages):
            raise ValueError("a retain batch must belong to one conversation")
        if any(not message.text.strip() for message in messages):
            raise ValueError("retained message text must not be blank")

        await self._require_pinned_version()
        stable_operation_id = operation_id or operation_id_for(self.config.bank_id, messages)
        items = [_retain_item(message) for message in messages]
        try:
            response = await self._client.aretain_batch(
                bank_id=self.config.bank_id,
                items=items,
                document_id=document_id_for(conversation_id),
                document_tags=["elowyn", "conversation"],
                retain_async=True,
                operation_id=str(stable_operation_id),
            )
        except Exception as exc:
            raise MemoryBackendError("Hindsight retain failed") from exc
        if not bool(response.success):
            raise MemoryBackendError("Hindsight rejected retain")
        returned_operation_id = getattr(response, "operation_id", None)
        if returned_operation_id and str(returned_operation_id) != str(stable_operation_id):
            raise MemoryBackendError("Hindsight returned an unexpected operation ID")
        await self._wait_for_retain(stable_operation_id)
        return RetainResult(
            operation_id=stable_operation_id,
            accepted_items=int(response.items_count),
        )

    async def recall(self, query: RecallQuery) -> RecallResult:
        if not query.text.strip():
            raise ValueError("recall query must not be blank")
        if query.max_tokens < 1:
            raise ValueError("recall max_tokens must be positive")
        await self._require_pinned_version()
        try:
            response = await self._client.arecall(
                bank_id=self.config.bank_id,
                query=query.text,
                types=list(query.kinds),
                max_tokens=query.max_tokens,
                query_timestamp=_iso_timestamp(query.query_timestamp)
                if query.query_timestamp
                else None,
                tags=list(query.tags) or None,
                tags_match=query.tags_match,
                include_source_facts=False,
                prefer_observations=False,
            )
        except Exception as exc:
            raise MemoryBackendError("Hindsight recall failed") from exc
        return RecallResult(memories=tuple(_map_recalled(item) for item in response.results))

    async def reflect(self, query: ReflectQuery) -> Reflection:
        if not query.text.strip():
            raise ValueError("reflect query must not be blank")
        if query.max_tokens < 1:
            raise ValueError("reflect max_tokens must be positive")
        await self._require_pinned_version()
        try:
            response = await self._client.areflect(
                bank_id=self.config.bank_id,
                query=query.text,
                max_tokens=query.max_tokens,
                tags=list(query.tags) or None,
                tags_match=query.tags_match,
                fact_types=list(query.kinds),
                include_facts=True,
            )
        except Exception as exc:
            raise MemoryBackendError("Hindsight reflect failed") from exc
        based_on = getattr(response, "based_on", None)
        memories = getattr(based_on, "memories", None) or ()
        evidence_ids = tuple(str(item.id) for item in memories if getattr(item, "id", None))
        return Reflection(text=str(response.text), evidence_backend_ids=evidence_ids)

    async def close(self) -> None:
        await self._client.aclose()

    async def _require_pinned_version(self) -> None:
        try:
            response = await self._client.aget_version()
        except Exception as exc:
            raise MemoryBackendError("Hindsight version check failed") from exc
        actual = str(response.api_version)
        if actual != HINDSIGHT_API_VERSION:
            raise MemoryBackendError(
                f"unsupported Hindsight API version {actual}; expected {HINDSIGHT_API_VERSION}"
            )

    async def _wait_for_retain(self, operation_id: uuid.UUID) -> None:
        deadline = time.monotonic() + self.config.timeout_seconds
        try:
            while time.monotonic() < deadline:
                operation = await self._client.operations.get_operation_status(
                    self.config.bank_id,
                    str(operation_id),
                    _request_timeout=self.config.timeout_seconds,
                )
                status = str(operation.status).lower()
                if status == "completed":
                    return
                if status in {"failed", "cancelled"}:
                    raise MemoryBackendError(f"Hindsight retain operation {status}")
                await asyncio.sleep(0.25)
        except MemoryBackendError:
            raise
        except Exception as exc:
            raise MemoryBackendError("Hindsight retain status check failed") from exc
        raise MemoryBackendError("Hindsight retain operation timed out")


def document_id_for(conversation_id: uuid.UUID) -> str:
    return f"elowyn:conversation:{conversation_id}"


def operation_id_for(bank_id: str, messages: tuple[RetainMessage, ...]) -> uuid.UUID:
    message_ids = ",".join(sorted(str(message.source.message_id) for message in messages))
    return uuid.uuid5(_OPERATION_NAMESPACE, f"{bank_id}:{message_ids}")


def _retain_item(message: RetainMessage) -> dict[str, Any]:
    source = message.source
    role = source.role.strip().upper()
    role_tag = role.lower()
    timestamp = _require_aware(source.occurred_at)
    tags = sorted(
        {
            "elowyn",
            f"conversation:{source.conversation_id}",
            f"role:{role_tag}",
            *(f"topic:{tag.strip().lower()}" for tag in message.topic_tags if tag.strip()),
        }
    )
    return {
        "content": message.text,
        "timestamp": timestamp,
        "context": f"Elowyn conversation message; role={role_tag}",
        "document_id": document_id_for(source.conversation_id),
        "metadata": {
            "conversation_id": str(source.conversation_id),
            "message_id": str(source.message_id),
            "role": role,
            "timestamp": _iso_timestamp(timestamp),
            "extraction_schema_version": METADATA_SCHEMA_VERSION,
            "source_type": "conversation_message",
        },
        "tags": tags,
        "update_mode": "append",
    }


def _map_recalled(item: Any) -> RecalledMemory:
    metadata = {str(key): str(value) for key, value in (item.metadata or {}).items()}
    source = _source_from_metadata(metadata)
    return RecalledMemory(
        backend_id=str(item.id),
        text=str(item.text),
        kind=str(item.type) if item.type is not None else None,
        document_id=str(item.document_id) if item.document_id is not None else None,
        source=source,
        metadata=metadata,
        tags=tuple(str(tag) for tag in (item.tags or ())),
    )


def _source_from_metadata(metadata: dict[str, str]) -> MemorySource | None:
    try:
        return MemorySource(
            conversation_id=uuid.UUID(metadata["conversation_id"]),
            message_id=uuid.UUID(metadata["message_id"]),
            role=metadata["role"],
            occurred_at=datetime.fromisoformat(metadata["timestamp"]),
        )
    except (KeyError, ValueError):
        return None


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("memory source timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _iso_timestamp(value: datetime) -> str:
    return _require_aware(value).isoformat().replace("+00:00", "Z")
