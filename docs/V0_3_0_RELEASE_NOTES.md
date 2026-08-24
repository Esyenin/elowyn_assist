# Elowyn v0.3.0 — Strategy and Planning

Elowyn теперь может предложить стратегию и план, обсуждать версии естественным языком,
сохранять историю, утверждать только конкретно показанный вариант после явного согласия
пользователя, вести прогресс и определять следующий шаг.

## Delivered

- stable Strategy and Plan identities with immutable, monotonically numbered PlanVersions;
- Candidate, Approved, Rejected and Superseded lifecycle with current Candidate/Approved isolation;
- exact `PlanVersionPresentation → assistant Message → approval USER_MESSAGE` binding;
- atomic approval and Strategy acceptance with Source/Operation/Event provenance;
- native PlanVersionItems, separate PlanItemProgress, dependencies and deterministic Next Action;
- bounded current Planning Context plus explicit bounded history, version, diff and provenance reads;
- PlanVersionBasis and read-only staleness detection as the safe foundation for v0.4.

Approval does not create or mutate Task, Project, Goal or Decision. Memory remains
non-authoritative and cannot approve or modify a Plan.

## Release validation

- full non-Hindsight suite: **218 passed**, with **3 real-Hindsight tests deselected**;
- deterministic v0.3 E2E, concurrency, rollback and restart persistence: **PASS**;
- Alembic upgrade/downgrade/upgrade, PostgreSQL constraints, permissions, consistency verifier and
  recovery drill: **PASS**;
- Ruff, MyPy, compileall, secret audit and `git diff --check`: **PASS**;
- real configured model planning acceptance: **PASS**;
- real Telegram Bot API multi-turn acceptance: **PASS**;
- real Hindsight 0.9.1 locally: **NOT RUN — Docker runtime unavailable**.

## v0.4 boundary

v0.3 does not implement automatic replanning, impact analysis, proactive invalidation, Schedule,
resource optimization, or Run/Worker lifecycles. The next stage is **v0.4 — dynamic replanning**:
World State changes may lead Elowyn to propose a new Candidate, but never to silently replace the
current Approved Plan.
