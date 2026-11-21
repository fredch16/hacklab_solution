# Motion: how the follower gets from one view to another

How to use and extend the motion layer. The design, the reasoning and the council review are in [MOTION_PLAN.md](MOTION_PLAN.md); hardware facts in [HARDWARE.md](HARDWARE.md).

**Status: the hub route has run on the real arm (see MOTION_PLAN.md, "First real session"). Direct moves and the other speed-ups from [MOTION_OPT.md](MOTION_OPT.md) are tested on the simulated arm only. Use one controlling program per session (the web server): a Stop on the page cannot stop a move another process started.**

## Contract (please keep stable)

The control page, the Claude director and voice all go through these:

```python
# motion.py
await go_to_view(arm, name, max_speed=SPEED_SLOW, allow_unknown_start=False)
    -> {"ok": bool, "view": str, "error": str | None, "duration": float, "route": [str],
        "kind": "direct" | "arc" | "hub" | None, "hold": None | "confirmed" | "unconfirmed" | "limp"}
await describe(arm, name=None)   # {"ok", "error", "blockers", "where", "route", "kind"}; moves nothing
route_kind(start_view, target_view)  # ("direct" | "arc" | "hub", why) from views.json alone, no arm needed
await hold(arm)                  # stop where it is; returns "confirmed" / "limp", raises if unconfirmed
await engage(arm)                # limp -> holding (joints 1-7), with the same arm checks as a move
await release(arm)               # go limp (joints 1-7); refused while moving
SPEED_FIRST, SPEED_SLOW, SPEED_NORMAL   # 100 / 200 / 400 steps/s peak
view_names(), view_descriptions()   # camera views; the transit pose is excluded
view_checked(name), mark_checked(name)
status                           # dict polled by UIs: moving, view, phase, phase_index/count, progress, error, route, kind
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
`"direct": false` on a view (set on `home`) sends every move from or to it through the hub.
Joint values are raw encoder steps (0–4095, 4096 per turn) keyed by motor id. `ranges` is the calibration at capture time: after a recalibration the saved steps no longer mean the same pose, and moves to that view are refused until it is re-captured. `transit` is a normal entry but is not offered as a camera view.

## What a move does

1. **Preflight** (nothing is written if it fails): transit saved, Station not mirroring a leader onto the arm, fresh data from motors 1–8, no faults, right arm and calibration, arm holding, the arm is at a known place.
2. **Plan.** Between two checked views (or a checked view and the transit pose), at every speed: one segment `direct`, all seven joints along the straight joint-space line, if the geometry model shows no part of the arm dips below where it is at the end poses and the arm does not fold onto itself (`motion.direct_plan`). If only the straight line fails, the same line lifted towards the hub in the middle: `arc`. Otherwise, and always for unchecked views and for views with `"direct": false`, the route network: `rise` (M2, M3, M4, M6 together to the transit configuration) → `turn` (M1, M5, M7 to the target's values) → `descend` (M2, M3, M4, M6 together to the target). Segments that need no motion are skipped when they run.
3. **Run each segment**: smoothstep in joint space, goals streamed at 30 Hz; a waypoint is only sent if it is within a short leash (140–260 steps, by speed) of where the arm really is. Guards on every sample until the segment has settled; any fault holds the arm and the result says whether that hold was confirmed.
4. **Arrive**: every moved joint within 50 steps of its goal, standing still, with the goal read back, checked after each segment.

Targets are clamped 30 steps inside each joint's calibrated range. The gripper is never written.

`describe()["where"]` says where the arm is: `view`, `hub`, `spoke` (between a view and the hub), `between` (on a direct line or arc between `view` and `to`, after a Stop), `above` (a blended move stopped while turning, above the clear height) or `unknown`. A move can start from all but `unknown`.

## Speeds and blending

| Speed | Peak steps/s | How |
|---|---|---|
| first-run | 100 | forced whenever a view involved is not marked checked |
| slow / normal | 200 / 400 | streamed waypoints on a leash, strictly rise, then turn, then descend |
| fast | 800 | streamed; **blended** on the hub route |
| turbo | 2000 | rise, turn and a straight direct move that does not end lower than it starts run on the servos' own speed/acceleration profile (goal written once; progress watchdog instead of the leash); descents, other direct moves and arcs streamed at fast; **blended** on the hub route |

Blended (fast and turbo, checked views, desk assumed clear): the turn starts as soon as the geometry model puts the forearm, wrist, gripper and camera at least `CLEAR_HEIGHT_M` (25 cm) above the desk, and the descent starts when the rise is done but waits at that height until the turn has arrived. Below the line base yaw and forearm roll never move. The wrist roll runs alongside from start to end, and the turn is paced to fill the time it has. `BLEND = False` in `motion.py` switches blending off; the flags for direct moves and for each refinement are listed in [MOTION_OPT.md](MOTION_OPT.md).

## Trying it

```bash
python3 tests/test_motion_sim.py      # 129 checks on the simulated arm, in simulated time (4 s)
python3 tests/test_motion_opt.py      # direct moves, arcs, blending refinements: every pair at every speed (~75 s)
python3 tests/bench_moves.py          # how long every move takes on a realistic simulated arm (~30 s)
python3 tests/test_server_sim.py      # Stop / start / shutdown races in the web server
python3 fk.py                         # geometry model: heights per view and per route segment
python3 go.py --sim --where           # where is the (simulated) arm
python3 go.py --sim home top left     # run moves on the simulated arm
python3 capture_server.py --sim --port 8891   # the web UI against the simulated arm: /control
```

On the real arm (follower "(Self-controlled)" in Station, one person at the arm):

```bash
python3 go.py --where                 # moves nothing; location, blockers, per-joint sag/load/current
python3 go.py --engage                # limp -> holding (support the arm first)
python3 go.py wide                    # unchecked views run at first-run speed (100 steps/s)
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
| `sim_arm.py` | simulated 8-motor arm with fault injection; `SimArm(realistic=True)` lags like the real one |
| `fk.py` | geometry model, route height report |
| `capture_server.py`, `capture_ui.html`, `control_ui.html` | web UI: capture views (`/`) and move to them (`/control`) |
| `tests/` | `test_motion_sim.py`, `test_motion_opt.py`, `test_fk.py`, `bench_moves.py` (timing), `virtual_time.py` |
