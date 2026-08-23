# Elowyn — Codex working instructions

This file is intentionally compact. It is the stable operating contract for coding work, not a replacement for the master document.

## Authority order

1. `docs/personal_ai_assistant_master_v0.6.docx` — authoritative project decisions and requirements.
2. Active version spec in `docs/specs/` — compact implementation contract derived from the master.
3. This `AGENTS.md` — stable coding/safety/workflow rules.
4. Existing code/tests — implementation evidence, not architectural authority.

If an active spec appears to conflict with the master, stop only for a real architectural/product conflict. Do not invent a new interpretation.

## Context discipline

For ordinary v0.2 work:
1. read this file;
2. read `docs/specs/MEMORY_V0_2.md`;
3. use `docs/ARCHITECTURE_INDEX.md` to open only relevant master sections;
4. inspect only relevant code/tests;
5. broaden scope only when evidence requires it.

Do not re-read the entire master or repository for every task.

## Frozen v0.1.0 baseline

`v0.1.0` is a stable foundation. Do not use v0.2 as a reason for aesthetic refactors of Core.

Preserve:
- typed domain write boundary;
- PostgreSQL authoritative World State;
- Operation/Event/Source provenance;
- corrections and append-only undo;
- Decision superseding;
- strict/controlled semantic relations;
- concurrency protections;
- restricted runtime DB role;
- recovery/consistency guarantees;
- transport-independent Core;
- real Telegram/provider vertical.

If a real v0.1 bug/security issue is found, report it separately.

## v0.2 core invariants

- v0.2 = **Long-term Memory**.
- Memory != Conversation History.
- Memory != World State.
- Memory != Event Log.
- Memory != Plan.
- Memory has **no direct write authority** over World State.
- Any canonical Task/Project/Goal/Decision change still goes through existing domain actions/Core validation/Event+Source.
- Memory may influence reasoning or suggest a change, but cannot silently mutate canonical state.
- Any Approved Plan or material revision requires explicit user approval.
- Raw Conversation/Message remains canonical raw conversational archive.
- Semantic memory must be derived/rebuildable from raw archive.
- Hindsight is a replaceable backend behind Elowyn-owned `MemoryService` / adapter boundaries.
- Hindsight must not receive ownership/write access over Core World State/history.
- Memory ingestion must not be a critical synchronous dependency of the user turn.
- Context composition must use a strict token budget; do not dump raw recall into every prompt.
- Provenance must allow navigation from high-level memory back to original Conversation/Message.
- Do not add Graphiti, LangGraph, Temporal, Celery, Kafka, OR-Tools, workers, ActivityWatch, full Behavioral Model, graph DB, etc. without a concrete v0.2 need.

## Hindsight rule

Before implementation depending on Hindsight:
- inspect current official docs/current stable releases;
- pin a concrete version;
- verify the exact retain/recall/reflect/observation/mental-model semantics used;
- integration-test the actual pinned version;
- prefer adapter/workaround/version change over violating Elowyn architecture.

Do not use `latest` blindly.

## Task size

Implement v0.2 in small coherent vertical increments. Prefer one PR-sized slice at a time.

Suggested sequence is in `docs/specs/MEMORY_V0_2.md`.

After a committed slice, prefer a fresh Codex task/session.

## Testing

During work:
- run the narrowest relevant tests first;
- do not run the entire DB/recovery/security suite after every local edit.

Before commit:
- run relevant regression tests and lint/type checks.

Before merge/release or when a change touches shared safety guarantees:
- run the full CI-equivalent suite and applicable recovery/security gates.

v0.2 is not complete because unit tests pass; final multi-session E2E acceptance is mandatory.

## Secrets and user data

- Never print/commit `.env`, API keys, Telegram token, DB passwords, private keys, secret-bearing DSNs.
- Use synthetic data for hosted prototype/free LLM endpoints unless privacy policy explicitly changes.
- Do not log raw secrets or provider credentials.
- Keep `.env` ignored.

## Escalation policy

Do not ask the user about minor implementation choices that can be safely decided from the spec/code.

Ask only if:
- product behavior must change;
- an approved invariant must be violated;
- a serious trade-off depends on user preference;
- a dependency requires a fundamental architecture change.

## Final report

Unless asked otherwise, keep reports short:
- Changed
- Tests/checks
- Bugs found/fixed
- Open issues/risks
- Branch/commit
