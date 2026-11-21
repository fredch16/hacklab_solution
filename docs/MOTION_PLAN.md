# Motion plan: getting to a saved view without breaking the arm

**Version 3** (after council round 1). Implemented in `motion.py`, tested on the simulated arm only (`python3 tests/test_motion_sim.py`, 76 checks). **Nothing here has run on the real arm yet.**
Background: [HARDWARE.md](HARDWARE.md), [MOTION.md](MOTION.md).

## Goal

Click a view in the web UI → the follower arm (ElRobot, 7 joints + gripper, camera on the gripper) moves to that saved view. It must not hit the table or itself or pull the camera cable, and a person must be able to stop it instantly.

## Council round 1

| Reviewer | Score for v1 | Main point |
|---|---|---|
| GPT-6.2 Sol | 3/10 | Only move from known poses on checked routes; stricter stop/fault handling; views sit on joint limits |
| Gemini 4 Neon | 5/10 | Shoulder-first lift drives the gripper ~65 mm into the table at `home`; merge to 3 phases |
| Claude Fable 5.2 | 6/10 | Same collision found independently (~49 mm); 3 phases; tie views to the calibration |

Gemini and Claude each built an offline kinematic model from the URDF (joint angle = `lower + p·(upper − lower)`, which they report matches all saved snapshots) and reached the same conclusion: **with the four pitch joints moving together, every view's path to the transit pose stays at or above the height of its own end poses; with the shoulder moving alone, `home` collides.** v3 adopts that.

| Round 1 finding | Decision in v3 |
|---|---|
| Shoulder-alone lift collides at `home` (Gemini, Claude) | **Three segments.** M2, M3, M4, M6 always move together: rise → turn → descend. |
| "Any pose" start is unsafe (all three) | Moves only start from a **known place on the route network** (at a view, on a view's spoke, at the hub). Otherwise refused; a slow, explicit "recover to transit" exists for supervised use. |
| `<60 steps` shortcut allowed `home → top` directly (Sol, Gemini) | **Shortcut removed.** Every move goes through the hub. |
| Views saved on/past joint limits were driven there via `JointArc.include` (all three) | `include()` removed. Targets are **clamped 30 steps inside the calibrated range**; `check_views()` lists affected views for re-capture. Wrapped, tiny or missing calibration is refused. |
| Views silently shift if the arm is recalibrated (Claude) | Each view stores the calibration ranges it was captured with; a move is **refused if live ranges differ by more than 15 steps**. |
| False arrival near stops; no guards during the settle wait (Sol, Gemini) | Arrival compares raw measured position with the goal per segment and requires standstill; guards keep running while settling. |
| Aborts left the last goal active; `hold()` threw on stale data (all three) | Every abort after the first command ends in a hold (goal := measured), shielded against cancellation. With stale data nothing is written (the last goal is ≤ 120 steps ahead, see leash). Refusals before the first command write nothing. Status is always reset. |
| Goal can run far ahead of a blocked arm (all three) | **Leash:** goals only advance while commanded − measured ≤ 120 steps; blocked for 0.4 s → abort. Servo speed ceiling = 1.5 × our peak + 50, never 0. |
| Station's current limit doesn't cover our writes (all three) | Own guards (table below). |
| Auto torque-on; gripper written on startup/hold (Sol, Gemini) | Moves **require torque already on**. `engage()` ("Hold here") writes goal := present with a low speed ceiling, then torque on. The gripper (M8) is **never written**. `hold()` never energises a limp joint. `torque.py on` now uses `engage()`. |
| Mirroring / other writers (all three) | Preflight reads Station's mirroring state and refuses while the follower is a mirror target. During a move the goal registers are read back: a goal we didn't write → abort. |
| Whole-frame freshness hides a dead arm or servo; short register dumps read as 0 (Gemini, Claude, Sol) | Freshness is tracked **per motor** from Station's per-motor read stamp; register dumps shorter than 71 bytes are refused; read errors that persist abort. |
| Smoothstep peaks at 1.5× nominal; accel units (Sol) | Speeds are true peak speeds (slow 200, normal 400 steps/s). Accel register 15 (1500 steps/s²). |
| Routes need a human check (Sol) | Per-view `checked` flag; **unchecked views only run at slow speed**; re-capturing clears it. |
| Wrong arm / missing motor changes which id is "the gripper" (Sol, Gemini) | Views record their bus serial; preflight requires that serial and exactly motors 1–8. Gripper is the constant 8. |
| `transit` leaked into the view list Claude's director sees (Gemini) | `view_names()` / `view_descriptions()` exclude it. |

Not adopted, on purpose (scope for this weekend): dynamic torque limit (all three said skip); read-back confirmation of each hold; `CT_STOP_MIRROR` sent by us (we refuse and tell the user instead); FK as a runtime gate (kept offline/advisory); a hard ban on recovery from unknown poses (kept, but slow, explicit and supervised).

## The route network

Joint groups: **PITCH** = M2, M3, M4, M6 (shoulder, elbow, pitch, wrist pitch), always together · **TURN** = M1, M5, M7 (base yaw, forearm roll, wrist roll), only at the hub · M8 gripper never moves.

The **hub** is the transit pose's pitch configuration (yaw/rolls anywhere). `transit` was taught by hand: arm raised, gripper pointing straight up, wrist straight (about 53 cm tall per the reviewers' models).

| Segment | Joints | Goes to |
|---|---|---|
| rise | M2, M3, M4, M6 together | transit values → now at the hub |
| turn | M1, M5, M7 together | the target's values |
| descend | M2, M3, M4, M6 together | the target's values |

- A **spoke** is the straight joint-space line between a view and the hub with the view's yaw/rolls. After a Stop the arm is on a spoke or at the hub, and `locate()` recognises that from the measured joints alone (all pitch joints the same fraction of the way), so the next click plans from there: up the current spoke, turn, down the target's spoke. If the target is the spoke's own view it goes straight back down.
- Safety argument: (1) the hub is safe for any yaw/roll the views use (chosen by a person; the reviewers' models give ≥ 17 cm table clearance for the whole sweep); (2) each spoke lies between two poses a person taught, is watched once at slow speed, then marked checked. Nothing else is ever driven.

## Preflight (a failure writes nothing)

Transit pose saved · exactly motors 1–8 · view captured on this arm's serial and with this calibration · no servo fault bits or read errors · joints 1–7 holding · Station not mirroring onto this arm · calibration sane (no wrap, range ≥ 400 steps, present position within range) · the arm is on the network.

## Guards while moving (each ends the move and holds)

| Guard | Threshold (provisional until measured on the arm) |
|---|---|
| Joint not following (leash) | commanded − measured > 120 steps; goals stop advancing at once, abort after 0.4 s |
| Held joint pushed/sagging | a non-moving joint > 150 steps from where the segment started |
| Another writer | a held joint's goal register changes (immediate), or a moving joint's goal is not one we sent (0.25 s) |
| Stale data | any motor not read for 0.3 s |
| Servo fault | any status bit except the voltage bit (immediate) |
| Stalled | load (PWM duty) > 800 for 0.25 s |
| Overcurrent | > 250 raw (≈1.6 A) for 0.25 s |
| Read errors | a motor's reads failing for 0.25 s |
| Write failure | Station doesn't accept a command within 0.5 s |
| Not arrived | after a segment, a joint > 40 steps from its goal or still moving after 2 s |
| Stop / cancel | immediate |

Every move appends per-joint max lag, load and current and the max temperature to `motion_log.jsonl` for tuning.

## FK model (advisory, offline)

`fk.py` models the arm from the vendored URDF. Used to print the lowest point of every route as a second opinion (and it is how the round-1 reviewers found the `home` collision). Not a runtime gate.

## Test protocol on the real arm

Follower "(Self-controlled)" in Station, one person at the arm, slow speed, desk cleared, cable slack checked by hand at full transit height and through the roll sweep (torque off) before the first powered run.

1. `python go.py --where` (nothing moves): calibration matches, mirroring off, location, register dump complete.
2. Hand-pose to `transit` (Go limp → pose → Hold here). Check held-still lag/load/current/temperature readings.
3. `transit → wide → transit`, then `top`, `close` (pitch-only views).
4. `left`, `right`, `side_low` after re-capturing them off the joint limits with a level horizon (smaller M5 sweep).
5. `home` last, watching its spoke closely.
6. Press Stop mid-segment once; check the arm holds and the next click recovers.
7. Mark each view checked after a clean there-and-back run. One full tour slow, then normal speed.
8. Read `motion_log.jsonl`, tune the lag/load/current limits per joint.

## Known limits

- No collision model at runtime: safety rests on taught poses, joint ordering, supervision and guards.
- If the link to Station dies mid-move we cannot confirm the hold; the last goal is at most 120 steps from the arm. Emergency stop is the power plug, and at transit the arm then falls from ~50 cm: prefer Stop, pad the desk for first runs.
- Whether a goal write energises a limp ST3215 joint, the idle telemetry values, and the servo's overload settings are still to be read from the real arm (step 1–2 above).
- `left`/`right` differ by ~179° of forearm roll (M5); watch the camera cable there until they are re-captured.
- A slow view-to-view move takes about 15–25 s.
