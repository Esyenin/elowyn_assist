from __future__ import annotations

import asyncio
import math
import re
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.assistant.context import BoundedMemoryContext
from elowyn.db.models import Message
from elowyn.domain.enums import ObservationStatus
from elowyn.memory.service import MemoryService, RecalledMemory, RecallQuery
from elowyn.services.memory_consolidation import MemoryPageService

DEFAULT_MEMORY_TOKEN_BUDGET = 512
DEFAULT_MEMORY_RECALL_TOKEN_LIMIT = 256
DEFAULT_MEMORY_RECALL_TIMEOUT_SECONDS = 0.75
DEFAULT_MEMORY_ITEM_LIMIT = 6

_HEADER = (
    "MEMORY (DERIVED, NON-AUTHORITATIVE; current user statement and WORLD STATE win):"
)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = {
    "about",
    "again",
    "also",
    "and",
    "are",
    "для",
    "его",
    "или",
    "как",
    "мне",
    "можно",
    "мой",
    "моя",
    "на",
    "the",
    "this",
    "что",
    "это",
    "with",
    "you",
}
_EXPLICIT_UPDATE_MARKERS = {
    "actually",
    "correction",
    "instead",
    "now",
    "not",
    "prefer",
    "больше",
    "исправление",
    "нет",
    "предпочитаю",
    "теперь",
}
_QUESTION_MARKERS = {
    "what",
    "which",
    "who",
    "why",
    "как",
    "какой",
    "когда",
    "помнишь",
    "почему",
    "что",
}


@dataclass(frozen=True)
class ContextComposerConfig:
    memory_token_budget: int = DEFAULT_MEMORY_TOKEN_BUDGET
    recall_token_limit: int = DEFAULT_MEMORY_RECALL_TOKEN_LIMIT
    recall_timeout_seconds: float = DEFAULT_MEMORY_RECALL_TIMEOUT_SECONDS
    memory_item_limit: int = DEFAULT_MEMORY_ITEM_LIMIT

    def __post_init__(self) -> None:
        if self.memory_token_budget < _token_upper_bound(_HEADER):
            raise ValueError("memory token budget is too small for the authority label")
        if self.recall_token_limit < 1 or self.memory_item_limit < 1:
            raise ValueError("memory recall and item limits must be positive")
        if self.recall_timeout_seconds <= 0:
            raise ValueError("memory recall timeout must be positive")


@dataclass(frozen=True)
class _RankedMemory:
    text: str
    relevance: float
    stable_key: str


class ContextComposer:
    """Select a small, relevant, non-authoritative memory fast path."""

    def __init__(
        self,
        session: AsyncSession,
        memory_service: MemoryService | None,
        config: ContextComposerConfig | None = None,
    ) -> None:
        self.session = session
        self.memory_service = memory_service
        self.config = config or ContextComposerConfig()

    async def memory_context(
        self,
        *,
        user_text: str,
        world_state: str,
        history: list[Message],
    ) -> BoundedMemoryContext | None:
        query_terms = _terms(user_text)
        if not query_terms:
            return None
        recent_text = "\n".join(message.text or "" for message in history)
        recent_ids = {message.id for message in history}
        ranked = await self._page_candidates(
            query_terms=query_terms,
            user_text=user_text,
            world_state=world_state,
            recent_text=recent_text,
        )
        if not ranked and self.memory_service is not None:
            ranked = await self._recall_candidates(
                user_text=user_text,
                query_terms=query_terms,
                world_state=world_state,
                recent_text=recent_text,
                recent_ids=recent_ids,
            )
        return _bounded_context(ranked, self.config)

    async def _page_candidates(
        self,
        *,
        query_terms: set[str],
        user_text: str,
        world_state: str,
        recent_text: str,
    ) -> list[_RankedMemory]:
        pages = await MemoryPageService(self.session).list_pages()
        ranked: list[_RankedMemory] = []
        for page in pages:
            page_terms = _terms(f"{page.title} {page.scope_key}")
            for entry in page.entries:
                candidate_terms = _terms(
                    f"{entry.claim_key} {entry.statement} {' '.join(page_terms)}"
                )
                relevance = _relevance(query_terms, candidate_terms)
                if relevance <= 0 or _shadowed_by_current(
                    entry.statement,
                    user_text=user_text,
                    world_state=world_state,
                    recent_text=recent_text,
                ):
                    continue
                qualifier = "ACTIVE"
                if entry.status == ObservationStatus.CONTESTED:
                    qualifier = f"CONTESTED/UNCERTAIN confidence={entry.confidence:.2f}"
                ranked.append(
                    _RankedMemory(
                        text=f"- [{qualifier}] {page.title}: {entry.statement}",
                        relevance=relevance
                        + (0.1 if entry.status == ObservationStatus.ACTIVE else 0),
                        stable_key=str(entry.observation_id),
                    )
                )
        return _deduplicated(ranked)

    async def _recall_candidates(
        self,
        *,
        user_text: str,
        query_terms: set[str],
        world_state: str,
        recent_text: str,
        recent_ids: set[uuid.UUID],
    ) -> list[_RankedMemory]:
        assert self.memory_service is not None
        try:
            async with asyncio.timeout(self.config.recall_timeout_seconds):
                result = await self.memory_service.recall(
                    RecallQuery(
                        text=user_text,
                        max_tokens=min(
                            self.config.recall_token_limit,
                            self.config.memory_token_budget,
                        ),
                    )
                )
        except Exception:
            # Memory is an optional derived layer. Backend timeout/outage must not fail the turn.
            return []
        ranked: list[_RankedMemory] = []
        for memory in result.memories:
            if memory.source is not None and memory.source.message_id in recent_ids:
                continue
            relevance = _relevance(query_terms, _terms(memory.text))
            if relevance <= 0 or _shadowed_by_current(
                memory.text,
                user_text=user_text,
                world_state=world_state,
                recent_text=recent_text,
            ):
                continue
            ranked.append(
                _RankedMemory(
                    text=_recalled_line(memory),
                    relevance=relevance,
                    stable_key=memory.backend_id,
                )
            )
        return _deduplicated(ranked)


def _bounded_context(
    ranked: list[_RankedMemory], config: ContextComposerConfig
) -> BoundedMemoryContext | None:
    if not ranked:
        return None
    selected: list[str] = []
    for item in sorted(ranked, key=lambda value: (-value.relevance, value.stable_key)):
        if len(selected) >= config.memory_item_limit:
            break
        candidate = "\n".join((_HEADER, *selected, item.text))
        if _token_upper_bound(candidate) <= config.memory_token_budget:
            selected.append(item.text)
    if not selected:
        return None
    text = "\n".join((_HEADER, *selected))
    return BoundedMemoryContext(
        text=text,
        token_upper_bound=_token_upper_bound(text),
        item_count=len(selected),
    )


def _recalled_line(memory: RecalledMemory) -> str:
    qualifier = f"{memory.semantics.category.value}/{memory.semantics.status.value}"
    return f"- [{qualifier}; NON-AUTHORITATIVE] {memory.text.strip()}"


def _terms(value: str) -> set[str]:
    return {
        term
        for term in (match.casefold() for match in _WORD.findall(value))
        if len(term) >= 3 and term not in _STOP_WORDS
    }


def _relevance(query: set[str], candidate: set[str]) -> float:
    overlap = query & candidate
    if not overlap:
        return 0.0
    return len(overlap) / math.sqrt(len(query) * len(candidate))


def _shadowed_by_current(
    statement: str,
    *,
    user_text: str,
    world_state: str,
    recent_text: str,
) -> bool:
    normalized = _normalized(statement)
    if normalized and (
        normalized in _normalized(world_state) or normalized in _normalized(recent_text)
    ):
        return True
    user_terms = _terms(user_text)
    statement_terms = _terms(statement)
    is_question = "?" in user_text or bool(user_terms & _QUESTION_MARKERS)
    explicit_update = not is_question and bool(user_terms & _EXPLICIT_UPDATE_MARKERS)
    if explicit_update and statement_terms:
        overlap = len(user_terms & statement_terms) / len(statement_terms)
        return overlap >= 0.25
    return False


def _deduplicated(items: list[_RankedMemory]) -> list[_RankedMemory]:
    result: dict[str, _RankedMemory] = {}
    for item in items:
        key = _normalized(item.text.partition("]")[2])
        previous = result.get(key)
        if previous is None or item.relevance > previous.relevance:
            result[key] = item
    return list(result.values())


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _token_upper_bound(value: str) -> int:
    # Hindsight/provider tokenizers are replaceable. UTF-8 bytes are a conservative
    # upper bound for byte-level BPE token count and require no provider dependency.
    return len(value.encode("utf-8"))
