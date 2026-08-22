from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from sqlalchemy import select

from elowyn.db.models import MemoryBackendRegistry, MemoryGeneration
from elowyn.domain.enums import MemoryGenerationStatus
from elowyn.memory.service import (
    MemoryHealth,
    MemoryService,
    RecallQuery,
    RecallResult,
    Reflection,
    ReflectQuery,
    RetainMessage,
    RetainResult,
)


class MemoryBackendFactory(Protocol):
    def open(self, bank_id: str) -> MemoryService: ...

    async def create_clean(self, bank_id: str) -> MemoryService: ...


_Result = TypeVar("_Result")


class ActiveGenerationMemoryService:
    """Route operations through the Core-owned active-generation pointer."""

    def __init__(self, session_factory, *, backend: str, factory: MemoryBackendFactory):
        self.session_factory = session_factory
        self.backend = backend
        self.factory = factory
        self._services: dict[uuid.UUID, MemoryService] = {}
        self._cache_lock = asyncio.Lock()

    async def health(self) -> MemoryHealth:
        return await self._with_active(lambda service: service.health())

    async def retain(
        self,
        messages: tuple[RetainMessage, ...],
        *,
        operation_id: uuid.UUID | None = None,
    ) -> RetainResult:
        # Hold the registry row lock across retain. Final generation switching takes
        # the same lock, so no receipt can be confirmed against the old bank after switch.
        async with self.session_factory() as session:
            registry = (
                await session.execute(
                    select(MemoryBackendRegistry)
                    .where(MemoryBackendRegistry.backend == self.backend)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            generation = await self._generation(session, registry)
            service = await self._service(generation)
            result = await service.retain(messages, operation_id=operation_id)
            await session.commit()
            return result

    async def recall(self, query: RecallQuery) -> RecallResult:
        return await self._with_active(lambda service: service.recall(query))

    async def reflect(self, query: ReflectQuery) -> Reflection:
        return await self._with_active(lambda service: service.reflect(query))

    async def close(self) -> None:
        async with self._cache_lock:
            services = tuple(self._services.values())
            self._services.clear()
        for service in services:
            await service.close()

    async def _with_active(
        self, operation: Callable[[MemoryService], Awaitable[_Result]]
    ) -> _Result:
        async with self.session_factory() as session:
            registry = await session.get(MemoryBackendRegistry, self.backend)
            generation = await self._generation(session, registry)
        return await operation(await self._service(generation))

    async def _generation(self, session, registry) -> MemoryGeneration:
        if registry is None or registry.active_generation_id is None:
            raise RuntimeError("active memory generation is not configured")
        generation = await session.get(MemoryGeneration, registry.active_generation_id)
        if (
            generation is None
            or generation.backend != self.backend
            or generation.status != MemoryGenerationStatus.ACTIVE
        ):
            raise RuntimeError("active memory generation pointer is invalid")
        return generation

    async def _service(self, generation: MemoryGeneration) -> MemoryService:
        async with self._cache_lock:
            service = self._services.get(generation.id)
            if service is None:
                service = self.factory.open(generation.bank_id)
                self._services[generation.id] = service
            return service
