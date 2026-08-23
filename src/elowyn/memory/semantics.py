from __future__ import annotations

import re
from datetime import datetime

from elowyn.memory.service import (
    EpistemicStatus,
    MemorySemantics,
    SemanticCategory,
)

SEMANTIC_SCHEMA_VERSION = "elowyn-memory-semantics-v1"

_IDEA = re.compile(
    r"\b(maybe|perhaps|possibly|could|might|consider|what if|idea|"
    r"может быть|возможно|попробуем|стоит ли|идея)\b",
    re.IGNORECASE,
)
_DECIDED = re.compile(
    r"\b(decided|we will|adopted|chosen|решил[аи]?|решили|выбрали|будем)\b",
    re.IGNORECASE,
)
_PREFERENCE = re.compile(
    r"\b(prefer|prefers|preferred|preference|rather|like|likes|favorite|"
    r"предпочитаю|предпочитает|предпочтение|"
    r"нравится|люблю|любимый)\b",
    re.IGNORECASE,
)
_CONSTRAINT = re.compile(
    r"\b(must|cannot|can't|need to|required|constraint|нельзя|не могу|"
    r"должен|должна|обязательно|ограничение)\b",
    re.IGNORECASE,
)
_EPISODE = re.compile(
    r"\b(yesterday|last week|last month|met|visited|happened|"
    r"вчера|на прошлой неделе|в прошлом месяце|встретил[аи]?|случилось)\b",
    re.IGNORECASE,
)
_CURRENT_CONTEXT = re.compile(
    r"\b(currently|right now|at the moment|working on|now|"
    r"сейчас|в данный момент|работаю над|текущий контекст)\b",
    re.IGNORECASE,
)


def classify_semantics(
    text: str,
    *,
    backend_kind: str | None = None,
    occurred_start: datetime | None = None,
) -> MemorySemantics:
    """Conservative message-level hint; Hindsight remains the fact extractor."""
    if _IDEA.search(text):
        return MemorySemantics(SemanticCategory.IDEA, EpistemicStatus.CONSIDERED)
    if _DECIDED.search(text):
        return MemorySemantics(SemanticCategory.CONTEXT, EpistemicStatus.DECIDED)
    if _PREFERENCE.search(text):
        return MemorySemantics(SemanticCategory.PREFERENCE, EpistemicStatus.PREFERRED)
    if _CONSTRAINT.search(text):
        return MemorySemantics(SemanticCategory.CONSTRAINT, EpistemicStatus.CURRENTLY_TRUE)
    if occurred_start is not None or backend_kind == "experience" or _EPISODE.search(text):
        return MemorySemantics(SemanticCategory.EPISODE, EpistemicStatus.MENTIONED)
    if _CURRENT_CONTEXT.search(text):
        return MemorySemantics(SemanticCategory.CONTEXT, EpistemicStatus.CURRENTLY_TRUE)
    if backend_kind == "observation":
        return MemorySemantics(SemanticCategory.OBSERVATION, EpistemicStatus.MENTIONED)
    return MemorySemantics(SemanticCategory.FACT, EpistemicStatus.MENTIONED)
