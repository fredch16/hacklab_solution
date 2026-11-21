# Faster moves: what changed, what it assumes, what is not verified

Branch `motion-opt`. **Everything here ran on the simulated arm only.** Nothing was run against Station or the real arm.
Usage and API: [MOTION.md](MOTION.md). Design background: [MOTION_PLAN.md](MOTION_PLAN.md).

## Result

Time for one move in simulated seconds, summed over all 56 moves (42 ordered view pairs plus every view to and from transit), on the simulated arm with a realistic response (see "The benchmark"). Every view is counted as checked.

| Speed | Before | After | Change | View to view only | Direct moves off (`DIRECT = False`) |
|---|---|---|---|---|---|
| normal (400) | 634 s | 365 s | −42% | 528 → 298 s | 634 s (unchanged) |
| fast (800) | 385 s | 209 s | −46% | 321 → 167 s | 306 s (−21%) |
| turbo (2000) | 245 s | 178 s | −27% | 205 → 144 s | 230 s (−6%) |

Average time per view-to-view move, by the route it now takes:

| Route now | Pairs | normal | fast | turbo |
|---|---|---|---|---|
| direct (straight line) | 14 | 10.6 → 3.7 s | 7.0 → 2.6 s | 4.6 → 2.2 s |
| arc (lifted line) | 16 | 13.6 → 5.3 s | 7.3 → 3.3 s | 4.6 → 3.3 s |
| hub (`home`) | 12 | 13.5 → 13.5 s | 8.8 → 6.4 s | 5.5 → 5.0 s |

Time with only one joint group moving while the other stands still fell from 276 s to 91 s at fast (base yaw / forearm roll turning on their own: 25 s to 3 s).

With the flags as they are in `views.json` today (only `home`, `right`, `top`, `wide` are checked) the 12 moves among those four go from 96 s to 55 s at fast. `left`, `close` and `side_low` stay on the hub route at first-run speed until they are marked checked.

## Route and time for every move

Times in seconds, before → after. Reproduce with `python3 tests/bench_moves.py --compare docs/bench/baseline.json`.

| Move | Route | normal | fast | turbo | Why this route |
|---|---|---|---|---|---|
| home → left | rise › turn › descend | 15.6 → 15.6 | 8.6 → 6.8 | 5.3 → 4.7 | `home` is opted out |
| home → right | rise › turn › descend | 15.5 → 15.5 | 8.6 → 6.6 | 5.3 → 4.7 | `home` is opted out |
| home → top | rise › turn › descend | 11.8 → 11.8 | 8.1 → 6.3 | 5.0 → 4.5 | `home` is opted out |
| home → wide | rise › turn › descend | 11.8 → 11.8 | 8.1 → 6.2 | 5.0 → 4.5 | `home` is opted out |
| home → close | rise › turn › descend | 10.8 → 10.8 | 7.1 → 5.8 | 4.6 → 4.0 | `home` is opted out |
| home → side_low | rise › turn › descend | 15.5 → 15.5 | 8.6 → 6.8 | 5.3 → 4.7 | `home` is opted out |
| left → home | rise › turn › descend | 15.6 → 15.6 | 9.8 → 6.7 | 5.9 → 5.6 | `home` is opted out |
| left → right | arc | 17.1 → 8.0 | 8.6 → 4.8 | 4.9 → 4.8 | straight line fails: jaw_tip to -5.0 cm, 14.5 cm below its end poses; lifted 40% |
| left → top | arc | 13.1 → 4.4 | 7.3 → 2.9 | 4.6 → 2.9 | straight line fails: jaw_tip to 8.1 cm, 3.7 cm below its end poses; lifted 20% |
| left → wide | direct | 13.1 → 5.0 | 7.3 → 3.4 | 4.6 → 2.1 | straight line passes |
| left → close | arc | 12.0 → 4.4 | 6.2 → 2.8 | 4.1 → 2.8 | straight line fails: camera to 4.3 cm, 2.5 cm below its end poses; lifted 20% |
| left → side_low | direct | 10.4 → 1.6 | 7.7 → 1.6 | 4.9 → 1.6 | straight line passes |
| right → home | rise › turn › descend | 15.5 → 15.5 | 9.8 → 6.7 | 5.9 → 5.6 | `home` is opted out |
| right → left | arc | 17.1 → 8.0 | 8.6 → 4.8 | 5.0 → 4.8 | straight line fails: jaw_tip to -5.0 cm, 14.5 cm below its end poses; lifted 40% |
| right → top | arc | 13.0 → 4.3 | 7.3 → 2.8 | 4.6 → 2.8 | straight line fails: jaw_tip to 5.2 cm, 3.2 cm below its end poses; lifted 20% |
| right → wide | direct | 12.8 → 5.0 | 7.3 → 3.4 | 4.6 → 2.1 | straight line passes |
| right → close | arc | 11.9 → 4.3 | 6.2 → 2.9 | 4.1 → 2.9 | straight line fails: Gripper_Base_v1_1 to 3.4 cm, 2.5 cm below its end poses; lifted 20% |
| right → side_low | arc | 17.0 → 7.9 | 8.7 → 4.8 | 5.0 → 4.8 | straight line fails: jaw_tip to -7.7 cm, 14.3 cm below its end poses; lifted 40% |
| top → home | rise › turn › descend | 11.8 → 11.8 | 9.4 → 6.2 | 5.8 → 5.4 | `home` is opted out |
| top → left | arc | 13.1 → 4.6 | 7.3 → 2.9 | 4.8 → 2.9 | straight line fails: jaw_tip to 8.1 cm, 3.7 cm below its end poses; lifted 20% |
| top → right | arc | 13.0 → 4.3 | 7.3 → 2.8 | 4.8 → 2.8 | straight line fails: jaw_tip to 5.2 cm, 3.2 cm below its end poses; lifted 20% |
| top → wide | direct | 8.3 → 3.5 | 6.9 → 2.4 | 4.6 → 1.8 | straight line passes |
| top → close | direct | 8.3 → 2.4 | 5.8 → 1.9 | 4.0 → 1.9 | straight line passes |
| top → side_low | arc | 13.0 → 4.5 | 7.3 → 2.9 | 4.8 → 2.9 | straight line fails: jaw_tip to 4.7 cm, 1.9 cm below its end poses; lifted 20% |
| wide → home | rise › turn › descend | 11.8 → 11.8 | 9.4 → 6.2 | 5.9 → 5.3 | `home` is opted out |
| wide → left | direct | 13.1 → 5.0 | 7.3 → 3.2 | 4.9 → 3.2 | straight line passes |
| wide → right | direct | 12.8 → 5.0 | 7.3 → 3.2 | 4.9 → 3.2 | straight line passes |
| wide → top | direct | 8.3 → 3.5 | 6.9 → 2.5 | 4.7 → 2.5 | straight line passes |
| wide → close | direct | 8.3 → 3.5 | 5.8 → 2.4 | 4.2 → 2.4 | straight line passes |
| wide → side_low | direct | 13.0 → 5.0 | 7.3 → 3.2 | 4.9 → 3.2 | straight line passes |
| close → home | rise › turn › descend | 10.8 → 10.8 | 8.9 → 5.9 | 5.6 → 5.3 | `home` is opted out |
| close → left | arc | 12.0 → 4.4 | 6.9 → 2.8 | 4.6 → 2.8 | straight line fails: camera to 4.3 cm, 2.5 cm below its end poses; lifted 20% |
| close → right | arc | 11.9 → 4.5 | 6.9 → 2.9 | 4.6 → 2.9 | straight line fails: Gripper_Base_v1_1 to 3.4 cm, 2.5 cm below its end poses; lifted 20% |
| close → top | direct | 8.3 → 2.4 | 6.4 → 1.9 | 4.4 → 1.6 | straight line passes |
| close → wide | direct | 8.3 → 3.5 | 6.4 → 2.5 | 4.4 → 1.8 | straight line passes |
| close → side_low | arc | 11.7 → 4.3 | 6.9 → 2.8 | 4.6 → 2.8 | straight line fails: Gripper_Gear_v1_1 to 2.6 cm, 2.0 cm below its end poses; lifted 20% |
| side_low → home | rise › turn › descend | 15.5 → 15.5 | 9.8 → 6.7 | 5.9 → 5.6 | `home` is opted out |
| side_low → left | direct | 10.3 → 1.6 | 7.7 → 1.6 | 4.9 → 1.2 | straight line passes |
| side_low → right | arc | 17.0 → 7.9 | 8.7 → 4.8 | 4.9 → 4.8 | straight line fails: jaw_tip to -7.7 cm, 14.3 cm below its end poses; lifted 40% |
| side_low → top | arc | 13.0 → 4.3 | 7.3 → 2.9 | 4.6 → 2.9 | straight line fails: jaw_tip to 4.7 cm, 1.9 cm below its end poses; lifted 20% |
| side_low → wide | direct | 13.0 → 5.0 | 7.3 → 3.4 | 4.6 → 2.1 | straight line passes |
| side_low → close | arc | 11.7 → 4.2 | 6.2 → 2.8 | 4.1 → 2.8 | straight line fails: Gripper_Gear_v1_1 to 2.6 cm, 2.0 cm below its end poses; lifted 20% |
| home → transit | rise › turn | 7.9 → 7.9 | 4.0 → 4.0 | 2.3 → 2.3 | `home` is opted out |
| left → transit | direct | 8.7 → 4.5 | 4.3 → 3.1 | 2.5 → 1.9 | straight line passes |
| right → transit | direct | 9.0 → 4.5 | 4.4 → 3.1 | 2.5 → 2.0 | straight line passes |
| top → transit | direct | 5.2 → 4.1 | 2.7 → 2.7 | 1.8 → 1.8 | straight line passes |
| wide → transit | direct | 5.2 → 4.1 | 2.7 → 2.7 | 1.9 → 1.9 | straight line passes |
| close → transit | direct | 4.2 → 3.0 | 2.3 → 2.3 | 1.6 → 1.6 | straight line passes |
| side_low → transit | direct | 8.8 → 4.5 | 4.3 → 3.1 | 2.5 → 1.9 | straight line passes |
| transit → home | rise › turn › descend | 9.0 → 9.0 | 7.8 → 4.9 | 4.4 → 4.1 | `home` is opted out |
| transit → left | direct | 9.9 → 4.5 | 5.7 → 3.0 | 3.9 → 3.0 | straight line passes |
| transit → right | direct | 10.1 → 4.5 | 5.7 → 3.0 | 3.9 → 3.0 | straight line passes |
| transit → top | direct | 6.4 → 4.1 | 5.3 → 2.7 | 3.1 → 2.7 | straight line passes |
| transit → wide | direct | 6.4 → 4.1 | 5.3 → 2.7 | 3.1 → 2.7 | straight line passes |
| transit → close | direct | 5.3 → 3.0 | 4.2 → 2.3 | 2.7 → 2.3 | straight line passes |
| transit → side_low | direct | 9.9 → 4.5 | 5.7 → 3.0 | 4.0 → 3.0 | straight line passes |

Raw tables: `docs/bench/baseline.txt` (before), `docs/bench/after.txt`, `docs/bench/hub_only.txt` (after, `DIRECT = False`).

## What each change does

All flags are module-level constants at the top of `motion.py`. All are on by default except `SHORT_HOPS`.

| Change | Flag | What it does | What it assumes |
|---|---|---|---|
| Direct moves | `DIRECT` | Between two checked views, at every speed, one segment `direct`: all seven joints along the straight joint-space line. Also view ↔ transit pose. | The geometry rule below; the desk is clear for moves between checked views (base yaw and forearm roll turn all the way along the line, also close to the desk). |
| Arcs | `ARC` | A pair whose straight line fails the rule takes the same line with the pitch joints bulging towards the hub in the middle, one segment `arc`. The smallest lift that passes, plus 10%. | Same rule, same model. The control dashboard keeps lift-over enabled. Pairs without a model-checked arc use transit. |
| Turbo direct | `DIRECT_TURBO_PROFILE` | In turbo a straight direct move that does not end lower than it starts (by the model) runs on the servos' own profile, like a rise. Moves that end lower, and all arcs, are streamed at fast on the leash, like a descent. | Per-joint speed and acceleration scaling keeps the servos on the line (true in the simulation, 46 steps off at worst). |
| Wrist roll on its own (2a) | `M7_FREE`, `M7_FREE_MAX` | Hub route, blended: M7 runs from the first moment to the end instead of with the gated turn. | M7 only spins the gripper and camera about their own axis. Checked with fk.py: no clear point moves except the camera, which moves 14 mm across all saved views and 33 mm at the 300-step limit. The height gate counts the lower of where the camera is and where it is going. Above 300 steps M7 stays in the gated turn. |
| Aligned turn (2b) | `ALIGN` | Hub route, blended: the turn (M1, M5) is slowed to fill the time between the rise clearing 25 cm and the descent coming back down to it. | Planned times. It saves no time; it removes racing and waiting. A turn the move is waiting for is never slowed. M1/M5 still never start below the clear height, and the descent never goes below it before they have arrived. |
| No standstill at the hub (2e) | `HUB_PASS_THROUGH` | Blended: the descent takes over as soon as the rise is within 50 steps of the hub with its goal read back. | Drops only "standing still" at the hub. Final arrival at a view is unchanged. The descent's leash covers the same joints from the next tick. About 0.3 s per hub move. |
| Descent at fast (2e) | `FAST_DESCENT` | At fast the descent runs at 800 steps/s instead of 400. | Turbo already does this on the real arm (three moves). The largest single gain on the hub route: 1.8 s per move. No guard changes. |
| Short hops (2e) | `SHORT_HOPS` (**off**) | A segment of a few steps takes 0.2 s or more, not 0.8 s. Worth about 0.1 s per hub move. | Only the minimum duration; peak speed of such a hop stays at or below slow speed. Off by default because `tests/test_server_sim.py` (not mine to change) counts on a 40-step correction at `home` still moving after 0.8 s and fails with it on. |
| Idle-joint guard | `BLEND_IDLE_GUARD` | Blended: joints that no segment is driving at the moment are checked for another writer and for being pushed away. | This is a new abort condition (same thresholds as a move in sequence). Blended moves had no such check before. |

Not built: the lower pass height (2d). Arcs cover its purpose for every pair that fails the straight line, in one move instead of three, so it had nothing left to gain except for `home`.

Other facts found on the way:
- A streamed move is paced by the leash, not by the requested speed: the servo follows at about 2.8 × its position error, so with the 260-step cap the arm does at most about 730 steps/s. Every segment then ends with roughly one second of settling. Direct moves win mostly by having one such tail instead of three.
- The `duration` field in the real log is planned time for sequential moves and wall time for blended ones; `took` is always wall time.
- The settle check needs two samples 0.15 s apart, so even a segment that is already there costs 0.3 s. Not changed.

## The rule for direct moves

`motion.direct_plan()` states it in full. A path passes when fk.py, at 48 poses along it and in both of its steps-to-angle mappings, shows:

1. **Desk.** No part of the arm (each key point and the lowest vertex of each link mesh) gets more than 1 cm lower than that same part is at the lower of the two end poses. For a part that ends within 2 cm of the desk the allowance is half its height. There is no absolute floor, because `close` and `side_low` themselves sit 1–3 cm up.
2. **Itself.** The arm never comes closer to itself than at its end poses less 1 cm, and never closer than 5 cm (`fk.self_clearance`: centre lines of links three or more joints apart).

Who may use a line: both views checked, neither opted out with `"direct": false` in `views.json`. `home` carries that flag.

About `home`: the model finds nothing wrong with the six straight lines out of `home` (no part dips, and the arm only opens up). They are refused twice anyway: by the flag, and by the 5 cm self-clearance floor, because `home` is folded to 3.3 cm centre-to-centre and the model has no self-collision meshes. To let `home` go direct you would remove the flag and set `DIRECT_SELF_CLEAR_M = 0`. I would not do that without watching each line slowly.

Rejected as straight lines, with the model's numbers (tests prove each): left ↔ right (jaw tip 5.0 cm under the desk), right ↔ side_low (7.7 cm under), right ↔ top (jaw tip 3.2 cm below its end poses), right ↔ close, close ↔ side_low, and also left ↔ top, left ↔ close, top ↔ side_low, which a whole-arm comparison lets through but the per-part comparison does not.

## Stop, and where the arm is afterwards

`locate()` has two new answers, both only when the geometry model is available:

- `{"where": "between", "view": a, "to": b, "s": 0..1}`: on an allowed direct line or arc, within 60 steps. The next move continues along that line to either end; for any other target it first finishes to the nearer end, then plans from there. Close to a view several lines run side by side; the one last driven or naming the target is preferred.
- `{"where": "above", "view": v}`: pitch joints on a view's line to the hub, yaw and forearm roll anywhere, and the model puts the arm at 23 cm or more. This is where a Stop leaves a blended move while the turn overlaps. The next move rises to the hub first. Before this branch that pose was "unknown".

A spoke is now also recognised with the wrist roll up to 360 steps from the view's value; going back down that spoke puts the roll right first.

Tested: Stop during a direct move, an arc, a turbo direct move and at seven moments of blended moves at fast and turbo. In every case the hold is confirmed, the arm rests, `describe()` recognises the pose, and the next move arrives. Where it is not recognised: with `DIRECT = False` a pose between two views is "unknown" and needs Recover to transit, as before.

## Not verified, because it only ran in simulation

- **The geometry model is the only thing vouching for direct lines and arcs.** It was never measured against the real arm. Its own notes say shoulder angles can be several degrees off and that the real gripper may be 2–3 cm lower. No direct line or arc has been watched on the real arm.
- **Arcs** have the thinnest margin: left ↔ right and right ↔ side_low would go under the desk as straight lines and pass with a 40% lift.
- **Turbo direct on the servo profile.** The real servos' profiles may not keep seven joints on one line. A Stop brakes each joint separately.
- **Self-collision** is not modelled beyond the centre-line distance.
- **The simulated response** is a first-order lag with gain 2.8/s, an acceleration limit and 20 steps of shoulder sag. The three moves I compared with the real log were within 30% (see "The benchmark"), but it has no gravity, no load, no overshoot and no current. `FAST_DESCENT` and turbo direct could overshoot on the real arm; the simulation cannot show that.
- **The idle-joint guard** could abort a real blended move if a held joint is shaken more than 60 steps (100 in turbo).
- **Planning a direct line blocks the event loop**: about 0.5 s the first time in a process (the link meshes are loaded), 0.02–0.1 s per new pair after that, nothing once a pair is cached. On the real arm the task that receives joint data cannot run meanwhile, so a move waits up to 1 s for fresh data after planning, before it commands anything (`_fresh_again`). The simulation cannot show whether that is enough; if the first click on a new pair is refused with "joint data is stale", click again. A Stop pressed during that half second is handled after it.
- Unchecked views were not changed and were tested only for that.

## If something looks wrong on the real arm

| Symptom | Switch off |
|---|---|
| Any doubt about a direct move or arc | `DIRECT = False`: every move goes through the hub again |
| Arcs come too close to the desk | Turn off **Go straight from view to view** in Motion settings to route through transit |
| Sideways sweeps close to the desk are not wanted | `DIRECT_TURN_TOL = 60`: direct only between views with the same heading |
| One view misbehaves | `"direct": false` on that view in `views.json` |
| Turbo direct leaves the line or overshoots | `DIRECT_TURBO_PROFILE = False` |
| Overshoot coming down onto a view at fast | `FAST_DESCENT = False`, and `DIRECT_SPEED_CAP = SPEED_NORMAL` for direct moves |
| A blended move aborts with "pushed off its position" or "something else is commanding" | `BLEND_IDLE_GUARD = False` |
| Jerk at the hub | `HUB_PASS_THROUGH = False` |
| Camera cable or wrist looks wrong early in a move | `M7_FREE = False` |

With `DIRECT`, `M7_FREE`, `ALIGN`, `HUB_PASS_THROUGH`, `FAST_DESCENT` and `BLEND_IDLE_GUARD` all off, the code plans and runs moves as before this branch. One thing stays: a blended move stopped while its turn overlaps is recognised as `above` instead of "unknown".

## Suggested order on the real arm

One person at the arm, hand on Stop, desk clear.

1. `python3 go.py --where` and the control page still report the arm correctly (nothing moves).
2. top → wide and back at slow speed (200). It is the gentlest direct line: same heading, the arm goes up. Then at normal, then fast.
3. Stop in the middle of top → wide. `python3 go.py --where` must print `between` with both views; the control page words it as "not at a known pose" until it is updated (see "For the other files") but keeps the view buttons enabled. Click either view.
4. right → wide and back, slow first. First direct line that turns base yaw and forearm roll close to the desk.
5. right → top (an arc), slow first, watching the gripper against the desk in the middle.
6. wide → transit and transit → right (direct to and from the transit pose).
7. home → top at fast: the hub route with the new blending (wrist roll from the start, no stop at the hub, descent at fast).
8. Turbo: top → wide (servo profile), then wide → top (streamed).
9. Only after `left`, `close` and `side_low` are re-captured and checked: their direct lines, each slowly first. left ↔ right and right ↔ side_low last.

## For the other files

I did not edit `capture_server.py`, `control_ui.html`, `capture_ui.html`, `tests/test_server_sim.py` or `mcp_server.py`. They work unchanged, with these gaps:

- `describe()`, `go_to_view()` results and `motion.status` have a new key `kind`: `"direct"`, `"arc"`, `"hub"` or `None`. `motion.route_kind(start, target)` gives `(kind, reason)` from `views.json` alone. `"arc"` is a third value next to the two that were asked for.
- `where` can now be `"between"` (with `view`, `to`, `s`) or `"above"` (with `view`). `control_ui.html` shows both as "not at a known pose" without the warning, and moves stay enabled. It should say "between A and B" and "above the desk near V".
- `status["phase"]` can contain `roll` (for example `rise+roll`), which is not a route segment. Routes can be `["direct"]`, `["arc"]`, or a line followed by more segments after a Stop.
- A view's `"direct": false` survives a re-capture (`capture_view.py`). There is no button for it.
- `sim_arm.py` changed on main as well (`stop_mirroring`); my changes there are additions to `__init__`, `_advance` and `write_goals` and may need a manual merge.

## The benchmark and simulated time

`python3 tests/bench_moves.py` runs every ordered pair at normal, fast and turbo and prints time, time with one joint group moving alone, time with nothing moving, the lowest height at which M1 or M5 was moving, and how far the arm dipped below its end poses. `--set NAME=VALUE` sets a `motion.py` flag for one run; `--compare FILE` shows before → after.

It uses `SimArm(realistic=True)`: speed proportional to the distance from the goal (gain 2.8/s), an acceleration limit from the acceleration register, and 20 steps of shoulder sag. Off by default, so other users of the simulated arm are unaffected.

Nothing runs in real time. `tests/virtual_time.py` provides an event loop whose clock jumps to the next timer whenever every task is waiting. `motion.py` and `sim_arm.py` each read one module-level clock, `_now`, which is `time.monotonic` unless that helper points it at the simulated clock for the length of a run. The full benchmark takes about 30 s of computing, `tests/test_motion_sim.py` 4 s (it took 75 s), `tests/test_motion_opt.py` about 75 s, almost all of it the geometry model. `MOTION_TEST_REALTIME=1` runs either suite on the real clock; `bench_moves.py --realtime` does the same for the benchmark.

Simulated against real clock, same moves (`bench_moves.py --check-clock`): top → wide 2.43 s against 2.41 s, right → top 2.80 against 2.82, home → left 6.77 against 6.77, wide → transit 2.70 against 2.71. Largest difference 0.8%.

Simulated arm before this branch against the real arm's log, same moves: fast right → top 7.3 s against 6.5 s; turbo top → transit 1.8 s against 2.0 s; fast transit → top 5.3 s against 4.0–5.2 s.
