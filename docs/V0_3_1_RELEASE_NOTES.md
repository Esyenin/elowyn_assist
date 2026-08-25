# Elowyn v0.3.1 — v0.3 acceptance fixes

`v0.3.1` is a patch release over `v0.3.0`. It records fixes found during live user acceptance and
adds no new Planning capability or domain semantics.

## Acceptance fixes

- Telegram delivers long plain-text responses in bounded, lossless chunks.
- Candidate revisions remap model-local item handles to new server-generated UUIDs, preserving
  same-version dependency topology without reusing historical primary keys.
- Plan-change explanations separate the canonical `user_trigger` from optional
  `assistant_rationale`.
- Returning to a historical Approved plan reactivates that exact immutable `PlanVersion`; it does
  not create a copy.
- Plain basis changes do not trigger replanning without explicit replanning intent.
- Deadline/basis updates persist through canonical Goal/Event provenance and make the affected
  Approved version canonically stale without replacing it.
- Explicit rejection targets the canonical current Candidate and records its rejection while the
  Approved version remains unchanged.
- Planning answers are grounded in canonical current/history state, including rejected historical
  variants.
- Compact and full plan rendering are clearer for ordinary Telegram use.
- Collaborative next-action selection respects progress on the current Approved version and skips
  completed or skipped items.
- Transient model-provider HTTP 500/502/503/504 and provider timeouts degrade to a safe Telegram
  response; failed turns leave no partial canonical domain writes.

## Operational tooling

- The isolated personal launcher under `build/` starts the configured Elowyn runtime and pinned
  Hindsight service from one Windows entry point while keeping credentials and persistent data
  outside the repository.

## Known limitation

- Minor, non-blocking: a negative historical semantic lookup may describe the absence of a match
  too broadly (for example, a query about a rejected seven-day variant). This patch deliberately
  does not change that lookup behavior.

## Validation

- full non-Hindsight pytest suite: **283 passed, 0 skipped** (`3` Hindsight tests explicitly
  deselected because Docker was unavailable);
- targeted Planning, Telegram, runtime, provider-resilience, launcher and concurrency suite:
  **132 passed**;
- DB safety, concurrency, permissions, constraints and verifier suite: **36 passed**;
- Alembic `upgrade → downgrade → upgrade`: **PASS**;
- Ruff, MyPy, compileall, secret audit and `git diff --check`: **PASS**;
- Hindsight integration: **NOT RUN — Docker unavailable**. Memory code was not changed in this
  patch; the pinned backend previously passed real personal-environment acceptance.
