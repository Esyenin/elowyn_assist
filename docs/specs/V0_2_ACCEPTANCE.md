# Elowyn v0.2 Memory — Slice 10 acceptance status

Status: release-candidate acceptance passed on 2026-08-23.

## Behavioral acceptance

All 13 scenarios in `MEMORY_V0_2.md` pass against the pinned official
`ghcr.io/vectorize-io/hindsight:0.9.1` image and PostgreSQL Core:

- cross-session recall and repeated-preference consolidation;
- IDEA remains distinct from FACT, and changed/contradictory information remains historical,
  non-authoritative evidence;
- provenance resolves through Elowyn metadata to the canonical Conversation/Message;
- irrelevant-memory suppression and the configured context budget;
- backend outage does not block the turn, and recovery catches up durable backlog;
- full rebuild from the raw archive, including derived observations/pages;
- no Memory write to World State or hidden Approved Plan mutation;
- exact-source answers use the canonical raw Message rather than a summary or synthesis.

The vertical acceptance uses actual `aiogram` Message updates, `TelegramAdapter`,
`ElowynRuntime`, PostgreSQL persistence, asynchronous ingestion, real Hindsight 0.9.1,
a new Telegram chat/session, runtime reconstruction, retrieval, and a natural synthetic
provider response. It does not require or expose a Telegram token and therefore does not
exercise external Bot API delivery/polling.

## Recovery result

The gate also discards the Hindsight test container and its pg0 volume, starts a clean
pinned backend at the same endpoint, rebuilds a new generation from canonical Core
Conversation/Message rows, switches generation only after success, and verifies recall and
provenance again. Core World State and Event history remain unchanged.

Direct stop/start of the 0.9.1 standalone image with embedded pg0 did not regain readiness
in GitHub Actions. The accepted recovery path is the stronger architecture-level guarantee:
Hindsight is disposable, and total backend loss is recovered by a clean generation rebuild.
Production deployments should use a separately operated persistent Hindsight database and
monitor its lifecycle independently.

## Release gate

GitHub Actions run `32606212011` passed both jobs:

- full non-Hindsight suite: 127 passed, zero skips/xfails/xpasses;
- mandatory real-Hindsight suite: 3 passed, zero skips;
- Ruff, mypy, compileall, secret audit, Alembic upgrade/downgrade/upgrade, least-privilege
  grants, DB acceptance/concurrency/consistency checks, and guarded recovery drill passed.

The only production defect found during Slice 10 was that successful normal ingestion did
not refresh Elowyn-owned summaries/observations/pages until a rebuild. Derived refresh now
runs after confirmed ingestion, outside the user-turn critical path; a targeted regression
test covers repeated preference page creation.
