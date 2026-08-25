# Elowyn architecture index

This is a navigation aid for `personal_ai_assistant_master_v0.11.docx`, not a second source of
architecture. Open only the sections relevant to the task. Master section 2.2 defines the authority
order: working code and published releases describe implemented reality; accepted decisions and
the canonical architecture guide extensions; roadmap, candidates and historical notes are lower
authority.

## Fast routing

| Need | Master sections | Repository evidence |
| --- | --- | --- |
| Product purpose, initiative and autonomy | 1.1–1.12 | `README.md` |
| How to interpret statuses and conflicts | 2.1–2.5 | `AGENTS.md`, version contracts |
| External projects and theoretical rationale | 3.1–3.12 | Section 6.1 and source links in 6.6 |
| System boundaries and sources of truth | 4.1–4.6, 4.23 | `src/elowyn/db/`, `src/elowyn/services/` |
| Long-term Memory | 3.8, 4.7, 5.3–5.4 | `specs/MEMORY_V0_2.md`, `specs/V0_2_ACCEPTANCE.md` |
| Strategy and Planning | 3.3, 4.9, 5.5 | `specs/PLANNING_V0_3.md`, migration `0007` |
| Planning progress and next action | 4.9, 4.11, 5.5 | `src/elowyn/services/planning.py` and Planning tests |
| Future execution / workers | 4.10–4.13 | Roadmap 5.6; not part of v0.3.1 |
| Permissions, safety and reversibility | 1.6, 1.10, 4.14, 4.23 | domain services, DB roles and safety tests |
| Evaluation, experience and skills | 3.5–3.6, 4.15–4.16 | Roadmap 5.6; future scope |
| Proactivity, reflection and self-improvement | 3.2, 3.7, 4.17–4.20 | Roadmap 5.6; future scope |
| Implemented releases and next stage | 5.1–5.6 | `../IMPLEMENTATION_STATUS.md`, release notes |
| Accepted decisions and open questions | 6.2–6.3 | current specs and tracked issues |
| Terminology | 6.5 | ORM/domain names in current code |
| Master history | 6.7 | Git history for software changes |

## Chapter map

### 1. Introduction

Sections 1.1–1.12 define Elowyn's purpose, continuous availability, mixed initiative, working
agenda, permissions, privacy, learning and development constraints. Read these for product intent,
not implementation details.

### 2. How to read the master

Sections 2.1–2.5 define the document's role, authority hierarchy, implemented/architecture/roadmap
time layers, decision labels and terminology. Read this chapter before resolving an apparent
conflict between roadmap prose and the current repository.

### 3. Ideas, theory and existing solutions

Sections 3.1–3.12 explain why particular patterns or external projects are considered. Labels such
as `[РЕАЛИЗОВАНО]`, `[БЕРЁМ: КОД]`, `[КАНДИДАТ]` and `[ОТЛОЖЕНО]` matter: mentioning a project does
not make it a dependency or approved implementation choice.

### 4. Canonical architecture

- 4.1–4.6: system model; truth boundaries; persistent Core; World State; Event/Source provenance;
  Conversation/Message and external sources.
- 4.7–4.8: Long-term Memory, perception and future user modeling.
- 4.9: Goal → Strategy → Candidate Plan → Approved Plan, immutable versions, presentation,
  progress, basis and staleness.
- 4.10–4.13: internal work agenda, prioritization, future Run/Worker lifecycle and capability
  routing.
- 4.14–4.20: policy, permissions, evaluation, experience, proactivity, reflection,
  self-improvement and strategic optimization.
- 4.21–4.22: evolutionary delivery and the complete target lifecycle.
- 4.23: cross-version architectural invariants. Re-read this section before any new domain layer
  or infrastructure dependency.

### 5. Implementation and roadmap

- 5.1: snapshot recorded by master v0.11.
- 5.2: v0.1 Core/World State baseline.
- 5.3–5.4: v0.2/v0.2.1 Memory and hardening.
- 5.5: v0.3/v0.3.1 Planning, live acceptance and known minor limitation.
- 5.6: roadmap beginning with v0.3.2 observability/logging; future version numbers are planning
  guidance, not implemented capabilities.
- 5.7: capability maturity map.

### 6. Reference material

Sections 6.1–6.7 contain external-project status, accepted decisions, open/deferred questions,
audit rules, glossary, sources and the master changelog. Section 6.3 distinguishes the one known
minor v0.3.1 lookup wording issue from future work.

## Current implementation boundaries

- **v0.1:** PostgreSQL-authoritative World State with typed domain writes and Operation/Event/Source
  provenance.
- **v0.2.1:** rebuildable, non-authoritative Long-term Memory behind Elowyn-owned boundaries;
  Hindsight is pinned and replaceable.
- **v0.3.1:** immutable Strategy/PlanVersion history, presentation-bound explicit approval,
  progress/next action, canonical basis/staleness and acceptance hardening.
- **Not implemented by v0.3.1:** dynamic replanning, proactive work, Run/Worker execution,
  formal scheduling, behavioral modeling, strategic optimization and other later roadmap layers.

When this index disagrees with the master, specs or code, do not silently reconcile it: apply the
authority order above and update the index as documentation maintenance.
