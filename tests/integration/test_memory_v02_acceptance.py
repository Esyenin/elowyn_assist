"""Real Hindsight/PostgreSQL behavioral acceptance for Memory v0.2."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.types import Message as TelegramMessage
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.base import Base
from elowyn.db.models import (
    Entity,
    Event,
    MemoryIngestionReceipt,
    MemoryObservation,
    MemoryPage,
    Message,
)
from elowyn.domain.enums import ObservationStatus, SemanticCategory
from elowyn.memory.generation import ActiveGenerationMemoryService
from elowyn.memory.hindsight import (
    BACKEND_NAME,
    HindsightAdapter,
    HindsightBackendFactory,
    HindsightConfig,
)
from elowyn.memory.service import (
    EpistemicStatus,
    MemorySource,
    RecallQuery,
    RetainMessage,
)
from elowyn.runtime import ElowynRuntime
from elowyn.services.context_composer import ContextComposer, ContextComposerConfig
from elowyn.services.memory_consolidation import MemoryPageService
from elowyn.services.memory_pipeline import MemoryIngestionProcessor, MemoryPipelineConfig
from elowyn.services.memory_provenance import MemoryProvenanceService
from elowyn.services.memory_rebuild import MemoryGenerationManager, MemoryRebuildConfig
from elowyn.transport.telegram import TelegramAdapter

pytestmark = [pytest.mark.hindsight, pytest.mark.postgres]


def _prompt_text(messages) -> str:
    chunks: list[str] = []
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                chunks.append(content)
            elif content is not None:
                chunks.append(str(content))
    return "\n".join(chunks)


class _PromptModel:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []
        self.tools: set[str] = set()
        self.model = FunctionModel(self._respond)

    def _respond(self, messages, info):
        self.prompts.append(_prompt_text(messages))
        self.tools.update(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart(self.answer)])


class _ExactSourceModel:
    def __init__(self, source_ref: str, raw_text: str, recall_query: str) -> None:
        self.source_ref = source_ref
        self.raw_text = raw_text
        self.recall_query = recall_query
        self.calls = 0
        self.tools: set[str] = set()
        self.model = FunctionModel(self._respond)

    def _respond(self, messages, info):
        self.calls += 1
        self.tools.update(tool.name for tool in info.function_tools)
        if self.calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="recall_long_term_memory",
                        args={"query": self.recall_query},
                    )
                ]
            )
        if self.calls == 2:
            assert self.source_ref in _prompt_text(messages)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="lookup_exact_memory_source",
                        args={"source_ref": self.source_ref},
                    )
                ]
            )
        assert self.raw_text in _prompt_text(messages)
        return ModelResponse(parts=[TextPart(f'Exact archive wording: "{self.raw_text}"')])


def _telegram_message(*, message_id: int, chat_id: int, text_value: str) -> TelegramMessage:
    return TelegramMessage.model_validate(
        {
            "message_id": message_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": 424242,
                "is_bot": False,
                "first_name": "Synthetic",
            },
            "text": text_value,
        }
    )


async def _turn(
    runtime: ElowynRuntime,
    adapter: TelegramAdapter,
    *,
    message_id: int,
    chat_id: int,
    text_value: str,
) -> str:
    response = await runtime.handle_message(
        adapter.to_incoming(
            _telegram_message(
                message_id=message_id,
                chat_id=chat_id,
                text_value=text_value,
            )
        )
    )
    assert response is not None
    return response


async def _drain(processor: MemoryIngestionProcessor) -> None:
    while await processor.process_once():
        pass


async def _wait_for_message(memory, message_id: uuid.UUID, query: str):
    for _ in range(30):
        result = await memory.recall(RecallQuery(text=query, max_tokens=1024))
        matching = [
            item
            for item in result.memories
            if item.provenance is not None and item.provenance.message_id == message_id
        ]
        if matching:
            return matching[0]
        await asyncio.sleep(1)
    raise AssertionError("real Hindsight did not return the expected canonical source")


async def _wait_for_conversation(memory, conversation_id: uuid.UUID, query: str):
    for _ in range(30):
        result = await memory.recall(RecallQuery(text=query, max_tokens=1024))
        matching = [
            item
            for item in result.memories
            if item.provenance is not None
            and item.provenance.conversation_id == conversation_id
        ]
        if matching:
            return matching[0]
        await asyncio.sleep(1)
    raise AssertionError("real Hindsight did not return the expected conversation document")


async def _isolated_source_recall(factory, message: Message):
    isolated = await factory.create_clean(f"elowyn-semantic-{uuid.uuid4()}")
    try:
        retained = RetainMessage(
            source=MemorySource(
                conversation_id=message.conversation_id,
                message_id=message.id,
                role=message.author.value,
                occurred_at=message.sent_at,
            ),
            text=message.text or "",
        )
        await isolated.retain((retained,))
        return await _wait_for_message(isolated, message.id, message.text or "")
    finally:
        await isolated.close()


async def _isolated_source_service(factory, message: Message):
    isolated = await factory.create_clean(f"elowyn-exact-{uuid.uuid4()}")
    retained = RetainMessage(
        source=MemorySource(
            conversation_id=message.conversation_id,
            message_id=message.id,
            role=message.author.value,
            occurred_at=message.sent_at,
        ),
        text=message.text or "",
    )
    await isolated.retain((retained,))
    recalled = await _wait_for_message(isolated, message.id, message.text or "")
    return isolated, recalled.text


def _restart_hindsight(container_name: str, base_url: str) -> None:
    subprocess.run(
        ["docker", "restart", container_name],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health/ready", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise AssertionError("Hindsight did not become ready after restart")


@pytest.mark.asyncio
async def test_memory_v02_behavioral_acceptance_and_telegram_multisession() -> None:
    base_url = os.getenv("ELOWYN_TEST_HINDSIGHT_URL")
    database_url = os.getenv("TEST_DATABASE_URL")
    container_name = os.getenv("ELOWYN_TEST_HINDSIGHT_CONTAINER")
    if not base_url or not database_url or not container_name:
        pytest.skip("real Hindsight/PostgreSQL acceptance environment is not configured")

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    factory = HindsightBackendFactory(base_url=base_url, timeout_seconds=180.0)
    bank_id = f"elowyn-acceptance-{uuid.uuid4()}"
    created = await factory.create_clean(bank_id)
    await created.close()
    backend = f"{BACKEND_NAME}:acceptance:{uuid.uuid4()}"
    manager = MemoryGenerationManager(
        session_factory,
        factory,
        MemoryRebuildConfig(
            backend=backend,
            bank_prefix="elowyn-acceptance-rebuild",
            batch_size=4,
            verification_attempts=30,
            verification_delay_seconds=1,
        ),
    )
    await manager.bootstrap_existing(bank_id)
    memory = ActiveGenerationMemoryService(
        session_factory,
        backend=backend,
        factory=factory,
    )
    processor = MemoryIngestionProcessor(
        session_factory,
        memory,
        MemoryPipelineConfig(
            backend=backend,
            batch_size=4,
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(seconds=2),
        ),
    )
    adapter = TelegramAdapter(allowed_user_id=424242)
    accepted: set[int] = set()
    exact_raw = 'Use the blue notebook, not the summary.'

    try:
        conversation_a_model = _PromptModel("Understood; synthetic memory evidence recorded.")
        conversation_a = ElowynRuntime(
            session_factory=session_factory,
            model=conversation_a_model.model,
            memory_service=memory,
        )
        inputs = [
            "I prefer concise synthetic technical answers.",
            "I prefer concise synthetic technical answers.",
            "Maybe Elowyn could evaluate Neo4j later.",
            "Project Aurora used SQLite in the prototype.",
            "Correction: Project Aurora now uses PostgreSQL.",
            exact_raw,
            "The balcony tomatoes need watering.",
        ]
        for message_id, user_text in enumerate(inputs, start=1):
            await _turn(
                conversation_a,
                adapter,
                message_id=message_id,
                chat_id=91005 if user_text == exact_raw else 91001,
                text_value=user_text,
            )
        await _drain(processor)

        async with session_factory() as session:
            user_messages = list(
                (
                    await session.execute(
                        select(Message)
                        .where(Message.text.in_(inputs))
                        .order_by(Message.sent_at, Message.created_at, Message.id)
                    )
                )
                .scalars()
                .all()
            )
            by_text = {message.text: message for message in user_messages}
            preference_observation = (
                await session.execute(
                    select(MemoryObservation).where(
                        MemoryObservation.category == SemanticCategory.PREFERENCE,
                        MemoryObservation.status == ObservationStatus.ACTIVE,
                    )
                )
            ).scalar_one()
            page = (await session.execute(select(MemoryPage))).scalars().first()
            page_views = await MemoryPageService(session).list_pages()
            assert preference_observation.confidence == 1.0
            assert page is not None
            assert page_views and all(view.authoritative is False for view in page_views)
            accepted.add(2)

            entity_count_before = int(
                await session.scalar(select(func.count()).select_from(Entity)) or 0
            )
            event_count_before = int(
                await session.scalar(select(func.count()).select_from(Event)) or 0
            )

        idea_message = by_text[inputs[2]]
        idea = await _isolated_source_recall(factory, idea_message)
        assert idea.semantics.category == SemanticCategory.IDEA
        assert idea.semantics.status == EpistemicStatus.CONSIDERED
        assert idea.authoritative is False
        accepted.add(3)

        correction_message = by_text[inputs[4]]
        old_message = by_text[inputs[3]]
        old_memory = await _isolated_source_recall(factory, old_message)
        current_memory = await _isolated_source_recall(factory, correction_message)
        assert current_memory.temporal.mentioned_at >= old_memory.temporal.mentioned_at
        assert current_memory.authoritative is False and old_memory.authoritative is False
        accepted.add(4)

        async with session_factory() as session:
            resolved = await MemoryProvenanceService(session).resolve_message(idea.provenance)
            assert resolved.id == idea_message.id and resolved.text == inputs[2]
        accepted.add(5)

        conversation_b_model = _PromptModel("You prefer concise synthetic technical answers.")
        conversation_b = ElowynRuntime(
            session_factory=session_factory,
            model=conversation_b_model.model,
            memory_service=memory,
        )
        response = await _turn(
            conversation_b,
            adapter,
            message_id=101,
            chat_id=91002,
            text_value="Do I prefer concise synthetic technical answers?",
        )
        assert "concise" in response
        assert "MEMORY (DERIVED, NON-AUTHORITATIVE" in conversation_b_model.prompts[-1]
        accepted.add(1)

        noise_model = _PromptModel("The balcony tomatoes need watering.")
        noise_runtime = ElowynRuntime(
            session_factory=session_factory,
            model=noise_model.model,
            memory_service=memory,
        )
        await _turn(
            noise_runtime,
            adapter,
            message_id=102,
            chat_id=91002,
            text_value="What needs watering on the balcony?",
        )
        assert "prefers concise" not in noise_model.prompts[-1].casefold()
        assert "neo4j" not in noise_model.prompts[-1].casefold()
        accepted.add(6)

        async with session_factory() as session:
            bounded = await ContextComposer(
                session,
                ContextComposerConfig(memory_token_budget=320, memory_item_limit=3),
            ).memory_context(
                user_text="Do I prefer concise synthetic technical answers?",
                world_state="{}",
                history=[],
            )
            assert bounded is not None
            assert bounded.token_upper_bound <= 320 and bounded.item_count <= 3
        accepted.add(7)

        unavailable = HindsightAdapter(
            config=HindsightConfig(
                base_url="http://127.0.0.1:1",
                bank_id=bank_id,
                timeout_seconds=0.2,
            )
        )
        outage_model = _PromptModel("Conversation continues while memory is unavailable.")
        outage_runtime = ElowynRuntime(
            session_factory=session_factory,
            model=outage_model.model,
            memory_service=unavailable,
        )
        outage_text = "The recovered backend remembers synthetic amber protocol context."
        assert "continues" in await _turn(
            outage_runtime,
            adapter,
            message_id=201,
            chat_id=91003,
            text_value=outage_text,
        )
        outage_processor = MemoryIngestionProcessor(
            session_factory,
            unavailable,
            MemoryPipelineConfig(
                backend=backend,
                retry_base=timedelta(seconds=1),
                retry_max=timedelta(seconds=1),
            ),
        )
        outage_time = datetime.now(UTC)
        assert await outage_processor.process_once(now=outage_time) is True
        await _drain_at(processor, outage_time + timedelta(seconds=2))
        async with session_factory() as session:
            outage_message = (
                await session.execute(select(Message).where(Message.text == outage_text))
            ).scalar_one()
            receipt_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryIngestionReceipt)
                    .where(MemoryIngestionReceipt.message_id == outage_message.id)
                )
                or 0
            )
            assert receipt_count == 1
        await _wait_for_conversation(
            memory,
            outage_message.conversation_id,
            "amber protocol context",
        )
        accepted.add(8)

        rebuilt = await manager.rebuild(explicit=True)
        assert rebuilt.bank_id != bank_id
        rebuilt_idea = await _wait_for_conversation(
            memory,
            idea_message.conversation_id,
            inputs[2],
        )
        assert rebuilt_idea.provenance is not None
        async with session_factory() as session:
            assert int(await session.scalar(select(func.count()).select_from(MemoryPage)) or 0)
        accepted.add(9)

        async with session_factory() as session:
            confident_storage_pages = [
                entry
                for page_view in await MemoryPageService(session).list_pages()
                for entry in page_view.entries
                if "aurora" in entry.statement.casefold()
            ]
            assert confident_storage_pages == []
        assert old_memory.authoritative is False and current_memory.authoritative is False
        accepted.add(12)

        exact_message = by_text[exact_raw]
        exact_memory, exact_recall_query = await _isolated_source_service(factory, exact_message)
        exact_model = _ExactSourceModel(
            exact_message_source_ref(exact_message),
            exact_raw,
            exact_recall_query,
        )
        exact_runtime = ElowynRuntime(
            session_factory=session_factory,
            model=exact_model.model,
            memory_service=exact_memory,
        )
        try:
            exact_response = await _turn(
                exact_runtime,
                adapter,
                message_id=301,
                chat_id=91002,
                text_value="What exactly did I say about the blue notebook?",
            )
            assert exact_raw in exact_response
            assert exact_model.calls == 3
            assert "lookup_exact_memory_source" in exact_model.tools
            assert not any(
                tool.startswith(("create_", "update_", "revoke_"))
                for tool in exact_model.tools
            )
        finally:
            await exact_memory.close()
        accepted.add(13)

        async with session_factory() as session:
            assert int(await session.scalar(select(func.count()).select_from(Entity)) or 0) == (
                entity_count_before
            )
            assert int(await session.scalar(select(func.count()).select_from(Event)) or 0) == (
                event_count_before
            )
        accepted.update((10, 11))

        await memory.close()
        await asyncio.to_thread(_restart_hindsight, container_name, base_url)
        memory = ActiveGenerationMemoryService(
            session_factory,
            backend=backend,
            factory=factory,
        )
        restarted = await _wait_for_conversation(
            memory,
            idea_message.conversation_id,
            inputs[2],
        )
        assert restarted.provenance is not None
        restart_model = _PromptModel("After restart: concise technical answers.")
        restart_runtime = ElowynRuntime(
            session_factory=session_factory,
            model=restart_model.model,
            memory_service=memory,
        )
        assert "After restart" in await _turn(
            restart_runtime,
            adapter,
            message_id=401,
            chat_id=91004,
            text_value="Do I prefer concise synthetic technical answers?",
        )

        assert accepted == set(range(1, 14))
    finally:
        await memory.close()
        await engine.dispose()


def exact_message_source_ref(message: Message) -> str:
    return f"elowyn:message:{message.id}"


async def _drain_at(processor: MemoryIngestionProcessor, now: datetime) -> None:
    while await processor.process_once(now=now):
        pass
