# Claude Code entry point

Read **`AGENTS.md` first and obey it**, then **`docs/STATUS.md`** (current progress, owner decisions, how to build/run, open issues). Then `docs/PRODUCT_SPEC.md`, `docs/ARCHITECTURE.md`, `packages/protocol/README.md` (locked wire contract) and the ADRs in `docs/adr/`.

**Working mode (owner, 2026-11-21):** one agent does all the work itself — **no subagents / delegated workers**. The owner tests on real hardware and reports results; do **not** drive the phone UI via adb taps or verify via screenshots. Build, run unit tests, install the debug APK, read logs when needed, then hand over to the owner with short test steps.

**Scope:** this is a **demo**. Prefer best-effort, working features over configurability and measurement rigor. Current priority: wake word + command transcription working simultaneously with the WebRTC stream.
