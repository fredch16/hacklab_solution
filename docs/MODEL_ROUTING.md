# Model routing and parallel implementation policy

**Binding for Claude Code and advisory for other agent runners.** The user's preference is to **use Sonnet for most coding**, not merely tiny tasks. Favor steady, tested implementation over unnecessary expensive-model reasoning. The user's agreed vertical-slice process and question-asking rules in `AGENTS.md` take precedence over speed.

## 1. Model selection

| Work | Default owner | When to escalate |
|---|---|---|
| Flutter / Dart feature implementation | **Sonnet** | Only if repeated evidence-based failures expose a genuinely difficult architecture/platform issue |
| React / TypeScript dashboard, design system, accessibility | **Sonnet** | Unusually complex cross-device behavior or security issue |
| Tests, mocks in isolated test code, fixture creation, build scripts, refactoring, documentation | **Sonnet** | Rarely; first improve the specification or failing test |
| Android Kotlin / iOS Swift implementation against an agreed bridge | **Sonnet** | Unresolved concurrent media capture, native API incompatibilities, hard real-time or audio lifecycle bugs |
| WebRTC signaling, stats, signaling/state machine implementation against an agreed contract | **Sonnet** | Intractable transport failure, codec fan-out or performance tradeoff supported by diagnostics |
| High-impact architecture, novel security protocol, unresolvable native camera/audio contention, severe concurrency bug | **Opus (as needed)** | Solve the precise issue, document decision, return coding to Sonnet |
| Routine code review of a completed task | **A different Sonnet worker** | Opus only for particularly complex privacy/security or cross-platform media correctness concerns |

**Use Sonnet as the main Claude Code model and for the majority of implementer subagents.** Do not reserve Sonnet just for trivial changes. A clear spec, narrow scope, tests and interface contracts allow Sonnet to implement substantial vertical slices.

- Included project `.claude/settings.json` sets `"model": "sonnet"` as a starting preference. A user's CLI, account or managed settings may override it; check `/status` or `/model` rather than assuming it applied.
- To explicitly start on Sonnet: `claude --model sonnet` (when supported by the installed Claude Code version/account).
- Use `.claude/agents/sonnet-implementer.md` and `sonnet-test-reviewer.md` for explicit Sonnet subagents. Claude Code accepts `model: sonnet` in an agent's YAML frontmatter.
- If working in another coding agent platform, translate **the policy**, not Claude-specific filenames or tooling.
- Never promise that a specific model is available; report when unavailable and keep the workflow moving with an available suitable model.

## 2. Delegate substantial coding to Sonnet proactively

The lead should **prefer delegating self-contained implementations** to Sonnet workers whenever tool support and cost constraints permit. Examples: build the Flutter camera-status widget and tests, implement an agreed WebSocket protocol schema, build a recording-library card, write native adapter tests, implement the completed UI design system, or implement a bounded service behind a stable interface.

Good delegation specifies:

1. Exact slice and accepted behavior; paths the worker **owns** (other paths read-only).
2. Input/output contracts and dependencies already in the repository.
3. Required tests and a reproducible verification command.
4. What not to change (other slice, security defaults, recording profile, camera/mic ownership, cloud prohibition).
5. A result format: code paths, tests run, evidence, failure/risks, open product questions.

If a Sonnet implementer encounters ambiguity that changes UX or functional behavior, **stop and ask the user** via the lead, with a recommendation. If a technical issue can be resolved within the agreed contract, debug and continue without interrupting.

## 3. Parallelism rules

**Parallelize only independent work, preferably in separate worktrees/branches or otherwise non-overlapping file ownership.** Examples within one approved slice:

- Sonnet A: Flutter view/widgets and widget tests for a documented interface; Sonnet B: dashboard components for the *same* approved interface; Sonnet C: protocol contract tests or read-only docs review.
- Sonnet A: native media capture adapter for a well-defined bridge; Sonnet B: isolated browser WebRTC receiver and browser tests; lead owns integration and end-to-end validation.
- Sonnet A: local KWS model benchmarking in a disposable experiment; Sonnet B: voice status UI/components; **do not** run competing edits to the shared microphone pipeline.

**Do not parallelize**: two workers editing the same file; two workers independently owning camera/mic capture; an untested next slice while this slice is incomplete; competing cross-platform architecture decisions; simultaneous git merges or conflicting schema changes.

Lead must agree API contracts first, identify file ownership, supervise integration, resolve conflicts, run regressions and **personally verify the whole slice** before marking it complete. Parallel subtask completion is not slice completion.

## 4. Escalation procedure

1. Sonnet creates a minimal failing test/reproducer, gathers platform version, command, logs, metrics and attempted fixes.
2. Sonnet tries reasonable bounded fixes using authoritative docs. If truly stuck, hand the **specific** technical question and evidence to Opus; do not restart the entire project with a second architect.
3. Opus recommends the smallest decision/diagnostic experiment and identifies any user-visible tradeoff.
4. The lead asks the user if behavior or scope would change; otherwise records a brief ADR.
5. **Return implementation, tests and routine review to Sonnet**. Escalating for diagnosis is not permission to shift all coding to Opus.

## 5. Quality gates are model-independent

Every contributor must obey `AGENTS.md`: one vertical slice at a time, tests before code when feasible, real hardware gates, no fabricated measurements, no unapproved scope, local-only operation and secure pairing. Do not trade away testing, code review or user questions to minimize token cost.

## 6. What to report at each slice gate

In addition to the `AGENTS.md` report, include:

- **Model/worker split:** Sonnet-owned implementations and reviews; any Opus escalation and why.
- **Parallel branches/worktrees:** work completed and integrated, no remaining conflicting edits.
- **Verification owner:** the lead's end-to-end result and any hardware tests that the user must run.
