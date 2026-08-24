# Elowyn v0.3 — Strategy and Planning implementation contract

Status: **IMPLEMENTED / RELEASE-READY FOR v0.3.0**

Source: the current project master, the working v0.2.1 implementation and migrations, the
pre-v0.3 technical audit, and the accepted v0.3 product decisions. If older planning notes
conflict with this contract, this contract records the current accepted v0.3 decision.

The implemented v0.3 scope includes Strategy, Plan and immutable PlanVersion history;
Candidate/Approved/Rejected/Superseded transitions; canonical Presentation binding and explicit
natural approval; PlanItemProgress and deterministic Next Action; bounded current Planning context;
history/explainability; and the read-only PlanVersionBasis/staleness foundation. Dynamic impact
analysis and replanning remain explicitly deferred to v0.4.

## 1. User capability

v0.3 moves Elowyn from:

> knowing the current state and remembering the past

to:

> proposing an explainable strategy and plan for reaching the user's goals, discussing them
> naturally, and recording an approved version only after the user's explicit agreement.

The user interacts with Elowyn in natural language. Internal PlanVersion IDs, statuses, database
objects, and CRUD mechanics are not part of the user interface.

The core v0.3 boundary is:

```text
Goal + World State + relevant Memory
          ↓
Elowyn proposes Strategy + Plan
          ↓
Candidate PlanVersion is persisted and presented to the user
          ↓
the user discusses and revises it naturally
          ↓
a material revision creates a new Candidate
          ↓
the user gives an unambiguous natural-language approval
          ↓
that presented Candidate becomes Approved
          ↓
Strategy is accepted atomically with it
          ↓
prior versions remain in history
          ↓
Elowyn can show the current Plan and compute the next action
```

## 2. Concepts and boundaries

- **Goal** — a desired state or result.
- **Strategy** — the stable, currently accepted general approach to reaching one or more Goals.
- **Plan** — the stable logical lineage of a user's plan.
- **PlanVersion** — one concrete, immutable content version of a Plan.
- **PlanVersionItem** — a native item of the user's Plan, not a Task in disguise.
- **PlanItemProgress** — the current execution state of an item, separate from immutable version
  content.
- **PlanVersionPresentation** — evidence that a specific version was actually presented in an
  assistant Message.
- **PlanVersionBasis** — the exact material Entity/Event basis from which a version was built.

The following distinctions are invariants:

```text
Goal != Strategy != Plan != Task
User Plan != Elowyn's internal working organization
Plan != Schedule
PlanVersion content != PlanItemProgress
```

Planning is a canonical domain layer, but it does not gain authority over other canonical domain
types. Memory may inform planning but remains derived and non-authoritative.

## 3. Plan and Task remain separate

A Plan consists of its own PlanVersionItems. Approving a Plan:

- does not create a Task, Project, or Goal;
- does not modify an existing Task, Project, Goal, or Decision merely because it is mentioned;
- does not treat a Plan item as a hidden Task.

A PlanVersionItem may optionally reference an already existing Task. Creating or changing a Task,
Project, Goal, or Decision still requires a separate natural user intention and the existing typed
domain operation.

## 4. Strategy semantics

Before the first approval, a proposed Strategy exists only as an immutable snapshot and rationale
inside a Candidate PlanVersion. Creating or presenting a Candidate must not create an empty or
canonical Strategy merely to reserve an identity.

On the first PlanVersion approval, a stable Strategy Entity is created from the approved version's
Strategy snapshot. On later approvals, that stable Strategy is atomically updated from the newly
approved version's immutable Strategy snapshot and rationale.

Strategy change history is preserved through existing Source/Operation/Event provenance and the
immutable historical PlanVersions. v0.3 does not introduce StrategyVersion or a Strategy Entity
supersession chain.

A Candidate never changes the accepted Strategy. A failed approval must leave both the current
Approved PlanVersion and the current Strategy unchanged.

## 5. PlanVersion state machine

The statuses are:

- `CANDIDATE` — the current version Elowyn proposes to the user;
- `APPROVED` — the current version explicitly accepted by the user;
- `SUPERSEDED` — a formerly current Candidate or Approved version replaced by a newer current
  version;
- `REJECTED` — a version explicitly rejected by the user.

Allowed transitions:

```text
CANDIDATE
 ├─→ APPROVED
 ├─→ REJECTED
 └─→ SUPERSEDED

APPROVED
 └─→ SUPERSEDED

SUPERSEDED — terminal
REJECTED   — terminal
```

Per Plan:

- there is at most one current `CANDIDATE`;
- there is at most one current `APPROVED`;
- a current Candidate and a current Approved may coexist;
- a new Candidate does not change the current Approved;
- successful presentation of a new Candidate supersedes the previous Candidate in the same Plan
  lineage;
- a material edit to a presented version creates a new monotonically increasing `version_number`;
- historical PlanVersion content is never overwritten;
- terminal versions never return to Candidate or Approved.

## 6. Version history and return to an old variant

Every version whose content was presented to the user is retained. The history must make it
possible to determine:

- the first version and the version lineage;
- what changed between versions;
- which user Message or assistant synthesis caused a new version;
- which versions were rejected, approved, or superseded;
- which version was previously current and which versions are current now;
- the Source/Operation/Event provenance of every state transition.

An historical version is never reactivated. If the user asks to return to an older variant, Elowyn
creates and presents a new Candidate based on that historical version. The original remains
unchanged.

## 7. Presentation binding

Creating Candidate content and presenting it are distinct domain facts.

Approval is allowed only for a Candidate having a PlanVersionPresentation linked to a persisted
assistant Message. Re-presenting an unchanged Candidate may add another Presentation without
creating another PlanVersion.

A newly formed Candidate becomes the current Candidate only when the Candidate, assistant Message,
Presentation, and superseding of the prior Candidate are successfully persisted together. If that
unit fails, the prior current Candidate remains current and the incomplete Candidate does not become
part of the visible lineage.

PlanVersionPresentation proves application-level presentation in the canonical conversation
archive. It does not invent a second Source or event store.

## 8. Natural-language confirmation

Approval requires no special command or button. Phrases such as “yes”, “ok”, “that works”, “let's
do it”, or “approve it” may express approval. The language model recognizes the natural intention;
the actual transition is performed only by a separate validated domain operation.

For a simple confirmation such as “Yes” in v0.3, the rule is deterministic:

> The immediately preceding assistant Message in the same Conversation must contain a
> PlanVersionPresentation for exactly one current Candidate.

If that Message presents multiple alternative Candidates, a simple confirmation is ambiguous and
Elowyn must clarify. Ambiguity is determined from conversation context, not from the word “yes”.

Silence, a topic change, performing a step, or failing to object is not approval.

## 9. Atomic approval

Approval of a PlanVersion is bound to both:

```text
Source(USER_MESSAGE) → canonical user Message
```

and a pre-existing Presentation of the exact Candidate being approved.

Approval is one atomic domain operation. It must:

1. change the previous Approved version, if any, to `SUPERSEDED`;
2. change the presented Candidate to `APPROVED`;
3. create the stable Strategy on first approval or update it from this version's Strategy snapshot;
4. preserve approval Source/Message/Operation/Event provenance;
5. initialize PlanItemProgress for the approved version.

Neither of these intermediate states may commit:

- new Approved version with the old Strategy;
- new Strategy with the old Approved version.

Approval never invokes automatic Task, Project, Goal, or Decision mutations. A retry for the same
approval evidence must be idempotent or fail safely without producing a second logical transition.

## 10. Progress

PlanVersionItem content is immutable. PlanItemProgress stores the current execution state.

The minimal states are:

- `NOT_STARTED`
- `IN_PROGRESS`
- `WAITING`
- `BLOCKED`
- `DONE`
- `SKIPPED`

“I finished the second item” changes Progress without creating a PlanVersion. “The second item is no
longer needed; replace it with another one” is a content revision and requires a new Candidate.

Progress changes retain normal Source/Operation/Event provenance and do not implicitly modify a
linked Task.

## 11. Dependencies and next action

PlanVersionItems may have directed dependencies. Self-dependencies and cycles are invalid, and a
dependency cannot connect items from different PlanVersions.

`next action` is not a canonical entity in v0.3. It is a computed PlanningService answer for the
current Approved Plan:

1. continue an `IN_PROGRESS` item when one is available;
2. otherwise consider `NOT_STARTED` items;
3. require all dependencies to be complete;
4. exclude `WAITING`, `BLOCKED`, `DONE`, and `SKIPPED` from ordinary selection.

Optimization, scheduling, and strategic replanning are outside v0.3.

## 12. Plan and Schedule

A Plan may describe:

- what to do and why;
- ordering and dependencies;
- conditions and deadlines;
- approximate durations.

A Schedule assigns work to a concrete date, time, or time slot among other commitments. Schedule is
not implemented in v0.3, and v0.3 must not create placeholder scheduling tables.

## 13. PlanVersionBasis and staleness

Each PlanVersion records the exact material canonical basis as:

```text
entity_id + event_id + role
```

The minimum roles are:

- `GOAL`
- `TASK`
- `PROJECT`
- `DECISION`
- `STRATEGY`

There is no global World State revision. Conversation, Memory, and other non-canonical evidence use
the existing Message, Source, and SourceDependency mechanisms rather than being turned into fake
Entities.

v0.3 only detects that canonical state relevant to a version changed after that version was built.
It does not automatically decide the impact, invalidate the Plan, or rebuild it. Impact assessment
and dynamic replanning belong to v0.4.

## 14. Provenance and ownership

Planning reuses the existing:

- `ActionContext`;
- `Source` and `SourceDependency`;
- `Operation` and `Event`;
- `Conversation` and `Message`.

It does not create another event store, provenance graph, or write path. A Candidate synthesized by
Elowyn is assistant inference/synthesis with evidence; it must not claim to be the user's exact
wording. Approval itself is authoritative only because it resolves to the explicit user Message and
the concrete previously presented Candidate.

Planning has no direct Memory-backend ownership, and Memory has no direct authority to approve or
mutate a Plan.

## 15. Plan-to-Goal relations

A Plan may support multiple Goals through strict typed relations. A relation may have a `PRIMARY`
or supporting role, but a Plan is not restricted to a single Goal. Adding or removing a Plan-to-Goal
relation does not mutate the Goal itself.

## 16. Explicit v0.3 exclusions

v0.3 does not include and must not pre-model tables for:

- v0.4 dynamic replanning or automatic impact analysis;
- Elowyn's internal working agenda;
- Run or Worker lifecycles;
- LangGraph, Temporal, OR-Tools, or another workflow/optimization framework;
- Schedule;
- Experience or Skills;
- Dreaming or a proactive loop;
- Activity tracking;
- Browser/Desktop perception;
- resource optimization;
- a new Memory backend;
- a global World State revision.

The accepted v0.3 product is complete when the end-to-end user scenario in section 1 works through
natural conversation, preserves every presented version and its provenance, performs atomic
PlanVersion/Strategy approval, and can show the current Approved Plan and compute its next action.
