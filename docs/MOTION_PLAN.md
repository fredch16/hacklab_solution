# Motion plan: getting to a saved view without breaking the arm

**Version 6** (after council round 3 and Sol's re-check). Implemented in `motion.py`, tested on the simulated arm only (`python3 tests/test_motion_sim.py`, 101 checks; `tests/test_server_sim.py`). **Nothing here has run on the real arm yet.**
Background: [HARDWARE.md](HARDWARE.md), [MOTION.md](MOTION.md).

## Goal

Click a view in the web UI → the follower arm (ElRobot, 7 joints + gripper, camera on the gripper) moves to that saved view. It must not hit the table or itself or pull the camera cable, and a person must be able to stop it instantly.

## Council review

| Reviewer | Round 1 (design v1) | Round 2 (built code, v3) | Round 3 (v4) | Final verdict |
|---|---|---|---|---|
| GPT-6.2 Sol | 3/10 | 4/10 | 5/10 | Re-check of v5: 6/10, still no (1 of 5 items fully fixed, 4 partly). Those gaps are fixed in v6; shipped without a further Sol round |
| Gemini 4 Neon | 5/10 | 6/10 | 8/10 | Yes, once the Stop-error message on the page was fixed (done) |
| Claude Fable 5.2 | 6/10 | 7/10 | 8/10 | Yes, no must-fix; conditions are in the test protocol |

Round 3 findings and what was done: Stop/abort always stops driven joints in place and confirms on fresh samples (tested with delayed register read-back); nothing is sent over the leash; skipped segments need two resting samples; held joints must stay near their planned place; a joint resting on its stop has not arrived; Go limp is confirmed by read-back; the mirroring state must be positively known; protocol step A made truly motionless; the server runs one tracked Stop cleanup and refuses clicks made before a Stop; a Stop that errors is shown as unconfirmed.

Round 2 asked: "is it safe and sensible to start the supervised test protocol with this code?" All three answered yes once their must-fix lists were done. Those lists and what was done:

| Round 2 finding | Decision in v4 |
|---|---|
| Stop, move start, torque, arm selection and shutdown can race in the web server (Sol, Gemini) | One command owner on the server's event loop; Stop bumps a generation counter so a start that was in flight can never run afterwards; race tests in `tests/test_server_sim.py` |
| Every healthy motor read as "not answering": generated readers are always truthy (Gemini) | Fixed in `station_arm.py` (checks the raw buffer); verified with synthetic frames |
| Mirroring check failed open on read errors and on an empty source (Gemini, Sol) | Only "queue/entry not found" means "never linked"; any other error or an undecodable entry refuses the move; empty source reads as "unknown" |
| `engage()` skipped the arm checks; `release()` wrote the gripper; `--bus auto` could pick the leader (Sol, Gemini) | `engage()` checks arm serial, motors 1–8, faults and mirroring, then confirms by read-back; `release()` touches joints 1–7 only; the server prefers the serial recorded in `views.json` and never guesses |
| Leash checked the previous goal, not the one about to be sent (Sol) | The waypoint about to be sent must be within the leash of every joint's measured position; late ticks are capped; leash is 60 / 80 / 120 steps at 100 / 200 / 400 steps/s |
| "At the hub" within 60 steps started turning; resting on a stop counted as arrived (Sol) | Every route states its full sequence: an exact rise even from the hub, a corrective descend when already at the target. Hand-off check between segments |
| Guards dropped while settling (Sol, Gemini) | One guard function runs from the first waypoint until the segment has settled; arrival needs two distinct fresh samples with the final goal read back |
| A servo already dead at connect looked fresh (Gemini) | Station's own per-motor read stamps are compared with each other |
| Views without serial/calibration metadata passed; invalid views could still identify the start (Sol, Gemini) | A view must carry its arm serial and calibration ranges and match the live arm, or it takes no part in anything |
| Hold failures only logged (Gemini, Sol) | `hold()` confirms by read-back; results and status carry `hold: confirmed / unconfirmed / limp`; the page shows an unconfirmed hold loudly |
| Stop at first-run speed wrote nothing, also when a joint was pushing (Claude) | Stop always pulls goals back to within 25 steps of the arm |
| `goal := position` on a loaded joint makes it sag a second time (Gemini) | `hold()` and `engage()` leave the goal of a joint that is already resting close to it |
| Re-teaching transit left views checked (Sol, Gemini) | Capturing `transit` un-checks every view |
| Two programs can command the arm (Claude, Sol) | A lock file: only one process moves the arm at a time. Use one controlling program per session |
| First run should be slower with tighter numbers (Sol) | `SPEED_FIRST` = 100 steps/s forced for unchecked views; held-joint drift limit 60 |

Kept provisional on purpose (to be set from real data, see protocol): arrival tolerance (50 steps), view/spoke recognition (60), current and load limits. Not adopted: dynamic torque limit, FK as a runtime gate, `CT_STOP_MIRROR` sent by us.

Round 1 decisions that still stand: three segments with the pitch joints always together (two reviewers' kinematic models and our own `fk.py` agree that shoulder-first collides at `home` and that pitch-together never goes below a route's own end poses); no shortcut between views; targets clamped 30 steps inside the calibrated range; torque must already be on; gripper never written; unchecked views run slowest.

## The route network

Joint groups: **PITCH** = M2, M3, M4, M6 (shoulder, elbow, pitch, wrist pitch), always together · **TURN** = M1, M5, M7 (base yaw, forearm roll, wrist roll), only at the hub · M8 gripper never moves.

The **hub** is the transit pose's pitch configuration (yaw/rolls anywhere). `transit` was taught by hand: arm raised, gripper pointing straight up (about 51 cm tall in the model).

| Segment | Joints | Goes to |
|---|---|---|
| rise | M2, M3, M4, M6 together | transit values → now at the hub |
| turn | M1, M5, M7 together | the target's values |
| descend | M2, M3, M4, M6 together | the target's values |

A **spoke** is the straight joint-space line between a view and the hub with the view's yaw/rolls. After a Stop the arm is on a spoke or at the hub and `locate()` recognises that from the measured joints alone, so the next click plans from there. Recognition tolerance (60 steps) only chooses the route; each route's first segment then pulls the joints onto the exact pose.

Safety argument: (1) the hub is safe for any yaw/roll the views use (chosen by a person; the models give ≥ 17 cm table clearance over the whole sweep); (2) each spoke lies between two poses a person taught, is watched once at first-run speed, then marked checked. Nothing else is ever driven.

## Preflight (a failure writes nothing)

Transit saved · Station not mirroring a leader onto the arm (and the state readable) · fresh data from every motor · exactly motors 1–8 · no fault bits or read errors · calibration sane · present position within range · transit and the target valid for this arm and this calibration · joints 1–7 holding · no joint being driven somewhere else · the arm is on the network.

`describe()` / `go.py --where` report all of these without moving, and say where the arm is even while it is limp.

## Guards while moving (each ends the move)

| Guard | Threshold |
|---|---|
| Leash: a waypoint is only sent within reach of the arm | 60 / 80 / 120 steps at 100 / 200 / 400 steps/s; not catching up for 0.4 s → abort |
| Held joint pushed/sagging | > 60 steps from where the segment started |
| Another writer | a held joint's goal register changes (immediate); a moving joint's goal is not one we sent in the last 0.6 s (for 0.25 s) |
| Joint went limp / motor dropped off the bus | immediate |
| Stale data | any motor not read for 0.3 s |
| Servo fault | any status bit except the voltage bit (immediate) |
| Stalled | load (PWM duty) > 800 for 0.25 s, any joint |
| Overcurrent | > 250 raw (≈1.6 A) for 0.25 s, any joint |
| Read errors | a motor's reads failing for 0.25 s |
| Write failure | Station doesn't accept a command within 0.5 s |
| Not arrived | a joint > 50 steps from its goal or still moving after 2 s |
| Stop / cancel | immediate |

After an abort the arm is held: goals more than 25 steps from the arm (suspected blockage or Stop) are pulled back to the measured position; a joint already resting next to its goal keeps it. The result says whether the hold was confirmed by read-back. Every move appends per-joint max lag, load and current and the max temperature to `motion_log.jsonl`.

## Test protocol on the real arm

One controlling program (the web server). Follower "(Self-controlled)" in Station, one person at the arm, desk cleared and padded, power plug in reach.

**A. Nothing moves** (leader arm stays unplugged for the whole session)
1. Restart the server with the follower named: `python3 capture_server.py --bus 5B79016329`. Open `/control`.
2. `python3 go.py --where` (it uses the arm the views belong to): calibration matches, per-joint numbers print, register dumps complete.
3. Mirroring check, without motion: Station still has the follower linked to the leader from the capture session, and with the leader unplugged nothing can drive it. `--where` must show the mirroring blocker now. Then set the follower to "(Self-controlled)" in Station: the blocker must clear. If it never showed, stop here: the check is not working. (Do not link a powered leader to test this: Station would drive the follower immediately.)
4. Saved poses still match the encoders: with the arm limp, place it in the `home` rest pose, then hold it at transit. The page must say "at home" / "at the hub", and the live picture at transit must match `views/transit.jpg`. (The calibration records of the first eight views were filled in afterwards, so this check stands in for them.)
5. Cable: with the arm limp at transit height, sweep the base and both rolls by hand through the range the views use; the camera cable must stay slack.

**B. Holding, not moving**
6. Hand-pose to `wide` (the page must say "at wide"), press Hold here. Read `--where`: goal error (sag), load, current, temperature at an extended pose.
7. Go limp (arm supported), hand-pose to transit with M1, M5 and M7 near their transit values (the page must say "at the hub"), Hold here. Read `--where` again. The sag decides what happens next: up to about 20 steps everything below should work as written; between 20 and 50, expect an "unknown pose" after a Stop mid-move (use Recover to transit, slowly and watched) and expect views saved on a joint stop to fail arrival; above 50, or with load/current near the limits, do not move: retune first. The arm is now holding at transit for step 8.

**C. First moves (first-run speed, 100 steps/s)**
8. From transit (where step 7 left it): go to `wide`. Then back to transit.
9. Stop test, high in the air: start `wide` again, press Stop mid-segment; the arm must hold. Press Stop three more times: no drift. Click a view: it must recover. If the page says "unknown pose" instead (the arm rests a little off the route because of sag), use Recover to transit and note the sag for tuning.
10. Block test: during a rise, hold a soft object (a cushion) in the path; expect an abort within about 1 s and no continued pushing.
11. `top`. Then `left`, `right` (after re-capturing them off the joint limits with a level horizon).
12. `close`, `side_low` (after re-capturing them at least 3 cm higher: the model puts them 0.2–1.6 cm above the table, and a replayed pose rests lower than taught by the gravity sag). `home` last.
13. Mark each view checked after a clean there-and-back run. One full tour, then normal speed.
14. Read `motion_log.jsonl`; set `ARRIVE_TOL` to measured sag + 15 and tune the lag/load/current limits.

## Known limits

- No collision model at runtime: safety rests on taught poses, joint ordering, supervision and guards.
- If the link to Station dies mid-move we cannot confirm the hold; the last goal is within the leash of the arm. Emergency stop is the power plug, and at transit the arm then falls from ~50 cm: prefer Stop.
- Arrival is accepted up to 50 steps short and nothing is monitored once a move has finished, so a view taught very close to the table can rest on it. Teach views with clearance.
- A Stop in the web page cannot stop a move started by `go.py` or `director.py` in another process; the lock file prevents two moves at once, not that. For the demo the director should call the server.
- The mirroring check reads the newest state Station recorded. If Station ever answers in a way we do not expect, the check refuses and may not clear; step 3 tests both directions before anything moves.
- Unproven until the first real session: the mirroring queue name on a live Station, read-back delay of goal registers (a delay above ~0.4 s would trip the "another writer" guard), static sag, whether a goal write energises a limp joint.
- First-run moves take 30–60 s; checked views at normal speed about 10–15 s.
