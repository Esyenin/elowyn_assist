from __future__ import annotations

from typing import Any

from elowyn.memory.deep import DeepMemoryRoute
from elowyn.services.deep_memory import DeepMemoryService


def deep_memory_policy(route: DeepMemoryRoute) -> str:
    return f"""

## Deep memory for this turn

Route: {route.value}. Deep memory is read-only, derived, and non-authoritative.
Use a provided deep-memory tool only to answer the explicit historical request.
Current user statements and current World State always win over recalled history.
Preserve contradictions as uncertainty/history. Never mutate World State from memory alone.
For exact wording, first recall candidates, then call exact source lookup
and quote only its raw text.
Reflection is synthesis, never an exact quote or source lookup.
"""


def register_deep_memory_tools(
    agent: Any,
    service: DeepMemoryService,
    route: DeepMemoryRoute,
) -> None:
    if route in (DeepMemoryRoute.RECALL, DeepMemoryRoute.EXACT_SOURCE):

        @agent.tool_plain(sequential=True)
        async def recall_long_term_memory(query: str) -> dict[str, object]:
            """Recall a bounded specific old fact; results are historical and non-authoritative."""
            result = await service.recall(query)
            return {
                "available": result.available,
                "memory_context": result.context,
                "item_count": len(result.items),
                "truncated": result.truncated,
                "authoritative": False,
            }

        @agent.tool_plain(sequential=True)
        async def lookup_exact_memory_source(source_ref: str) -> dict[str, object]:
            """Resolve a source_ref from recall to the canonical raw Message for exact wording."""
            result = await service.exact_source(source_ref)
            return {
                "found": result.found,
                "source_ref": result.source_ref,
                "conversation_id": str(result.conversation_id)
                if result.conversation_id
                else None,
                "message_id": str(result.message_id) if result.message_id else None,
                "role": result.role,
                "sent_at": result.sent_at.isoformat() if result.sent_at else None,
                "raw_text": result.raw_text,
                "surrounding_context": [
                    {
                        "message_id": str(item.message_id),
                        "role": item.role,
                        "sent_at": item.sent_at.isoformat(),
                        "raw_text": item.raw_text,
                        "truncated": item.text_truncated,
                    }
                    for item in result.surrounding_context
                ],
                "token_upper_bound": result.token_upper_bound,
                "truncated": result.truncated,
                "context_complete": result.context_complete,
                "canonical_raw_source": result.canonical_raw_source,
                "world_state_authority": False,
            }

    if route == DeepMemoryRoute.REFLECT:

        @agent.tool_plain(sequential=True)
        async def reflect_on_memory_history(query: str) -> dict[str, object]:
            """Synthesize a broad historical pattern; never use this as an exact quote."""
            result = await service.reflect(query)
            return {
                "available": result.available,
                "synthesis": result.synthesis,
                "evidence_count": len(result.evidence_backend_ids),
                "truncated": result.truncated,
                "authoritative": False,
                "exact_quote": False,
            }
