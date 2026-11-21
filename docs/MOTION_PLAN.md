# Motion plan: getting to a saved view without breaking the arm

**Version 2** (after council round 1). Implemented in `motion.py`, tested on the simulated arm only (`python3 tests/test_motion_sim.py`, 70 checks). **Nothing here has run on the real arm yet.**
Background: [HARDWARE.md](HARDWARE.md), [MOTION.md](MOTION.md).

## Goal

Click a view in the web UI → the follower arm (ElRobot, 7 joints + gripper, camera on the gripper) moves to that saved view. It must not hit the table or itself or pull the camera cable, and a person must be able to stop it instantly.

## What changed from version 1 (review round 1)

| Round 1 finding | Decision |
|---|---|
| "Any pose" start with a blind shoulder-first lift is unsafe | Moves only start from a **known place on a fixed route network** (at a view, on a view's spoke, at the hub). Anything else is refused. An explicit, slow, supervised "Recover to transit" exists for unknown poses. |
| The `<60 steps` shortcut allowed `home → top` as one direct move (M2 +1494, M4 −1535) | **Shortcut removed.** Every move goes through the hub. |
| Views saved at or past joint limits (`home`, `left`, `right`, `side_low`) were driven to those values (`JointArc.include`) | `include()` removed. Targets are **clamped inside the calibrated range minus 30 steps**; `check_views()` lists affected views so they can be re-captured. Wrapped or tiny calibration ranges are refused. |
| Arrival check clamped the measured position and could report a false arrival | Arrival compares raw measured position with the goal, per segment, and requires the joint to be standing still. |
| Only cancellation held the arm; other failures left the last goal active | **Every** abort path ends in a hold (goal := measured) once anything has been commanded. Refusals before the first command write nothing. |
| Goal can run far ahead of the arm (residual travel if the link dies) | Goals **only advance while the arm keeps up** (commanded − measured ≤ 120 steps). The servo's own speed ceiling is set just above ours (1.5× peak + 50). |
| Station's current limit doesn't cover our writes | Own guards: tracking stall, servo status faults, overcurrent, stale data, pushed joints, failed writes. |
| Torque switched on automatically; gripper written on startup/hold | Moves **require torque already on** (a person pressed "Hold here"). The gripper (M8) is **never written** by any move, hold or engage. `hold()` never energises a limp joint. |
| Fighting Station's leader→follower mirroring | Preflight reads Station's mirroring state and **refuses while the follower is a mirror target**. |
| Smoothstep peaks at 1.5× the nominal speed; accel units | Speeds are now true peak speeds (slow 200, normal 400 steps/s); duration = 1.5 × distance / peak. Accel register set to 15 (1500 steps/s²). |
| Routes should be checked by a person before being trusted | Per-view `checked` flag (`go.py --mark-checked`, UI button). **Unchecked views only run at slow speed.** Re-capturing a view clears it. |
| Wrong arm / wrong motors | Views record the bus serial they were captured on; preflight requires that serial and motors 1–8. |

Not adopted (scope): dynamic torque-limit changes (skipped, as recommended), per-motor source timestamps (we use frame receipt age ≤ 0.3 s), read-back confirmation of holds, FK-based checks as a hard gate (advisory only, see below).

## The route network

Joint groups: **LIFT** = M2 (shoulder) · **SHAPE** = M3, M4, M6 (elbow, pitch, wrist pitch) · **TURN** = M1, M5, M7 (base yaw, forearm roll, wrist roll) · M8 gripper never moves.

The **hub** is the transit pose's pitch configuration (M2, M3, M4, M6 at the `transit` values; yaw/rolls anywhere). `transit` was taught by hand: arm raised, gripper pointing straight up, wrist straight.

Each view has a **spoke** to the hub. A move is a walk along spokes, one joint group at a time:

| Segment | Joints | Goes to |
|---|---|---|
| lift | M2 | transit value (leave the view: shoulder first) |
| fold | M3, M4, M6 | transit values → now at the hub |
| turn | M1, M5, M7 | the **target's** values, at the hub |
| shape | M3, M4, M6 | target's values |
| lower | M2 | target's value |

- Entering a view is the exact reverse of leaving it.
- A view can be flagged `"exit": "fold_first"` in `views.json` to swap lift and fold on its spoke (candidate: `home`, which is folded back with M2 at its minimum; to be decided by the first supervised run and the FK model).
- After a Stop the arm is still on the network (on a spoke or at the hub), and `locate()` recognises that from the measured joints alone, so the next click plans from there: finish going up the current spoke, turn, go down the target's spoke. If the target is the spoke's own view, it goes straight back down.
- The safety argument: (1) the hub is safe for any yaw/roll the views use (a person chose the transit pose for that); (2) each view's spoke is safe (a person watches it once, slowly, both ways, then marks the view checked). Nothing else is ever driven.

## Preflight (before the first command; a failure writes nothing)

Transit pose saved · motors 1–8 present · view captured on this arm's serial · no servo fault bits · joints 1–7 holding (torque on) · Station not mirroring onto this arm · calibration sane (no wrap, range ≥ 400 steps, present position within range) · the arm is on the network.

## Guards while moving (each ends the move and holds)

| Guard | Threshold (provisional until measured on the arm) |
|---|---|
| Joint not following | commanded − measured > 120 steps for > 0.6 s |
| Held joint pushed/sagging | a non-moving joint > 150 steps from where the segment started |
| Stale data | joint data older than 0.3 s |
| Servo fault | any status bit except the voltage bit |
| Overcurrent | > 250 raw (≈1.6 A) for > 0.3 s on a moving joint |
| Write failure | Station doesn't accept a command within 0.5 s |
| Not arrived | after a segment, a joint > 40 steps from its goal or still moving after 2 s |
| Stop / cancel | immediate |

Every move appends per-joint max lag and max current to `motion_log.jsonl`, so the thresholds can be tuned from real data after the first runs.

## FK dry-run (advisory)

`fk.py` (being built) models the arm from the vendored URDF with the mapping Station's 3D viewer uses. If it validates against the saved views, the planner can report the lowest point of each segment as a second opinion. It is not a gate: the network and guards must be safe on their own.

## Test protocol on the real arm

Follower "(Self-controlled)" in Station, one person at the arm with the power plug in reach, slow speed, cables checked for slack at both roll joints.

1. `python go.py --where` (nothing moves): confirms calibration, mirroring off, location.
2. Hand-pose to `transit` (Go limp → pose → Hold here), then `transit → wide → transit`, then `top`, `close`: the views that only differ in pitch.
3. Views with big rolls: `left`, `right`, `side_low` (after re-capturing them away from the joint limits and with a level horizon).
4. `home` last, watching its spoke closely; switch it to `fold_first` if lift-first looks wrong.
5. Press Stop mid-segment once; check the arm holds and the next click recovers.
6. Mark each view checked after a clean there-and-back run. Then one full tour slow, then normal speed.
7. Read `motion_log.jsonl`, tune lag/current limits.

## Known limits

- No collision model: safety rests on taught poses, ordering, supervision and guards.
- A slow move takes 10–30 s (five segments in sequence). Merging segments or raising the speed is a later step once routes are checked.
- If the link to Station dies mid-move we cannot confirm the hold; the last goal is at most 120 steps from the arm. Emergency stop is the power plug.
- `left`/`right` differ by ~179° of forearm roll (M5); watch the camera cable there.
