# Agent Instructions - AI Camera Director Demo Beta

This demo addendum overrides older project handoffs wherever they prescribe phone-first capture, subagents or a single-preset best-view objective. Do not overwrite the existing repository AGENTS.md/CLAUDE.md; read them and preserve non-conflicting project rules.

## Working method - ONE agent only

- **NO subagents**: do not invoke Claude Code Task/subagent tool, Agent Teams, external coding agents, parallel agents, delegated code reviewers or autonomous parallel worktrees. Work in one Claude Code session sequentially.
- Use **Sonnet as the active implementation model for most coding** (frontend, CV, backend, tests, bug fixes). For an unusually complex safety/kinematics question, reason carefully in the lead context or ask the user; do not spawn an Opus subagent. If switching the *main* session model is appropriate and supported, that does not authorize delegation.
- Work sequentially by **vertical slice**, not by library/layer. Each slice delivers actual UI + local service + integration + tests + observable result, with one small commit after verification.
- Do not pause on routine software choices; choose the simplest approach compatible with the current repo. Ask the user for genuine behavioral ambiguities or physical safety facts not established by inspection. Never invent safe joint positions or collision geometry.
- Inspect the actual codebase before assuming language, routes, Camera/Station APIs, joint units, file paths or preset names. Do not replace the working camera/dashboard/MCP architecture with a new stack.

## Objective and priority

- Robot cameras first, with **no Pixel, no voice, no internet and no Claude API required** to run the demo.
- Target acquisition: automatic Hack Lab sticker at `topview` from a genuine reference image or credible local feature match; manual drawn ROI for any object using the same tracker and director.
- **Optimize the selected target's view near every existing approved camera preset**, not only `topview`. Save best verified **measured** pose and a thumbnail per preset; if the object is missing or pose is unsafe, mark unavailable and skip.
- Cinematic mode should follow **only known-safe approved transitions** between those saved valid shots and monitor target and robot-state freshness. Smooth transitions if existing motion supports it; never interpolate an unverified collision-risky joint-space shortcut.
- Bonus demo features only after core sequence: one-click Director Tour, before/after gallery, confidence/score overlays, record status, recall best shot, optional Claude high-level MCP commands. Phone integration remains last.

## Strict layer boundaries

- Camera capture/WebRTC -> local target detector/tracker -> local quality scorer -> local director state machine -> existing robot motion bridge / NormaCore Station.
- Claude is an **optional orchestrator**: start job/status/cancel/recall; never per-frame vision, best-angle rating, raw servo command generation or a required part of local loop.
- Browser draws ROIs/overlays and initiates jobs, but does not become the authoritative motor controller.
- `topview` sticker image localization, calibrated table-to-pixel pick grids and arm eye-in-hand visual servoing are different coordinate transforms. Audit existing precision mapping and validate what it actually does.

## Mandatory movement safety

- Establish real camera source identity (moving wrist camera vs fixed external camera).
- Verify active arm configuration, table plane/height, URDF or current kinematics, camera/end-effector/gripper offsets, cable geometry and **swept** workspace clearance along each route, measured servo feedback, speed/step limits, overload/fault handling, emergency/manual stop, plus existing lift-over path behavior.
- Start dry-run. Physical exploration only within operator-approved safe preset neighborhoods and route segments, with operator present and verified stop functionality.
- If collision clearance or safe path cannot be verified, **do not move**. Produce the completed non-motion tracking and virtual scoring slice, report blocker and ask the user for the exact missing hardware fact.
- STOP/manual override preempts every autonomous job; conflicting commands, lost targets, stale video/servo data or disconnect -> HOLD with clear UI status.

## Definition of verified

A slice is complete only after code checks + tests + integrated demonstration on real available hardware, with logs/screenshots/measurements. Replay-only validation does not establish actual mechanical safety. Never claim a score improved unless the same target has been verified at the compared settled views.

## Source of truth

1. `docs/AI_CAMERA_DIRECTOR_DEMO_SPEC.md`
2. `docs/superpowers/plans/2026-11-22-ai-camera-director-demo.md`
3. `docs/DEMO_RUNBOOK.md`
4. Existing repo code and verified installed Station SDK
5. Earlier reference research only where it does not contradict above.
