# Project documents

`personal_ai_assistant_master_v0.11.docx` is the current master project document. It records the
product goal, accepted architecture, implemented baseline and forward roadmap. For claims about
what is already implemented, working code, migrations, tests and published releases take priority,
as defined by master section 2.2.

Use `ARCHITECTURE_INDEX.md` to locate the relevant master sections without reading the entire DOCX.

## Current baseline

- Stable product baseline: **v0.3.1** (Core/World State v0.1, Long-term Memory v0.2.1 and
  Strategy/Planning v0.3.1).
- Next roadmap stage in master v0.11: **v0.3.2 — observability, diagnostic logging and regular
  real-world operation**.
- Current implementation status: `../IMPLEMENTATION_STATUS.md`.
- Release notes: `V0_2_1_RELEASE_NOTES.md`, `V0_3_0_RELEASE_NOTES.md` and
  `V0_3_1_RELEASE_NOTES.md`.

## Version contracts and evidence

- `specs/MEMORY_V0_2.md` — implemented Long-term Memory contract.
- `specs/V0_2_ACCEPTANCE.md` — v0.2 behavioral acceptance evidence.
- `specs/V0_2_SLICE_1_DESIGN.md` — historical Hindsight feasibility/design record.
- `specs/PLANNING_V0_3.md` — implemented and acceptance-hardened v0.3.1 Planning contract.
- `HINDSIGHT_INTEGRATION_TEST.md` — reproducible pinned Hindsight integration gate.
- `DATA_INVARIANT_AUDIT_V0_1.md` — historical v0.1 invariant audit.

The master remains in DOCX form. Do not rename it without updating this README,
`ARCHITECTURE_INDEX.md`, `AGENTS.md` and affected version contracts.
