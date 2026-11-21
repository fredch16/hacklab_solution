# Motion plan: any pose → a saved view, without breaking the arm

Design for review (council round 1). Status: **proposal, nothing here has run on the real arm.**
Background: [HARDWARE.md](HARDWARE.md), [MOTION.md](MOTION.md), current code in `motion.py`.

## Goal

Click a view in the web UI → the follower arm (ElRobot, 7 joints + gripper, camera on the gripper) moves from **wherever it is now** to that saved view. It must not hit the table, itself, or yank the camera cable, and a person must be able to stop it instantly.

## Constraints

- Hackathon: M2 should work this afternoon (Sat), demo Sunday. Prefer simple and predictable over clever.
- No verified kinematic model yet (an FK model from the URDF is being built in parallel, see "FK dry-run").
- Positions are raw ST3215 encoder steps (4096/turn). We command the servos by writing goal position (0x2A), goal speed (0x2E), acceleration (0x29), torque enable (0x28) through Station.
- Station's `current-threshold` only protects its own leader→follower mirroring, **not our direct writes**. Any overload protection for our moves has to be ours.
- Views are poses a human physically put the arm in (taught), so each end pose is known-reachable and collision-free. The risk is the path between them.

## Joints

| Motor | Joint | Group |
|---|---|---|
| 1 | base yaw | TURN |
| 2 | shoulder pitch | LIFT (the big lever) |
| 3 | elbow pitch | SHAPE |
| 4 | pitch | SHAPE |
| 5 | forearm roll (~336°) | TURN |
| 6 | wrist pitch | SHAPE |
| 7 | wrist roll (~340°), camera rolls with it | TURN |
| 8 | gripper | never moved (camera mount) |

Saved views (steps):

```
motor          1     2     3     4     5     6     7     8
home        2095  1061  1906  3196  1899  1912  2303  1279
left        2547  3032  1444  1652  2949  3069  2268  1279
right       1675  3032  1381  1655   909  3071  2251  1279
top         2120  2555  1522  1661  1899  2991  2250  1279
wide        2117  1780  1521  2124  1900  2993  2251  1279
close       2104  2548  1522  2133  1941  2553  2261  1279
side_low    2518  3023  1668  1545  2923  2957  2173  1279
follower calibrated ranges: M1 1105–2991, M2 1071–3025, M3 924–3172, M4 897–3199,
                            M5 141–3955, M6 1017–3079, M7 164–3932, M8 1263–2833
```

## Proposed approach: taught transit pose + lift → turn → lower

One extra taught pose, **`transit`**: arm raised and compact, wrist roughly straight, camera well above the table, chosen by a human so that turning the base and the two roll joints through their used range hits nothing. Every non-trivial move goes through it in ordered phases:

| Phase | Joints that move | To | Why |
|---|---|---|---|
| 0. Hold | all | present position | goal := present, then torque on. No jump from a stale goal register |
| 1. Lift | M2 (shoulder) | transit value | rigid rotation of the whole arm about the shoulder: the most predictable way up and away from the table |
| 2. Shape | M3, M4, M6 | transit values | fold into the compact transit shape while high |
| 3. Turn | M1, M5, M7 | **target** values | all yaw/roll happens at the transit height, with the wrist straight |
| 4. Shape | M3, M4, M6 | target values | take the target's arm shape while the shoulder is still high |
| 5. Lower | M2 | target value | rigid rotation down into the taught pose |

Rules:
- A phase is skipped if its joints are already within 25 steps of that phase's goal.
- **Shortcut:** if TURN joints (M1, M5, M7) of present and target differ by < 60 steps each, skip the transit and do a single direct move of the pitch joints (covers top ↔ wide ↔ close, which share yaw and roll). Open question: should the shortcut still order shoulder vs. the rest?
- Each phase is a joint-space move with smoothstep easing, streamed at 30 Hz, duration `max(1.0 s, furthest joint / speed)`, default speed 400 steps/s (~35°/s). The UI offers a "slow" setting (200 steps/s) and first runs use it.
- Interpolation stays inside each joint's calibrated arc (existing `JointArc`), 30 steps clear of the stops.
- Unknown present pose (e.g. arm was left limp somewhere odd) is handled the same way: phase 1 lifts from wherever it is.

## Safety guards during every phase

1. **Stop = hold.** Cancel/Stop button/Ctrl-C writes goal := present for all joints immediately. Torque stays on (the arm must not drop).
2. **Tracking-error abort.** If any moving joint's present position lags its commanded waypoint by more than `LAG_LIMIT` (proposed 150 steps ≈ 13°) for more than 0.3 s → treat as blocked: hold, report `ok: False` with the joint. (Servo ceiling speed is set to ~2× the waypoint speed so normal tracking lag is small.)
3. **Current abort.** Read present current (register 0x45) from the state stream each tick; if any joint exceeds `CURRENT_LIMIT` for 0.2 s → hold and abort. Starting value: to be measured during the first slow runs (log max current per joint), then set to ~1.5× the observed peak.
4. **Stale data abort.** If Station joint data is older than 0.3 s → hold and abort.
5. **Arrival check** after each phase (not just at the end): 40 steps tolerance, otherwise abort before starting the next phase.
6. **One move at a time** (lock); a new click while moving is rejected (or: stops the current move first, open question).
7. **Mirroring check.** Refuse to move if Station is mirroring the leader onto the follower (they would fight). If we can't detect it reliably, the UI shows a reminder.
8. **Optional torque limit** (register 0x30) lowered during moves so a collision stalls gently. Open question: is this worth it, given gravity load on the shoulder?

## FK dry-run (advisory, built in parallel)

`fk.py`: forward kinematics from the vendored ElRobot URDF using the same steps→angle mapping Station's 3D viewer uses (the viewer visibly matches the real arm). Before executing, the planner samples each phase's path and computes the height of the elbow, wrist, gripper tip and camera above the base plane. If any point goes below `TABLE_MARGIN` → refuse and say which phase. It is a second opinion, not the primary safety: the taught transit + ordering + guards must be safe on their own.

## Test protocol on the real arm

1. Sim: unit tests for phase planning (order, skips, shortcut), guards (inject lag / current / stale data), cancel-holds.
2. Real arm, follower "(Self-controlled)", one person at the arm with the power plug in reach, slow speed (200 steps/s):
   a. hold only (phase 0) → arm stiffens where it is, no movement.
   b. current pose → `transit`.
   c. `transit` → `wide` → `transit` (pitch-only views first), then `top`, `close`.
   d. views with big rolls: `left`, `right`, `side_low`, then `home`.
   e. full tour ×1 slow, then ×5 at normal speed.
3. Log per-joint max lag and max current for every move; tune limits from the data.

## Open questions for reviewers

1. Is lift → shape → turn → shape → lower the right order, and is "shoulder first, alone" right for lifting? Should phases 1+2 or 4+5 be merged or reordered for some configurations (e.g. leaving `home`, where M2 is at its minimum and M4 at its maximum, i.e. folded)?
2. Is a single transit pose enough, or do we need the roll joints (M5/M7) sequenced separately from base yaw?
3. Guard thresholds: are lag 150 steps / 0.3 s and the current approach sensible for ST3215 servos? Anything better available from the servo (present load 0x3C, status 0x41)?
4. Torque limit during moves: worth doing?
5. What are we missing that could break hardware?
