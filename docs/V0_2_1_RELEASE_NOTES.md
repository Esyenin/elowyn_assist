# Elowyn v0.2.1 — Memory hardening

`v0.2.1` is a scoped bugfix release over `v0.2.0`. It adds no new Memory
semantics, retrieval framework, or World State authority.

## Fixed

- Blank, whitespace-only, and non-text archive Messages now receive an
  idempotent `IGNORED_BLANK` ingestion receipt. They never reach Hindsight and
  cannot block later Messages in the same Conversation.
- Successful retain now durably marks Elowyn-owned derived state dirty until
  ConversationSummary, observations, and Memory Pages have been refreshed from
  the canonical raw archive. Failed refreshes use the existing bounded retry
  policy and can be reconciled after restart without repeating retain.
- Failed and superseded Hindsight generations can be diagnosed and removed by
  an explicit maintenance action. The active registry target and all BUILDING
  generations are protected from deletion; a failed backend deletion preserves
  the Core generation journal for retry.

## Operator action

Orphan cleanup is opt-in and uses the existing memory maintenance entrypoint:

```text
ELOWYN_ALLOW_MEMORY_CLEANUP=YES python -m elowyn.memory.rebuild_cli --confirm-orphan-cleanup
```

The command only considers Core-journaled `FAILED` and `SUPERSEDED` generations.
It does not touch the raw Conversation/Message archive, World State, or Event
history.

## Compatibility and scope

- Hindsight remains pinned to `0.9.1` and replaceable behind `MemoryService`.
- Migration `0006_memory_hardening` preserves existing receipts as `INGESTED`
  and adds only retry/diagnostic state owned by Elowyn Core.
- Memory remains non-authoritative and has no domain write authority.
