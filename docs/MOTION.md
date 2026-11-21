# Motion: how the follower gets from one view to another

How to use and extend the motion layer. The design, the reasoning and the council review are in [MOTION_PLAN.md](MOTION_PLAN.md); hardware facts in [HARDWARE.md](HARDWARE.md).

**Status: tested on the simulated arm only. The first runs on the real arm follow the test protocol in MOTION_PLAN.md, slowly, with a person at the arm.**

## Contract (please keep stable)

The control page, the Claude director and voice all go through these:

```python
# motion.py
await go_to_view(arm, name, max_speed=SPEED_SLOW, allow_unknown_start=False)
    -> {"ok": bool, "view": str, "error": str | None, "duration": float, "route": [str]}
await describe(arm, name=None)   # where is the arm / what would a move do; moves nothing
await hold(arm)                  # freeze where it is (skips limp joints and the gripper)
await engage(arm)                # limp -> holding: goal := present, then torque on (joints 1-7)
await release(arm)               # go limp; refused while moving
view_names(), view_descriptions()   # camera views; the transit pose is excluded
view_checked(name), mark_checked(name)
status                           # dict polled by UIs: moving, view, phase, phase_index/count, progress, error, route
```

- To stop a move, cancel the asyncio task running `go_to_view`: the arm holds and `CancelledError` propagates.
- A refused or aborted move returns `ok: False` with an `error` that is safe to show to the user.
- `arm` is a `station_arm.Arm` (real) or `sim_arm.SimArm` (simulated, with fault injection).

`views.json`:
```json
{"views": {"top": {"joints": {"1": 2120, "...": 0, "8": 1279},
                   "description": "top-down over the workspace",
                   "bus": "5B79016329",
                   "ranges": {"1": [1105, 2991], "...": []},
                   "captured_at": "...", "snapshot": "views/top.jpg", "checked": false}}}
```
Joint values are raw encoder steps (0–4095, 4096 per turn) keyed by motor id. `ranges` is the calibration at capture time: after a recalibration the saved steps no longer mean the same pose, and moves to that view are refused until it is re-captured. `transit` is a normal entry but is not offered as a camera view.

## What a move does

1. **Preflight** (nothing is written if it fails): transit saved, motors 1–8 present, right arm and calibration, no servo faults, arm holding, Station not mirroring a leader onto it, the arm is at a known place.
2. **Plan** on the route network: `rise` (M2, M3, M4, M6 together to the transit configuration) → `turn` (M1, M5, M7 to the target's values) → `descend` (M2, M3, M4, M6 together to the target). From a spoke or the hub, only the remaining segments.
3. **Run each segment**: smoothstep in joint space, goals streamed at 30 Hz, advancing only while the arm keeps up (leash of 120 steps). Guards on every tick; any fault holds the arm.
4. **Arrive**: every moved joint within 40 steps of its goal and standing still, checked after each segment.

Targets are clamped 30 steps inside each joint's calibrated range. The gripper is never written.

## Trying it

```bash
python3 tests/test_motion_sim.py      # 76 checks on the simulated arm (~30 s)
python3 fk.py                         # geometry model: heights per view and per route segment
python3 go.py --sim --where           # where is the (simulated) arm
python3 go.py --sim home top left     # run moves on the simulated arm
python3 capture_server.py --sim --port 8891   # the web UI against the simulated arm: /control
```

On the real arm (follower "(Self-controlled)" in Station, one person at the arm):

```bash
python3 go.py --where                 # moves nothing; shows location and any refusal reason
python3 go.py --engage                # limp -> holding (support the arm first)
python3 go.py wide                    # slow by default
python3 go.py --mark-checked wide     # after watching a clean there-and-back run
```

## Tuning after the first real runs

Every move appends a line to `motion_log.jsonl` with per-joint max lag, load and current and the max temperature. The limits at the top of `motion.py` (`LEAD_LIMIT`, `LOAD_LIMIT`, `CURRENT_LIMIT`, `DRIFT_LIMIT`, `ARRIVE_TOL`) are provisional until set from that data.

## The geometry model (`fk.py`)

Forward kinematics from the vendored URDF (`vendor/normacore/hardware/elrobot/simulation/`). Joint angle for motors 1–7 is `lower + p·(upper − lower)`, where `p` is the position within the calibrated range and `lower`/`upper` are the URDF limits; this is what Station's 3D viewer does (the inverted formula an earlier version of this file quoted only applies to the gripper). A second "true-scale" mapping (one step = 360/4096°) is included because the viewer's mapping overstates shoulder and yaw motion by about 7%.

It is an offline, advisory check: signs of M1, M2, M5 and M8 are verified against the snapshots, the others and the absolute offsets are assumed, and it does not model self-collision, the camera stalk or cables. On the current views it shows every route's lowest point is the taught view itself, and that lifting the shoulder alone from `home` would put the gripper below the table, which is why the pitch joints always move together.

## Code map

| File | What |
|---|---|
| `station_arm.py` | Station connection: joints, telemetry (load, current, status, per-motor freshness), ranges, torque, `write_goals`, mirroring state |
| `motion.py` | route network, planning, guarded execution, hold/engage/release |
| `go.py` | CLI for all of the above |
| `sim_arm.py` | simulated 8-motor arm with fault injection |
| `fk.py` | geometry model, route height report |
| `capture_server.py`, `capture_ui.html`, `control_ui.html` | web UI: capture views (`/`) and move to them (`/control`) |
| `tests/` | `test_motion_sim.py`, `test_fk.py` |
