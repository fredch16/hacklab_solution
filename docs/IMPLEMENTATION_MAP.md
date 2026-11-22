# Camera director: implementation map and safety gate

Audit of the real repository for the [camera director spec](AI_CAMERA_DIRECTOR_DEMO_SPEC.md), branch `beta/ai-camera-director`, created from `main` at `8d06da1` (22 Nov 2026). This maps each responsibility from the [plan](superpowers/plans/2026-11-22-ai-camera-director-demo.md) to existing code. Nothing here duplicates a service.

## Where things really are

| Responsibility | Actual code | Notes |
|---|---|---|
| Robot camera producer | `camera.py` `Camera`, owned by `capture_server.py` `State.cams` | Station owns the USB camera. The capture server follows Station's `usbvideo/<md5>` queue and keeps the latest JPEG (`cam.jpeg`, `cam.stamp` monotonic). Station stores 960×720 frames on this laptop. |
| Moving (wrist) camera identity | Station queue `usbvideo/74e4d17bc903e40ebc78117539fca31f` ([hardware guide](HARDWARE.md)) | Camera sits on the follower's gripper and rolls with motor 7. The jaws are visible at the bottom of the frame. There is no fixed external robot camera; the Pixel is the only other camera. |
| Feed to the browser | `capture_server.py /stream` (MJPEG) → `laptop_dashboard.py /laptop/robot/events/{tab}` (WebSocket blobs) → `<img>` in `dashboard/apps/dashboard/src/laptop.ts` `robotCard()` | Not WebRTC. The browser shows the exact JPEG the server holds, so a box on the image maps to the source by `object-fit` geometry alone. |
| Presets | `views.json` via `motion.load_views()` / `view_names()` / `view_checked()` | `home`, `left`, `right`, `top`, `wide` are checked; `close`, `side_low` are not; `transit` is the hub pose, not a view. The spec's "topview" is `top`. |
| Approved transitions | `motion.plan_route()`, `motion._Links`, `motion.direct_plan()`, `motion.route_kind()` | Between checked views: one direct line or a lifted arc ("lift over") when `fk.py` shows no dip and no self-approach; otherwise rise → turn → descend through `transit`. |
| Measured joints and telemetry | `station_arm.Arm.joints(max_age_s)`, `telemetry()` | Raw encoder steps 0–4095 by motor id 1–8; per-motor freshness, load, current, status, goal read-back. `motion.STALE_S = 0.3` s. |
| Motion owner, STOP, lease | `capture_server.State.command()`, `start_move()`, `stop()`, stop generation `gen` | One command at a time; STOP never queues, bumps `gen`, cancels the move and holds; any command carrying an older `gen` is refused. `motion.go_to_view` also takes a file lock so no second process can move the arm. |
| Guards during a move | `motion._run_segment` / `_guard` | Leash on streamed waypoints, stall, drift, load, current, temperature, foreign goal writes, stale data → hold with confirmation. |
| Geometry model | `fk.py` (vendored ElRobot URDF), `framing.py` (camera ray model, DLS IK, dry-run only) | Advisory: signs of M1, M2, M5, M8 checked against snapshots; offsets and camera stalk/cables not modelled; camera offset/tilt and HFOV are fitted guesses. |
| Robot clip recording | `laptop_dashboard.py` `start_robot` / `end_robot`, tab lease | Clips under ignored `Camera recordings/`; tab must stay visible and live. |
| MCP | `mcp_server.py` (fixed views, `best_view`, `preview_frame`) | Calls the capture server over HTTP with the stop generation. |
| Prior attempts | `framing.py` + `POST /api/frame` (dry-run framing preview); `origin/keep-in-view` branch (`watch.py`, never run, Claude vision judge) | The parked branch's agreed rule: no free nudging to unchecked poses; positions on the lines between checked views are acceptable. Its Claude-based judge is not used here (spec: local vision only). |

New code lives in `camera_director/` (vision, scoring, jobs) and is wired into `capture_server.py` behind `--director` (off by default) and into `laptop_dashboard.py` / `robotCard()`.

External references (norma-core, Robotics_berlin_hack, norma-robot) were not copied. This repo already has its own calibration, URDF model and guarded motion layer; no joint values are taken from other projects.

## Candidate viewpoints and how the director uses them

A candidate near preset `P` is either `P` itself or the pose a fraction `s` (8–32%) of the way along a verified direct line or arc from `P` to another checked view `Q`. These poses are on paths that `motion.direct_plan()` already approves and that the arm sweeps whenever it moves `P → Q`; a STOP during that move leaves the arm at exactly such a pose, and `motion.locate()` / `plan_route()` already continue from there (`where: between`). No new joint arrays are invented.

The director does not visit candidates to compare them. It localizes the marked object from settled sightings (measured servo steps + Station's live calibration → `fk.py` camera pose → ray; triangulated across views), predicts every candidate (`camera_director/predict.py`), and moves once per preset to the predicted best, where the camera verifies it. See [camera director](CAMERA_DIRECTOR.md).

Lines among the checked views today (`motion.route_kind`, from `views.json` alone):

| From | To | Kind |
|---|---|---|
| left | right | arc (40% lift) |
| left | top | arc (20%) |
| left | wide | direct |
| right | top | arc (20%) |
| right | wide | direct |
| top | wide | direct |
| home | any | hub only (`"direct": false`) |
| close, side_low | any | hub only (not checked) |

So `left`, `right`, `top` and `wide` each have three lines for candidates; `home` gets its baseline only; `close` and `side_low` are reported `BLOCKED` (not checked on the real arm).

## Physical safety gate — status: BLOCKED (pending owner sign-off)

| Requirement | Evidence in repo | Status |
|---|---|---|
| Moving camera identified | Hardware guide names the gripper camera queue | Needs the live queue id compared on this laptop |
| Measured joint feedback, freshness | `station_arm` per-motor stamps, `motion.STALE_S` | In code |
| STOP preempts, stale commands refused | `State.stop()`, stop generation, tests in `test_server_sim.py` | In code, simulator-tested |
| Overload / stall / foreign writer → hold | `motion._guard` | In code, limits provisional |
| Approved routes between checked views | `direct_plan` checks desk dip and self-approach in two mappings | Model only; direct to `left`/`top` and arc to `right` ran on the real arm (`motion_log.jsonl`) |
| Swept clearance of camera stalk and cables | Not modelled anywhere | **Unverified: owner must confirm** |
| Desk clear along the lines used for candidates | `direct_plan` assumes a clear desk | **Owner must confirm for the demo layout** |
| Moves to computed poses (visual servoing) | `framing.py` is preview-only by team decision | **Not enabled** |

Until the owner writes the local, ignored approval file `director_safety.json`, the director runs dry-run on the real arm: it detects, tracks, scores the current view and lists the candidate plan, but moves nothing. In the simulator (`capture_server.py --sim --director`) the same jobs run end to end on the simulated arm with a rendered camera and are labelled `SIMULATED`.
