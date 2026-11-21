# Motion: getting the follower from one view to another safely

Handoff for whoever works on motion planning (M2). Read [HARDWARE.md](HARDWARE.md) first for the arm, motor ids and ranges.

## Where we are

- 7 views are captured in [`views.json`](../views.json) (+ snapshots in `views/`): `home, left, right, top, wide, close, side_low`.
- `motion.py` already moves between them with a simple, conservative planner (below). It is **tested on the simulated arm only**. **Our code has not driven the real arm yet.**
- Nobody has checked yet whether any view-to-view path hits the table, the arm itself or the camera cable.

## Contract: please keep these stable

The rest of the team (Claude director, voice, overlay) codes against these:

```python
# motion.py
await go_to_view(arm, name) -> {"ok": bool, "view": str, "error": str | None, "duration": float}
view_names() -> list[str]            # keys of views.json, also Claude's tool enum
view_descriptions() -> dict[str, str]
await hold(arm)                      # freeze where it is
```

`views.json` format:
```json
{"views": {"top": {"joints": {"1": 2120, "...": 0, "8": 1279},
                   "description": "top-down over the workspace",
                   "bus": "5B79016329", "captured_at": "...", "snapshot": "views/top.jpg"}}}
```
Joint values are raw encoder steps (0–4095), keyed by motor id as a string. Extra fields are fine (e.g. `"via": ["home"]` for waypoints); the director only reads names and descriptions.

`arm` is a `station_arm.Arm` (real) or `sim_arm.SimArm` (fake). Both provide:
`joints() -> {id: pos}`, `ranges() -> {id: (min, max)}`, `await write_goals({id: pos}, speed=None, accel=None)`, `await set_torque(bool, ids=None)`.

## What `motion.py` does today

1. **Hold first:** writes goal = present position, then enables torque, so a stale goal register can't make the arm jump.
2. **Joint-space interpolation:** every joint goes from present to target **simultaneously and linearly in joint space**, eased with smoothstep (`3s² − 2s³`), streamed as goal positions at **30 Hz**.
3. **Duration** = `max(1.5 s, biggest joint distance / 500 steps/s)` (~44°/s for the joint that travels furthest).
4. **Limits:** interpolation happens inside each joint's calibrated arc (`JointArc`), 30 steps clear of each stop, so a move never sweeps through the unreachable part of a joint's circle (that matters for wrapping arcs). Poses up to 300 steps outside the arc are trusted (the arm has physically been there); anything further out is clamped.
5. **Servo settings:** goal speed 1500 and accel 50 are written once at the start as a ceiling; the waypoints set the real speed.
6. **Gripper excluded:** motor 8 never moves unless `include_gripper=True` (the camera is on the gripper).
7. **Arrival check:** 0.4 s after the last waypoint, any joint more than 40 steps from target → `ok: False` ("blocked or overloaded?").
8. **Cancel / Ctrl-C:** writes goal = present position (holds where it is).
9. A module-level lock stops two moves running at once.

Station adds its own protection: `current-threshold: 100` in `station.yaml` (the servo driver limits current).

## Known risks: what the math work should solve

- **Joint-space straight lines aren't straight in space.** Going `home` (folded, M2≈1061) to `left` (M2≈3032, M5≈2949) moves the shoulder, elbow and both rolls at once; the gripper/camera can swing through the table or into the arm. Nothing checks this today.
- **The roll joints (M5, M7) travel far.** `left`/`right`/`side_low` have M5 at ~2949 / ~909 / ~2923 vs ~1900 elsewhere, i.e. ±90° of forearm roll. That also makes their pictures sideways, so those three views will probably be re-captured with a level horizon (smaller M5 changes).
- **No collision model.** The URDF + meshes are in `vendor/normacore/hardware/elrobot/simulation/` (see below), but nothing uses them yet.
- **Cable wrap:** the camera USB cable runs down the wrist; big M5/M7 rotations can pull it.
- **Load:** the arm sags under gravity at some poses; the arrival tolerance (40 steps) may need tuning per joint.

Cheap ideas, roughly in order of effort:
1. **Route via waypoints:** e.g. always go through `home` or a "safe high" pose; add `"via"` to views.json for risky pairs.
2. **Order the joints:** lift first (shoulder/elbow up), then pan/roll, then lower. That's three phases instead of one simultaneous move.
3. **Forward kinematics check:** use the URDF to compute the camera/gripper height along the interpolated path and reject or reroute paths that dip below the table plane (z < margin).
4. Full collision checking against meshes (probably overkill for the weekend).

## Mapping encoder steps ↔ URDF joint angles (unverified)

URDF: `vendor/normacore/hardware/elrobot/simulation/elrobot_follower.urdf` (joints `rev_motor_01`…`rev_motor_07`, gripper `rev_motor_08` + two prismatic jaws). Joint limits (rad):

| Joint | lower | upper | axis (approx.) |
|---|---|---|---|
| rev_motor_01 | −1.551 | 1.551 | z |
| rev_motor_02 | −1.612 | 1.612 | −x |
| rev_motor_03 | −1.761 | 1.761 | −x |
| rev_motor_04 | −1.753 | 1.753 | −x |
| rev_motor_05 | −2.620 | 3.252 | −y |
| rev_motor_06 | −1.378 | 1.764 | −x |
| rev_motor_07 | −3.201 | 2.734 | −y |
| rev_motor_08 | 0 | 2.203 | y |

Station's 3D view maps a motor to its URDF joint like this (`station-viewer/src/st3215/robot-rendering/BaseRobotRenderer.tsx` + `devices/elrobot/config.ts`):

```
p = (pos - range_min) / (range_max - range_min)       # 0..1 within the calibrated arc (wrap-aware)
jointPosition = lower + p * (upper - lower)
angle = jointPosition            if joint name ends with "_1"
        upper - jointPosition    otherwise
```

We copied this from Station's code and **have not checked it against the real arm**. In particular, the `upper - jointPosition` branch doesn't map into `[lower, upper]` for symmetric limits. Verify it with a couple of known poses before building kinematics on it (e.g. put the arm straight up, read `python joints.py`, compare with the 3D view at :8889).

## How to test without breaking anything

1. **Sim first:** `python3 tests/test_motion_sim.py` checks the arc math, smooth moves, gripper untouched and cancel-holds on `sim_arm.SimArm`; rerun it after changing `motion.py`. To try things interactively: `uv run python director.py --sim`, or write a script like:
   ```python
   from sim_arm import SimArm; from motion import go_to_view
   arm = SimArm(); print(await go_to_view(arm, "top"))
   ```
2. **Real arm, supervised, slow:**
   - Follower on "(Self-controlled)" in Station.
   - One person at the arm; Ctrl-C freezes it, pulling the follower's power is the emergency stop.
   - `python go.py home top --speed 250` (half speed), then `python go.py --tour 1`, then `--tour 5` for the M2 check.
3. **Never leave a powered arm unattended** (hackathon rule).

## Code map

| File | What |
|---|---|
| `station_arm.py` | Station connection: joints, ranges, torque, `write_goals`, leader/follower detection, `settled()` |
| `motion.py` | `JointArc`, `move_to`, `go_to_view`, `hold`, views.json loading |
| `go.py` | CLI: `go.py top`, `go.py home top home`, `--tour N`, `--speed`, `--gripper` |
| `sim_arm.py` | Fake arm: joints glide toward goals at a fixed speed |
| `capture_server.py` + `capture_ui.html` | Capture web UI (:8890) to (re)capture views |
| `vendor/normacore/` | Station Python client, generated protobufs, ElRobot URDF/meshes/manuals (MIT) |
