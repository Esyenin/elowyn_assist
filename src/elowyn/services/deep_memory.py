from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from elowyn.db.models import Message
from elowyn.memory.deep import (
    DeepMemoryRoute,
    DeepRecallItem,
    DeepRecallView,
    DeepReflectionView,
    ExactSourceContextMessage,
    ExactSourceView,
)
from elowyn.memory.service import MemoryProvenance, MemoryService, RecallQuery, ReflectQuery
from elowyn.services.memory_provenance import MemoryProvenanceService

DEFAULT_DEEP_QUERY_BUDGET = 512
DEFAULT_DEEP_RECALL_BUDGET = 1536
DEFAULT_DEEP_REFLECT_BUDGET = 2048
DEFAULT_EXACT_SOURCE_BUDGET = 2048
DEFAULT_DEEP_BACKEND_TOKEN_LIMIT = 1024
DEFAULT_DEEP_RESULT_LIMIT = 6
DEFAULT_DEEP_TIMEOUT_SECONDS = 8.0

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_RECALL_HEADER = (
    "DEEP MEMORY (DERIVED, NON-AUTHORITATIVE; preserve contradictions/history; "
    "current user statement and WORLD STATE win):"
)
_REFLECT_HEADER = (
    "MEMORY SYNTHESIS (DERIVED, NON-AUTHORITATIVE; not an exact quote or WORLD STATE):"
)
_STOP_WORDS = {
    "about",
    "and",
    "did",
    "для",
    "или",
    "как",
    "мне",
    "the",
    "this",
    "what",
    "when",
    "which",
    "что",
    "это",
    "you",
}
_EXACT_PHRASES = (
    "exactly did i say",
    "exact words",
    "what did i say exactly",
    "какими словами",
    "точно я сказал",
    "точно я говорила",
    "что именно я сказал",
    "что именно я говорила",
    "цитат",
)
_REFLECT_PHRASES = (
    "across our conversations",
    "historical pattern",
    "over time",
    "recurring pattern",
    "за всё время",
    "историческ",
    "как менял",
    "паттерн",
    "повторя",
    "тенденц",
)
_RECALL_PHRASES = (
    "did i",
    "do you remember",
    "earlier conversation",
    "last time",
    "last ",
    "previous conversation",
    "what did i prefer",
    "когда-то",
    "говорил",
    "говорила",
    "помнишь",
    "прошл",
    "раньше",
    "стар",
    "тогда",
)


@dataclass(frozen=True)
class DeepMemoryConfig:
    query_budget: int = DEFAULT_DEEP_QUERY_BUDGET
    recall_budget: int = DEFAULT_DEEP_RECALL_BUDGET
    reflect_budget: int = DEFAULT_DEEP_REFLECT_BUDGET
    exact_source_budget: int = DEFAULT_EXACT_SOURCE_BUDGET
    backend_token_limit: int = DEFAULT_DEEP_BACKEND_TOKEN_LIMIT
    result_limit: int = DEFAULT_DEEP_RESULT_LIMIT
    timeout_seconds: float = DEFAULT_DEEP_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        budgets = (
            self.query_budget,
            self.recall_budget,
            self.reflect_budget,
            self.exact_source_budget,
            self.backend_token_limit,
            self.result_limit,
        )
        if any(value < 1 for value in budgets) or self.timeout_seconds <= 0:
            raise ValueError("deep memory limits must be positive")
        if self.recall_budget <= _token_upper_bound(_RECALL_HEADER):
            raise ValueError("deep recall budget is too small")
        if self.reflect_budget <= _token_upper_bound(_REFLECT_HEADER):
            raise ValueError("deep reflect budget is too small")


def route_deep_memory(user_text: str) -> DeepMemoryRoute:
    normalized = " ".join(user_text.casefold().split())
    russian_exact = "именно" in normalized and any(
        word in normalized for word in ("говорил", "говорила", "сказал", "сказала")
    )
    if russian_exact or any(phrase in normalized for phrase in _EXACT_PHRASES):
        return DeepMemoryRoute.EXACT_SOURCE
    if any(phrase in normalized for phrase in _REFLECT_PHRASES):
        return DeepMemoryRoute.REFLECT
    if any(phrase in normalized for phrase in _RECALL_PHRASES):
        return DeepMemoryRoute.RECALL
    return DeepMemoryRoute.NONE


class DeepMemoryService:
    """Bounded, read-only deep retrieval over the replaceable MemoryService."""

    def __init__(
        self,
        session: AsyncSession,
        memory_service: MemoryService,
        config: DeepMemoryConfig | None = None,
    ) -> None:
        self.session = session
        self.memory_service = memory_service
        self.config = config or DeepMemoryConfig()
        self._allowed_sources: dict[str, MemoryProvenance] = {}

    async def recall(self, query: str) -> DeepRecallView:
        query = " ".join(query.split())
        if not query or _token_upper_bound(query) > self.config.query_budget:
            return _recall_failure("Deep-memory query is empty or exceeds its budget.")
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                recalled = await self.memory_service.recall(
                    RecallQuery(text=query, max_tokens=self.config.backend_token_limit)
                )
        except Exception:
            return _recall_failure("Deep memory is temporarily unavailable; do not infer details.")

        query_terms = _terms(query)
        relevant = [
            item
            for item in recalled.memories
            if item.provenance is not None and _relevance(query_terms, _terms(item.text)) > 0
        ]
        selected: list[DeepRecallItem] = []
        lines: list[str] = []
        text_was_truncated = False
        for memory in relevant:
            if len(selected) >= self.config.result_limit:
                break
            assert memory.provenance is not None
            clipped, clipped_flag = _clip_utf8(memory.text.strip(), 320)
            line = (
                f"- [{memory.semantics.category.value}/{memory.semantics.status.value}; "
                f"NON-AUTHORITATIVE; at={memory.temporal.mentioned_at.isoformat()}; "
                f"source={memory.provenance.source_ref}] {clipped}"
            )
            candidate = "\n".join((_RECALL_HEADER, *lines, line))
            if _token_upper_bound(candidate) > self.config.recall_budget:
                continue
            lines.append(line)
            selected.append(
                DeepRecallItem(
                    backend_id=memory.backend_id,
                    text=clipped,
                    semantics=memory.semantics,
                    provenance=memory.provenance,
                    mentioned_at=memory.temporal.mentioned_at,
                    text_truncated=clipped_flag,
                )
            )
            self._allowed_sources[memory.provenance.source_ref] = memory.provenance
            text_was_truncated = text_was_truncated or clipped_flag
        context = "\n".join((_RECALL_HEADER, *lines))
        truncated = text_was_truncated or len(selected) < len(relevant)
        return DeepRecallView(
            available=True,
            context=context,
            items=tuple(selected),
            token_upper_bound=_token_upper_bound(context),
            truncated=truncated,
        )

    async def reflect(self, query: str) -> DeepReflectionView:
        query = " ".join(query.split())
        if not query or _token_upper_bound(query) > self.config.query_budget:
            return _reflection_failure("Memory synthesis query is empty or exceeds its budget.")
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                reflection = await self.memory_service.reflect(
                    ReflectQuery(text=query, max_tokens=self.config.backend_token_limit)
                )
        except Exception:
            return _reflection_failure(
                "Memory synthesis is temporarily unavailable; do not invent a pattern."
            )
        available = self.config.reflect_budget - _token_upper_bound(_REFLECT_HEADER + "\n")
        synthesis, truncated = _clip_utf8(reflection.text.strip(), available)
        text = f"{_REFLECT_HEADER}\n{synthesis}"
        return DeepReflectionView(
            available=True,
            synthesis=text,
            evidence_backend_ids=reflection.evidence_backend_ids[:12],
            token_upper_bound=_token_upper_bound(text),
            truncated=truncated,
        )

    async def exact_source(self, source_ref: str) -> ExactSourceView:
        provenance = self._allowed_sources.get(source_ref.strip())
        if provenance is None:
            return _source_not_found(source_ref)
        try:
            message = await MemoryProvenanceService(self.session).resolve_message(provenance)
        except LookupError:
            return _source_not_found(source_ref)
        raw_text = message.text or ""
        clipped, truncated = _clip_utf8(raw_text, self.config.exact_source_budget)
        remaining = self.config.exact_source_budget - _token_upper_bound(clipped)
        surrounding, context_complete = await self._surrounding_context(message, remaining)
        return ExactSourceView(
            found=True,
            source_ref=provenance.source_ref,
            conversation_id=message.conversation_id,
            message_id=message.id,
            role=message.author.value,
            sent_at=message.sent_at,
            raw_text=clipped,
            surrounding_context=surrounding,
            token_upper_bound=_token_upper_bound(clipped)
            + sum(_token_upper_bound(item.raw_text) for item in surrounding),
            truncated=truncated,
            context_complete=context_complete,
            canonical_raw_source=True,
        )

    async def _surrounding_context(
        self, source: Message, budget: int
    ) -> tuple[tuple[ExactSourceContextMessage, ...], bool]:
        before_rows = (
            (
                await self.session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == source.conversation_id,
                        Message.id != source.id,
                        Message.sent_at < source.sent_at,
                    )
                    .order_by(Message.sent_at.desc(), Message.created_at.desc())
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        before = list(reversed(before_rows))
        after = (
            (
                await self.session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == source.conversation_id,
                        Message.id != source.id,
                        Message.sent_at > source.sent_at,
                    )
                    .order_by(Message.sent_at, Message.created_at)
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        neighbors = [*before, *after]
        result: list[ExactSourceContextMessage] = []
        complete = True
        for message in neighbors:
            if budget <= 0:
                complete = False
                break
            raw_text = message.text or ""
            clipped, truncated = _clip_utf8(raw_text, min(320, budget))
            result.append(
                ExactSourceContextMessage(
                    message_id=message.id,
                    role=message.author.value,
                    sent_at=message.sent_at,
                    raw_text=clipped,
                    text_truncated=truncated,
                )
            )
            budget -= _token_upper_bound(clipped)
            complete = complete and not truncated
        return tuple(result), complete and len(result) == len(neighbors)


def _recall_failure(detail: str) -> DeepRecallView:
    return DeepRecallView(
        available=False,
        context=detail,
        items=(),
        token_upper_bound=_token_upper_bound(detail),
        truncated=False,
    )


def _reflection_failure(detail: str) -> DeepReflectionView:
    return DeepReflectionView(
        available=False,
        synthesis=detail,
        evidence_backend_ids=(),
        token_upper_bound=_token_upper_bound(detail),
        truncated=False,
    )


def _source_not_found(source_ref: str) -> ExactSourceView:
    return ExactSourceView(
        found=False,
        source_ref=source_ref.strip(),
        conversation_id=None,
        message_id=None,
        role=None,
        sent_at=None,
        raw_text=None,
    )


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


def _clip_utf8(value: str, budget: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= budget:
        return value, False
    marker = "…"
    marker_size = len(marker.encode("utf-8"))
    clipped = encoded[: max(0, budget - marker_size)]
    while clipped:
        try:
            return clipped.decode("utf-8") + marker, True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return marker if budget >= marker_size else "", True


def _token_upper_bound(value: str) -> int:
    return len(value.encode("utf-8"))
