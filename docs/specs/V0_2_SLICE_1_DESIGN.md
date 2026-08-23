# Elowyn v0.2 Slice 1 — extension points and Hindsight feasibility

Status: feasibility design; no Memory subsystem schema or production adapter is implemented here.

## v0.1 extension points

- End of turn: `ElowynRuntime.handle_message()` records the assistant `Message` and commits it. Slice 4 should record durable ingestion work in the same Core transaction as that assistant message; actual Hindsight I/O starts only after commit and never decides whether the Telegram reply succeeds.
- Context composition: `assistant.context.build_turn_prompt()` is the single current assembly point for timestamp, authoritative World State, recent `Message` history, and user text. Slice 7 should replace its growing argument list with an Elowyn-owned context DTO/composer and add a separately budgeted memory section. Hindsight results must not be passed as World State.
- Derived Core tables: ORM models extend `elowyn.db.models.Base`; Alembic imports that metadata in `alembic/env.py`. Slice 2 adds only Core-owned derived summary and ingestion state in a new migration.
- Runtime dependencies: `app.main()` is the composition root for `SessionFactory`, provider model, `ElowynRuntime`, and Telegram router. Slice 3 constructs `MemoryService`/adapter here and injects the interface into runtime/context composition.
- Catch-up: Telegram polling currently uses serial `handle_as_tasks=False`. It must stay independent of memory availability. A small lifecycle-owned catch-up loop (or separate process using the same service) claims durable pending rows with bounded batches and retry/backoff. No Celery/Temporal/Kafka is needed.

## Pinned Hindsight candidate

Pin server image/API to `ghcr.io/vectorize-io/hindsight:0.9.1` and the feasibility client to `hindsight-client==0.9.1`. Tag `v0.9.1` is the newest published repository tag observed on 2026-08-22. Pinning the server, not adding the all-in-one runtime to Elowyn Core, preserves process and dependency isolation.

Verified 0.9.1 contract:

- async Python `aretain[_batch]`, `arecall`, and `areflect` plus version discovery;
- retain accepts content, event timestamp, context, string metadata, tags, entities, stable `document_id`, `update_mode` (`append`/`replace`), and caller operation identity for async retries;
- recall filters `world`, `experience`, and `observation`, supports tag scopes, temporal query anchors, source facts for observations, and bounded token results;
- observations are asynchronously consolidated, evidence-linked derived beliefs; consolidation may create/update observations and preserve observation history. It is eventual, not a retain response guarantee;
- mental models are precomputed, explicitly refreshed/scheduled synthesized views. They are a backend facility and may inform, but do not own, Elowyn Memory Pages;
- reflect is synthesized reasoning over retrieved memory/mental models and is never authoritative truth;
- `/health/live` is process liveness; `/health/ready` and `/health` test database readiness. Queue/operation state and consolidation recovery are separate operational signals;
- self-hosting requires PostgreSQL 14+ with a supported vector extension (embedded pg0 is development-oriented; external PostgreSQL is preferred for production), an extraction/reasoning LLM, and persistent storage. Hindsight is MIT licensed.

Document behavior: the same `document_id` groups retained content. `append` adds content; `replace` replaces the document-derived content and therefore is unsafe as a default incremental-message operation. Elowyn should use a stable conversation document identity plus append-only message batches and its own cursor/idempotency state. Full rebuild should target a fresh bank/version, not depend on in-place replacement semantics.

## Design for Slices 2–10

### Slice 2 — Core-owned metadata

Add `conversation_summaries` (one row per conversation: summary, topics/related entity IDs as bounded secondary metadata, last processed message, updated time, derivation version) and `memory_ingestion_state`/outbox rows (conversation/backend, through-message cursor or batch range, status, attempts, retry time, sanitized last error, operation/idempotency key, timestamps). Both are derived/rebuildable; raw `Message` remains canonical.

### Slice 3 — boundary and adapter

Define Elowyn-owned DTOs and `MemoryService` protocol under `elowyn.memory`; implement an HTTP `HindsightAdapter` outside domain/Core. Capabilities: retain batch, recall, reflect, page/mental-model read, health, and rebuild target management. The adapter receives message DTOs, never an SQLAlchemy session and never Core DB credentials.

### Slice 4 — ingestion and catch-up

Write/advance outbox state transactionally after assistant-message persistence. A bounded catch-up runner reads raw messages after the durable cursor, calls async retain with stable operation identity, then advances the cursor in a new Core transaction. Retry with capped exponential backoff; backend failure leaves conversation and Telegram response successful.

### Slice 5 — semantics and provenance

Use document `elowyn:conversation:<uuid>`, bank scoped to the single-user memory generation, tags for conversation/topic/role, and metadata containing conversation/message IDs, role, timestamp, and extraction schema version. Map backend fact types into Elowyn taxonomy after retrieval; do not equate Hindsight `world` with authoritative World State. Every returned item must carry or resolve to original message IDs.

### Slice 6 — observations and pages

Treat Hindsight observations as evidence-grounded backend candidates. Materialize small Elowyn-owned page DTOs/derived records by topic; store evidence links and refresh version. Do not copy canonical Task/Project/Goal/Decision fields as memory authority. Mental models can feed refresh but are replaceable implementation detail.

### Slice 7 — Context Composer

Compose identity + authoritative World State + recent conversation + small relevant page/memory section. Enforce an explicit memory token budget before prompt construction, rank/drop items deterministically, label memory as non-authoritative, and test irrelevant-memory resistance.

### Slice 8 — deep retrieval

Expose recall for specific facts/episodes and reflect for broad synthesis only on demand. Exact wording always follows provenance back to Core `Message`; reflect output is labeled synthesized and cannot execute domain writes itself.

### Slice 9 — rebuild and failure hardening

Replay ordered Core messages into a new backend bank/version, verify counts/provenance, atomically switch the adapter's active generation, and retire the old derived bank later. Add outage, stuck-operation, failed-consolidation, cursor idempotency, and catch-up tests.

### Slice 10 — behavioral E2E

Run the persistent synthetic multi-session acceptance set: recall, preference consolidation, idea/fact distinction, changed fact, contradiction, provenance/exact source, noise/budget, outage/catch-up, rebuild, World State safety, and Approved Plan safety; finish with real Telegram/provider smoke.

## Invariant check

The design keeps five state layers separate; keeps Conversation/Message verbatim and Core-owned; gives Hindsight no Core SQL credentials or domain write path; routes canonical changes through existing tools/Core/Event/Source; requires user approval for Plan changes; makes ingestion eventual and rebuildable; budgets prompt memory; preserves provenance; pins and health-checks the replaceable backend; and adds none of the deferred frameworks/capabilities. No invariant conflict was found.

## Operational risks

- Consolidation is asynchronous and LLM-dependent: readiness alone does not prove retained data was indexed or observations refreshed. Monitor operations/backlog and verify retrieval after write.
- Upstream self-hosted releases have reported silent retain/index and stalled/racing consolidation failures. Keep a pinned version, separate DB/role, durable Elowyn cursor, operation checks, and rebuild path.
- Hindsight metadata is string-valued and backend taxonomy is not Elowyn taxonomy; normalize in the adapter.
- The full Python server bundle is large and dependency-heavy. Prefer an isolated pinned server/container and thin client/API boundary.
