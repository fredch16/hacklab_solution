# Claude Code entry point

Read **`AGENTS.md` first and obey it**; it applies equally to Claude and any delegated agents. Then read `MASTER_PROMPT.md`, `docs/PRODUCT_SPEC.md`, `docs/ARCHITECTURE.md`, `docs/SLICE_PLAN.md`, and `docs/TEST_PLAN.md`. For UI decisions consult `docs/DESIGN_SYSTEM.md` and `ui/reference-dashboard.html` (static design only). For build sequencing consult `docs/superpowers/plans/2026-11-21-phone-first-console-implementation.md`.

**Model routing:** **Sonnet is the default for most implementation work** (including meaningful feature coding, not only simple subtasks). `.claude/settings.json` selects `sonnet` where supported. Proactively use `.claude/agents/sonnet-implementer.md` for scoped coding and `sonnet-test-reviewer.md` for independent test/review; delegate independent work in parallel only with disjoint files and shared contracts. Reserve Opus for exceptional architecture, security, or hard native audio/video debugging; return implementation to Sonnet afterward. Read `docs/MODEL_ROUTING.md`.

**Current state:** documentation/design handoff only; nothing has been implemented or tested on the phone by the author of these files. Begin by inspecting the actual repository and toolchain; if the repo is empty, implement Slice 0 rather than scattering untested stubs across later features.

**Key instruction:** complete vertical slices end-to-end and ask the user when product behavior is unclear. Do not guess UX decisions or add cloud/PC inference.
