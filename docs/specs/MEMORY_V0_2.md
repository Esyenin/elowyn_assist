# Elowyn v0.2 — Long-term Memory implementation contract

Status: **IMPLEMENTED / ACCEPTANCE-HARDENED FOR v0.2.1**

Source: master v0.11 and the released v0.2/v0.2.1 implementation, migrations and acceptance
evidence. For implemented behavior, working code and published releases take priority as defined
by master section 2.2; unresolved product or architecture conflicts defer to the current master.

## 1. User capability

After v0.2, a new conversation should feel like the same Elowyn:

- she remembers significant prior interactions;
- can recover useful past context without manual reminders;
- accumulates facts/preferences/ideas/episodes/context and higher-level observations;
- uses only a small relevant subset in normal prompts;
- can go deeper when necessary;
- does not confuse memory with canonical truth.

Key rule:

> STORE ALMOST EVERYTHING SIGNIFICANT. LOAD VERY LITTLE INTO ACTIVE LLM CONTEXT.

## 2. State layers — never collapse them

1. **World State** — current structured Task/Project/Goal/Decision/etc.
2. **Event History** — what changed and why.
3. **Raw Experience Archive** — original Conversation/Message; later other raw sources.
4. **Long-term Memory** — derived useful facts/preferences/ideas/episodes/observations/context.
5. **Plan** — agreed way of acting.

Memory is its own layer.

## 3. Authority

Memory has no direct write authority over World State.

If memory suggests a canonical update:

Memory
→ reasoning/suggestion
→ existing Domain Action
→ Core validation
→ World State + Event/Source.

Memory also cannot silently modify an Approved Plan.

Any working Approved Plan and material revision requires explicit user approval.

Do not implement authority as one simplistic integer score unless the implementation genuinely benefits from it. Semantically, current authoritative World State/current explicit user statement outrank old extracted memory and derived observations.

## 4. v0.2 memory pipeline

Raw Experience Archive
→ Atomic Memories
→ Consolidated Observations
→ Memory Pages / shortcuts
→ Context Composer
→ Elowyn turn

The chain must be traceable backward:

Memory Page
→ Observation
→ atomic memory/evidence
→ original Conversation/Message.

## 5. Raw experience archive

Core-owned `Conversation` / `Message` remains the canonical verbatim conversation archive.

Requirements:
- keep original messages completely;
- do not replace transcript with summaries;
- do not give a third-party memory backend ownership of raw history;
- semantic memory must be rebuildable from this archive.

Hindsight removal must not destroy the ability to reconstruct memory.

## 6. ConversationSummary

Add a derived per-conversation shortcut (exact schema is an implementation choice).

Expected information:
- conversation id;
- short summary;
- topics;
- relevant entities/projects/goals;
- temporal range where useful;
- last processed message;
- updated time.

It is:
- derived;
- disposable/recomputable;
- not source of truth;
- useful for navigation/relevance/context cost reduction.

## 7. Atomic memory taxonomy

Initial Elowyn semantic categories:

- `FACT`
- `PREFERENCE`
- `CONTEXT`
- `IDEA`
- `EPISODE`
- `CONSTRAINT`
- `OBSERVATION` for consolidated inference (or equivalent representation)

Future autonomous-worker work may add `EXPERIENCE`/outcome semantics. Do not prematurely model every conversation as an Experience lifecycle.

Critical semantic distinction:
- mentioned
- considered
- preferred
- decided
- currently true

Example:
“Maybe use Neo4j” may become IDEA/consideration.
It must not become FACT “Elowyn uses Neo4j” and must not replace the Decision domain entity.

## 8. Observations / consolidation

Repeated evidence may produce a stronger evidence-grounded observation.

An observation needs:
- supporting evidence/provenance;
- confidence;
- refinement/update behavior;
- contradiction handling.

A single weak mention must not become a durable behavioral pattern.

## 9. Memory Pages

Memory Pages are compact high-level Elowyn-owned shortcuts.

Initial page types may include:
- User Profile;
- Communication Preferences;
- Work/Career Context;
- Personal Constraints;
- Project: <name>;
- Topic: <name>.

Avoid one giant “everything about user” page.

Pages:
- remain small;
- can be independently refreshed;
- are derived/rebuildable;
- carry evidence/provenance;
- do not duplicate World State.

Example:
World State “Project Elowyn” stores current goals/tasks/decisions/status.
Project Memory Page stores historical rationale, rejected alternatives, recurring preferences, old context and deferred ideas.

## 10. Context Composer

Normal turn fast path:

Identity
+ relevant World State
+ recent messages in current Conversation
+ small amount of relevant Memory Pages/compact memory.

Requirements:
- explicit strict memory token budget;
- do not raw-vector-recall every turn;
- avoid prompt pollution/noise.

Deep path:
- `recall` for specific old facts/episodes;
- `reflect`/synthesis for broad historical reasoning;
- original Conversation/Message for exact wording/evidence.

Retrieval depth should increase only as needed:

shortcut
→ synthesized memory
→ atomic memory
→ raw source.

## 11. MemoryService boundary

Core/Context Composer must depend on an Elowyn-owned memory interface, not directly on Hindsight APIs.

Conceptually:

Elowyn / Context Composer
→ MemoryService
→ HindsightAdapter
→ Hindsight

Exact methods/DTOs are implementation choices after inspecting current code.

Expected capabilities likely include:
- retain/ingest;
- recall;
- reflect;
- relevant pages;
- rebuild;
- health.

Names are not mandated by this spec.

## 12. Hindsight

Hindsight is the first selected practical backend for v0.2.

It is a dependency, not architecture owner.

Before relying on it:
1. check current official docs and stable releases;
2. pin a concrete version;
3. verify actual API/semantics used;
4. integration-test retain/recall/consolidation/observations/mental-model behavior needed by Elowyn;
5. inspect append/upsert/document-id behavior;
6. add health/failure coverage.

Prefer:
Conversation/Messages with roles + timestamps + useful context/tags
→ Hindsight retain.

Do not:
Conversation
→ our lossy summary
→ retain(summary) as the primary ingestion path.

Use stable document identity linked to Elowyn conversation/source.

Known design risk:
self-hosted Hindsight has had real consolidation/refresh failure modes. Protect Elowyn with version pinning, adapter isolation, health checks, durable catch-up and full rebuildability.

If Hindsight conflicts with Elowyn boundaries, change adapter/version/backend — not Core authority rules.

## 13. Backend isolation

Preferred boundary:
- memory backend isolated from Core World State ownership;
- separate DB/schema ownership boundary and role where practical;
- no backend write permissions to Core World State/history.

Exact deployment layout is an implementation detail.

## 14. End-of-turn ingestion

Expected lifecycle:

user Message saved
→ Context Composer builds current turn context
→ Elowyn response/tools
→ assistant Message saved
→ memory ingestion recorded/scheduled
→ backend processing
→ summaries/memories/observations/pages eventually refreshed.

Memory backend availability must not determine whether the core user turn succeeds.

If memory backend is down:
- original Message remains saved;
- Core conversation continues;
- ingestion remains pending;
- retry/catch-up later.

## 15. Durable ingestion state

Need durable state/cursor capable of answering:
- what conversation/source is being processed;
- through which message ingestion succeeded;
- backend;
- status;
- attempts;
- last error;
- updated time.

Exact SQL/ORM shape is intentionally left to implementation.

Do not add Temporal/Celery/Kafka merely for this if a simple durable outbox/worker/catch-up loop is sufficient.

## 16. Rebuildability

The entire semantic-memory backend must be disposable/rebuildable.

Rebuild input:
- Core Conversation/Message archive;
- Elowyn provenance/metadata.

Rebuild output:
- semantic/atomic memories;
- observations;
- summaries/pages as applicable.

A vendor/backend change must not require rewriting Core domain architecture.

## 17. Behavioral acceptance

v0.2 must demonstrate at least:

1. **Cross-session recall** — remembered after restart/new conversation.
2. **Preference consolidation** — repeated preference becomes useful observation/page.
3. **Idea vs fact** — consideration is not remembered as adopted truth.
4. **Changed fact** — current info wins while history remains recoverable.
5. **Provenance** — memory result can reach original Conversation/Message.
6. **Irrelevant-memory resistance** — unrelated history does not pollute normal answer.
7. **Context budget** — memory does not expand every prompt without bound.
8. **Backend outage** — conversation works; ingestion catches up later.
9. **Rebuild** — derived memory can be deleted/rebuilt and tests pass again.
10. **World State safety** — memory cannot directly mutate Task/Project/Goal/Decision.
11. **Plan safety** — memory cannot silently alter Approved Plan.
12. **Contradiction** — conflicting memories do not collapse into confident falsehood.
13. **Exact-source lookup** — exact wording comes from raw source, not fabricated summary.

Build a small persistent synthetic regression dataset for these behaviors.

## 18. Definition of Done

v0.2 is not done because “Hindsight is connected”.

Done means Elowyn demonstrably has:
- long-term recall between conversations;
- automatic ingestion;
- correct semantic distinctions;
- consolidation;
- compact pages/shortcuts;
- relevant bounded context;
- deep recall/reflect on demand;
- temporal/update handling;
- provenance to original messages;
- rebuildability;
- failure recovery/catch-up;
- no direct Memory → World State writes;
- no hidden Memory → Approved Plan changes;
- regression + real multi-session E2E confirmation.

## 19. Explicit non-goals

Do not implement in v0.2 unless a direct Memory blocker proves otherwise:
- Strategy/Planner;
- dynamic replanning;
- Strategic Optimizer;
- OR-Tools scheduling;
- autonomous coding/research workers;
- ActivityWatch/OpenAdapt;
- full Behavioral Model;
- desktop perception;
- complex Resource model;
- distributed durable workflow engine;
- Graphiti/graph DB “for the future”;
- LangGraph merely for ingestion.

## 20. Coding-window freedom

Without new product approval, Codex may choose:
- exact MemoryService API/DTOs;
- exact ConversationSummary schema;
- exact ingestion cursor/outbox schema;
- pinned Hindsight version/deployment after real smoke;
- metadata/tag conventions;
- retrieval token budgets/thresholds;
- first page set;
- retry/backoff/health mechanics;
- separate memory DB/schema layout.

Escalate only when a choice changes product behavior or violates an invariant.

## 21. Recommended implementation order

### Slice 1 — extension points + Hindsight feasibility
- inspect relevant v0.1 code only;
- identify integration points;
- verify current Hindsight docs/version/API;
- pin candidate version;
- run minimal retain/recall/consolidation smoke;
- write short implementation design against this spec.

### Slice 2 — Core-owned derived metadata
- ConversationSummary;
- durable ingestion state/outbox;
- migrations/tests.

### Slice 3 — Memory boundary/backend adapter
- MemoryService;
- HindsightAdapter;
- health;
- integration tests.

### Slice 4 — asynchronous/end-of-turn ingestion
- schedule/record ingestion;
- catch-up/retry;
- backend outage does not fail main turn.

### Slice 5 — memory semantics/provenance
- taxonomy mapping;
- stable source/document identity;
- source links.

### Slice 6 — observations/pages
- consolidation behavior;
- initial page set;
- evidence/provenance.

### Slice 7 — Context Composer
- fast-path relevant memory;
- strict budget;
- noise resistance.

### Slice 8 — deep retrieval
- recall;
- reflect/synthesis;
- exact-source path.

### Slice 9 — rebuild/failure hardening
- full replay;
- health/backlog;
- recovery tests.

### Slice 10 — behavioral E2E
- regression dataset;
- multi-session Telegram smoke;
- final v0.2 acceptance.

Do not merge all slices into one enormous Codex session.
