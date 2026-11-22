# AI Camera Director Multi-Preset Demo Implementation Plan

> **For the single coding agent:** REQUIRED EXECUTION MODE: sequential implementation in the current main Claude Code session (equivalent to `superpowers:executing-plans`). **Do NOT use `subagent-driven-development`, Task, Agent Teams, parallel code agents, delegated code reviews or parallel workers.** After each slice: failing test -> implementation -> green tests -> local demonstration -> commit -> next slice.

**Goal:** Create a robot-camera-first, fully local director that selects a target, optimizes its view safely near **every existing approved preset**, saves the best measured viewpoint for each, and makes a safe cinematic sequence for the November 23 demo.

**Architecture:** Reuse existing robot-camera producer/WebRTC, laptop dashboard and NormaCore motion bridge. Local CV provides target observations and per-view scores; a single local state machine requests bounded movements through exclusive motion ownership; optional Claude MCP commands enqueue the same jobs as dashboard controls. Phone, voice, Claude cloud and internet are not needed for the core demonstration.

**Tech Stack:** Existing repository frontend/backend + local OpenCV (or current computer vision library) + installed NormaCore Station/robot driver. Determine exact versions and paths in Slice 0; avoid inventing a parallel stack.

**Spec:** `docs/AI_CAMERA_DIRECTOR_DEMO_SPEC.md`. Follow existing root `AGENTS.md`, `docs/AGENTS_DEMO_ADDENDUM.md` (overrides delegation conflicts) and `docs/LOCAL_DIRECTOR_CONTROL_CONTRACT.md`.

## Global Constraints

- Branch: `beta/ai-camera-director`; inspect branch/status first, preserve all dirty work and existing feature commits, never force reset or push over main.
- Baseline reportedly at main commit `8d06da1`, to be verified. Preserve existing Pixel QR, optional phone/voice, WebRTC, separate recordings, arm lift-over, fixed MCP presets and Claude key behavior.
- One agent only; use **Sonnet as the lead model for most coding**. Never delegate/parallelize.
- All detection, tracking, view scoring, motion and STOP work **with phone disconnected, voice off, internet disconnected and `CLAUDE_API_KEY` unset**.
- Every preset means **every current approved, reachable named camera preset**, not every arm pose. Reacquire target after major viewpoint changes; don't assume a short-term track survived them.
- Do not move physically until measured joint feedback, physical table clearance for **all swept configurations**, conservative local bounds, stop behavior and an operator-supervised dry-run are verified.
- Do not guess hardware IDs, joint arrays, calibration units, camera-to-gripper offsets, valid reachability or safe transition routes.
- All network, vision and motor faults HOLD motion. No fake successful score, pose or collision-check evidence.
- Preserve robot clip recording semantics and gitignored `Camera recordings/`; cinema recording needs an open/visible live tab under current implementation.

## Implementation Map (filled from ACTUAL repo in Slice 0)

Use these responsibility names, adapting file locations to the existing project. **Do not create a second server or incompatible frontend folder just to match these example paths.** At Slice 0, record actual paths in `docs/IMPLEMENTATION_MAP.md`.

- `camera_director/types.py`: normalized `TargetSelection`, `TargetObservation`, `ViewCandidate`, `PresetResult`, `DirectorJob` and status types; add equivalents in existing domain package if present.
- `camera_director/frame_source.py`: timestamped consumer of the already-open real robot camera; no duplicate camera owner.
- `camera_director/target.py`: reference-sticker and manual ROI acquisition feeding one tracker.
- `camera_director/quality.py`: pure local score and settled-frame aggregation.
- `camera_director/preset_search.py`: per-preset search with reacquisition, candidate scoring and best measured pose catalogue.
- `camera_director/motion.py`: adapter to **existing** motion owner, measured joints, verified routes, STOP/watchdog; no raw unclamped SDK calls outside it.
- `camera_director/jobs.py`: local job state machine for `track`, `center`, `survey_all`, `cinematic`, `director_tour`.
- Existing web dashboard component(s): camera overlay, ROI selection, per-position cards/gallery, progress/STOP, camera director action buttons.
- Existing API/MCP registration: high-level job start/status/cancel, no continuous servo loop through Claude.
- `tests/`: unit tests for normalized coordinates, target rejection, stale frames, scoring, job transitions, safety arbitration and cinematic route selection; archived replay fixtures with no private image content.

If project language differs, preserve responsibility boundaries using native modules instead of blindly copying `.py` paths.

## Review Focus - test these failure classes in their owning slice

1. Browser letterbox/crop/rotate: drawn ROI maps to the **correct source image** rather than the CSS element (Slice 1).
2. Sticker absent, distractor label, low-confidence or stale frame: UI offers manual selection and controller does not move (Slice 2).
3. Manual/preset/STOP action while director owns motors: current job releases lease and robot HOLDS without conflicting commands (Slice 3).
4. Target lost or wrong target after switching to another preset: score is invalid and that preset is skipped (Slice 5).
5. Cinematic route exists only as endpoints but has unvalidated swept clearance: block physical path; provide storyboard fallback (Slice 6).

---

## Slice 0 - Beta isolation, capture source, localization and safety audit

**Deliverable:** Source map, preservation tests and a physical safety-gate report; zero autonomous movement.

**Interfaces:** Map real robot source IDs, existing WebRTC producer, dashboard component, known presets, joint readback, motion/STOP APIs, robot clip recording API and installed Station version. Produce `docs/IMPLEMENTATION_MAP.md` and config `camera_director_enabled=false` by default.

- [ ] Check `git status`, `git log`, current branch, uncommitted edits; establish beta branch without resetting others' work; verify whether `8d06da1` is ancestor.
- [ ] Read real `AGENTS.md`, `CLAUDE.md`, developer readme, current robot/Station code, topview/right presets, `lift-over` and localization/pixel-grid source. Document which parts of external reference repo are and are NOT present locally.
- [ ] Trace moving wrist camera vs fixed external camera and identify decoded frame producer; inspect current stream clock/source IDs. Prove baseline camera still works with Pixel absent.
- [ ] Determine actual joint units, measured vs commanded telemetry, URDF/kinematics, table plane, camera/gripper/cable collision model, pre-approved paths, manual STOP and overload response. Write a **PASS / BLOCKED with exact reason** safety gate for physical movements, including full swept clearance; do not infer safety from lift-over alone.
- [ ] Add feature flag and test `test_director_disabled_preserves_fixed_presets` (expected: no director commands when flag off); run existing backend/web tests as discovered.
- [ ] Run and record repo-specific verification commands, baseline status and how to launch a real/replay camera. Commit only documentation/config/tests once green.

**Go/no-go:** Working robot-only preview, source and motion interfaces documented, regression behavior preserved. If safety blocked, later non-motion slices may proceed but physical moves stay disabled.

## Slice 1 - Browser target selection, overlay and diagnostics (no motion)

**Deliverable:** Draw any ROI on a robot video frame, see accurate target box/selection and source/frame metadata.

**Interfaces:** `TargetSelection(source_id, frame_id, timestamp, bbox_xyxy_normalized, origin)`; callback/event to the existing dashboard API (use actual names after audit).

- [ ] Write failing UI coordinate tests for `contain`, `cover`, rotated/mirrored video, scroll/resizing, letterboxes, source change and invalid/too-small ROI.
- [ ] Implement `Select Object` drag rectangle with confirm/reselect/cancel, source label and consistent overlay. Robot feed visible by default without a paired Pixel.
- [ ] Pass selection to local backend with frame/source metadata and reject stale or wrong-source targets. Mock source may be used for UI testing, clearly labeled.
- [ ] Run frontend tests, screenshot the box on a real robot feed, check existing camera/recording UI, commit.

**Gate:** ROI corresponds to same physical image region on video at different window sizes; never moves robot.

## Slice 2 - Local reference sticker discovery and generic tracking (no motion)

**Deliverable:** `Auto-find Hack Lab sticker` produces a validated reference target in `topview`; manual rectangle also starts same tracker; confidence/loss overlay updates live.

**Interfaces:** `TargetAcquisition.acquire(frame, mode, reference?) -> TargetSelection|TargetNotFound`; `TargetTracker.update(frame) -> TargetObservation|TargetLost`.

- [ ] Write failing tests for missing sticker reference, false sticker-like text, feature match with poor inlier ratio, stale frames and manual ROI path. Use actual captured topview frames and a known negative fixture when available.
- [ ] Inspect real sticker reference: if absent allow one-time confirmed ROI crop saved locally. Try ORB/AKAZE + homography/RANSAC with temporal consistency; optional OCR to support identity. Never assume sticker artwork or use cloud Claude to detect it.
- [ ] Share one tracker for both acquisition modes; cap frame queue to latest valid frame, track identity/confidence and emit `TARGET_LOST` on uncertainty.
- [ ] Render live box, confidence, frame age and target state; save one annotated proof image and failed-case log; run replay + live no-motion check, commit.

**Gate:** Detects the actual sticker if reference is usable or transparently requests manual selection. Tracks selected generic object on stationary/moving camera footage without motor control.

## Slice 3 - Local safe motion owner and dry-run centering

**Deliverable:** Motor telemetry/STOP/motion ownership accessible to director; computed small corrections shown in dry-run. No real moving until physical gate PASS.

**Interfaces:** `MotionBridge.get_measured_pose()`, `is_route_verified(route_id)`, `plan_small_move(delta,region)->Permit|Reject`, `execute_verified(step)->result`, `hold(reason)`; bind these to existing real interfaces.

- [ ] Write failing tests: stale servo data, conflicting preset command, manual override, STOP preemption, invalid route, table-clearance missing, overload, outside region -> REJECT/HOLD.
- [ ] Reuse actual installed Station, existing lift-over commands and calibrated localization only after mapping their purpose/units. Keep grasp-plane pixel-to-world distinct from eye-in-hand image Jacobian.
- [ ] Implement `dry_run` with logs of predicted joint deltas, proposed image effect, TTL, source/pose data freshness; explicit UI badge `DRY RUN`, disabled physical motion switch unless gate PASS.
- [ ] Exercise STOP and conflicting control through existing dashboard and confirm logged HOLD, run tests, commit.

**Gate:** Faults cannot silently produce raw motor commands; operator sees safety status. Physical dry-run does not move the robot.

## Slice 4 - Real centering near `topview` (operator supervised)

**Deliverable:** Small closed-loop corrections safely center selected target from `topview` and keep it visible; record measured before/after.

**Interfaces:** `center_target(target_id, preset_id='topview', dry_run=True) -> job_id`; local image-based controller uses measured `delta_image ~= J * delta_joint` only in approved region.

- [ ] Write failing control tests for wrong image-to-joint sign, excessive step request, target stale/lost, oscillation, unresponsive robot, and deadline exceed -> HOLD.
- [ ] Verify actual table/camera swept clearance, mount/gripper offsets and operator STOP; if not verified, **do not execute physical movement**. Keep dry-run output and request the specific missing clearance proof.
- [ ] With operator present, perform smallest safe perturbations; measure actual image shifts, estimate damped local Jacobian and direction; limit speed/step, settle then reobserve.
- [ ] Run bounded loop and log normalized center error, selected target identity, measured joint positions, fault/stop response and original screenshot. Compare before/after on same target, commit.

**Gate:** If physically verified, target comes closer to desired composition with no uncontrolled oscillation and Stop works. Otherwise `BLOCKED_BY_SAFETY` with tests and dry-run evidence, not a fake pass.

## Slice 5 - Critical: best view near EACH existing approved preset

**Deliverable:** A **preset result for every approved region** (`best valid view` or explicit `SKIPPED`), each with thumbnail, measured pose, score and safe route.

**Interfaces:** `survey_preset(target_id, preset_id, candidate_limit, dry_run=True) -> PresetResult`; `survey_all(target_id, preset_ids, dry_run=True) -> DirectorJob`; `recall_best(preset_id) -> Job`.

- [ ] Write failing tests for empty preset set, unknown route, wrong camera, target loss after preset change, stale measured pose, no better candidate, high score on wrong target and unsafe intermediate path.
- [ ] Read existing named preset list from real arm controls (no fabricated `left/front`); make each preset `pending/valid/skipped/blocked` and show reason.
- [ ] Validate existing route to each position including **intermediate** table clearance, STOP and telemetry. Transition only on approved existing routes. On arrival, reacquire same target from saved reference/keyframes; do not carry over stale ROI after large move.
- [ ] Within each approved region, enumerate a few prevalidated candidate waypoints/perturbations. Settle, capture multiple fresh frames, compute robust median quality, record actual measured joint pose + thumbnail + frame metadata.
- [ ] Rank candidates with hard identity/safety gates and quality function from spec, return along approved route to best candidate, settle and re-evaluate before marking `valid`. Keep baseline if it was best.
- [ ] Render region cards with old/best score, image, target box, measured pose and `Recall` button. Test multiple actual presets where safe and all failure paths in replay. Commit.

**Gate:** **Every approved preset** gets a truthful outcome; at least two actual regions with valid observable scores make an especially strong live demo. If only one is safe/visible, display honest skipped reasons rather than broadening motion bounds to fake coverage.

## Slice 6 - Cinematic Director using verified best shots

**Deliverable:** Smooth, **route-safe** shot sequence among saved valid best viewpoints, with target checks and robot clip recording.

**Interfaces:** `create_storyboard(valid_preset_results)->ShotPlan`; `start_cinematic(shot_plan_id, dry_run=True)->job_id`; `stop_cinematic()->hold_result`.

- [ ] Write failing tests for unverified route, sequence with <2 valid shots, stale target, joint feedback loss, STOP, tab hidden/recording stopped and incompatible arm configuration.
- [ ] Build storyboard UI showing order and shot thumbnails; allow operator reorder only if transition remains verified safe. Single shot with hold is valid fallback, not fake multi-shot cinema.
- [ ] For each approved pose-to-pose path use existing validated transition segments, smooth when supported (bounded velocity/acceleration/jerk); **never interpolate shortcuts** through the table. At stable poses reacquire selected object and reframe with small already-validated corrections, then dwell for capture.
- [ ] When transitional target visibility cannot be maintained, insert a shot cut/dwell between poses, or skip the segment; do not do unsafe pursuit. HOLD on fresh-state loss/fault; STOP always priority.
- [ ] Integrate **existing** robot recording without changing laptop folder or hidden-tab stop rules; keep dashboard tab/feed active for the demo. Verify actual recorded clip and status. Commit.

**Gate:** Demonstrated multi-shot camera motion only along cleared routes; else clearly labeled storyboard/still-montage fallback and no unsupported claims of a 3D orbit.

## Slice 7 - One-click Director Tour and demo-quality UI

**Deliverable:** One dashboard action orchestrates sticker/ROI target -> topview center -> survey every permitted preset -> select valid shots -> cinematic -> save/summary; compact state and progress for presentation.

**Interfaces:** `start_director_tour(target_mode, preset_ids, dry_run=True)->job_id`; `get_director_job(job_id)->status`; `cancel_director_job(job_id)->hold_result`.

- [ ] Write failing integration tests for duplicate start, no target, preset skipped, safety block, camera disconnect and operator stop during tour.
- [ ] Implement explicit state steps, progress `(preset 2 of 4, candidate 3/6)`, visible STOP, before/after cards, scores, safe-route and frame-age badges, downloadable job summary.
- [ ] Add one optional demo-friendly feature if core works: **Best Shots gallery**, `Recall best`, or `Before/After` slider (prefer fastest value). Avoid UI redesign.
- [ ] Run offline/no-phone/no-voice/no-Claude test and record proof. Commit.

**Gate:** One-click autonomous local sequence works as far as safety/visibility permit. Report skipped/blocked states without claiming unseen views were optimized.

## Slice 8 - Optional high-level Claude MCP integration

**Deliverable:** Existing Claude can start/status/cancel a **local** director job; no realtime dependency.

- [ ] Write failing tests for high-level tool contract, missing `CLAUDE_API_KEY`, timeout and cancellation. Require dashboard path without any Claude connectivity.
- [ ] Bind `camera_director_start_tour`, `camera_director_improve_all`, `camera_director_get_status`, `camera_director_stop` to existing MCP namespace and local asynchronous job API. Tool responses return `job_id`/status, NEVER raw motor paths.
- [ ] Test that Claude interruption does not alter a safely running local job, and key absence never disables direct dashboard job. Commit.

**Gate:** One optional voice/text command can start same local workflow; no regression to existing presets or voice.

## Slice 9 - Stretch: smartphone camera adapter

No phone changes unless preceding robot-only camera director works, user wants it, and time remains. Treat as a new independently testable source adapter with its own calibration/geometry questions; never merge camera feeds merely to guess target 3D pose.

---

## Evidence and acceptance for the Nov 23 demo

- Robot dashboard initially shows robot feed without any Pixel pairing.
- UI displays actual sticker detection or one-time taught reference and retains manual selection for arbitrary objects.
- For topview and every additional **approved** named preset: target visible or explicitly skipped, before/after score, thumbnail, selected camera, frame age and actual measured joint pose.
- One `Optimize all positions` action emits a complete region catalogue. `Recall best` returns using verified route and rechecks image.
- One `Cinematic` action follows a verified shot storyboard, with a visible STOP. Robot clip stored under existing `Camera recordings/` if enabled; validate file before claiming successful recording.
- No phone, internet, voice or Claude API is required for main demonstration. Tests run locally; hardware proof and replay proof labeled separately.
- Safety evidence: dry-run logs, approved route IDs, actual swept clearance verification, telemetry data freshness, manual stop, target-loss HOLD, collision checks. If missing, physical execution remains disabled.
- No subagents or parallel coding agents were used. One verified commit per completed slice.

## Demo priority if scope must be cut

**Must have:** Slice 0 -> 1 -> 2 -> 3 -> 4 -> 5. Demonstrate best view near actual safe presets, even with only 2 verified regions.

**High value:** Slice 6 cinematic and Slice 7 one-click tour. Use storyboard cuts if continuous moves are physically unsafe.

**Nice to have:** Slice 8 Claude MCP; Slice 9 phone. Do NOT sacrifice local best-view quality, safety or tests for these extras.

## Execution instruction

Do not stop after writing more planning docs. Inspect current repo, fill actual interface map, and execute one slice at a time in the **current single-agent session**, using Sonnet for ordinary coding. No subagents. At each gate output: commit, tests run and actual result, camera/target evidence, real-hardware-versus-replay distinction, safety status, next slice. Ask only for an essential user-controlled safety measurement/approval or important behavioral ambiguity.
