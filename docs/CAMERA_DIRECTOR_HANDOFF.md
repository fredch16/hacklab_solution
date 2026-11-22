# Camera director: agent handoff (22–23 Nov 2026)

Branch **`beta/ai-camera-director`**, created from `main` at `8d06da1` (main untouched, nothing pushed). 20 commits, newest first in `git log --oneline main..HEAD`. One agent did all the work, no subagents. The owner was present for every real-arm move and gave full permission to test.

Read in this order: this file → [camera director guide](CAMERA_DIRECTOR.md) (controls, approval file, scoring) → [implementation map](IMPLEMENTATION_MAP.md) (where things live, safety gate) → [evidence](evidence/camera-director-2026-11-22.md) → [demo spec](AI_CAMERA_DIRECTOR_DEMO_SPEC.md) and [plan](superpowers/plans/2026-11-22-ai-camera-director-demo.md) (the original brief) → repo [AGENTS.md](../AGENTS.md) and [AGENT_HANDOFF.md](AGENT_HANDOFF.md) (baseline system).

## What the owner wants (in their words, condensed)

- A **director, not a search**: the arm must not sweep around to find views while recording. Estimate first, move on purpose; footage must stay usable.
- **Any object** marked in the dashboard (the Hack Lab sticker is only an example).
- **Free, cinematic camera motion** ("not fixed movement scripts"; arcs, cross, left/right, top/bottom; "the third recording arm for YouTubers"), smooth, the marked object always roughly in frame.
- **Claude only as planner/orchestrator**, never the real-time controller; real-time = local computer vision.
- **No per-object measurements**: estimate from the robot's own data (joints, calibration, images).
- **No servo EEPROM changes** if avoidable. When the shoulder nears its limit: **stop and lock there** as the boundary, not a full retreat.
- Approximate is fine; make it work.

## Hardware facts learned tonight (important)

| Fact | Evidence |
|---|---|
| Arm is the **NormaCore ElRobot** follower (7+1 DOF, 8× ST3215 12 V/30 kg, 12 V/4 A PSU), bus `5B79016329` | norma-core `hardware/elrobot/README.md`, Station log |
| Gripper camera queue on this laptop: `…/usbvideo/dc84eab2202fb0ae9704788db7d9f9a4` (jaws visible at the bottom), 1280×720 | verified by eye; added to `GRIPPER_CAMERAS` in `camera_director/jobs.py` |
| **Every servo**: overload torque **25 %**, protection time **2 s**, protective torque 20 %, max torque / torque limit **50 %** | read-only register dump (Station state bytes 0x10–0x30) |
| These are **norma-core Station defaults**, written to all motors at **every calibration freeze** (`software/drivers/st3215/src/presets.rs`, `port.rs` FreezeCalibration), not LeRobot values | norma-core source on GitHub |
| The **shoulder (motor 2)** trips that protection (status 0x20) when the arm reaches out: 5 trips tonight during computed approaches (load 28–32 %), even with the gripper closed and at first-run speed. Views and their checked lines never tripped it. | motion log, telemetry during runs |
| Station must be started with the `dialout` group. The Claude Code session lacked it: `cd ~/station && sg dialout -c "./station --web --tcp"` | `/dev/ttyACM0` permission denied otherwise |
| The model's camera pose is wrong at some poses: `home` vs `top` disagreed by 28 cm; at `top` the image's up/down is **inverted** in the model (sticker moved away from the base was localized closer) | real dry runs, owner's tape measure (31 cm ahead, centred) |
| Gripper was open (motor 8 at 2815) for much of the night; the owner closed it (1258). Open jaws hang low and fail desk-clearance checks. | telemetry |

## What works on the real arm (verified)

- Box selection on the dashboard (frozen frame pinned until confirmed), sticker crop saved, auto-find, live tracking.
- **Optimize all views** (estimate → one checking move per worthwhile preset): `top`, `wide` (32 % toward top), `home` verified.
- **Cinematic** between verified shots, ending at home; glides along checked lines, cuts (recording paused) where no glide keeps the object in frame.
- **Gimbal take** (newest working camera move): shoulder and elbow parked at `top`, base yaw ±200 steps, wrist joints keep the framing; one 13 s move, sticker in frame 77 % of sampled frames, shoulder peak load 14 %, no faults, back on `top`.
- **Snap** back onto a nearby view (144 steps) in 2 s instead of the transit lift.
- **Director Show** (film grammar, the best demo): establishing hold at `wide` → push-in along the checked wide–top line → gimbal arc at `top` (yaw −325..+350 steps, sticker in frame 77 %) → hold → pull-out to `wide` → closing hold. 66 s end to end, no faults, ends on `wide`. The sticker is too small to recognise from `wide` at its current spot, so the wide holds report `steady: false`. Motor 2 briefly touched load 284 during a move without tripping: close to the limit, watch it.
- Shoulder-load guard (23 % for 1.2 s) caught real overload build-ups and locked; easing back worked; but a stop at a heavy pose can still trip on the next move (see open issues).

## Dashboard buttons and what each does on the real arm (23 Nov)

| Button | Status on the real arm |
|---|---|
| Select object / Auto-find sticker | works; far objects (from `wide`) found via the full-resolution retry |
| Keep in frame | preview only (computed centring correction, never moves) |
| Keep in view | simulator-tested; on the real arm it cuts between verified shots only |
| Optimize all views | works (estimate, then one checking move per worthwhile view) |
| **Director Show** | works: the recommended demo (66 s, no faults) |
| Gimbal take | works (shoulder and elbow parked) |
| Scene | with `computed_paths: false` it films a Director Show instead of refusing; free-camera scenes need `computed_paths: true` and overload the shoulder |
| Return | for free-camera scenes only (needs `computed_paths`) |
| Snap to view | works (short move back onto a view within 200 steps) |
| Self-calibrate | built and simulator-tested; run it on the real arm next (see open issues) |
| Cinematic / Director Tour | work (glides and cuts between verified shots) |
| STOP | always works; cancels any job and holds |

## What is built but disabled or unverified on the real arm

- **Scenes** (free camera: keyframes in azimuth/elevation/distance, IK + monotone-cubic paths, approach/look/re-aim, return): pass all simulator tests; **disabled on the real arm** (`computed_paths: false` in `director_safety.json`) because approaches overload the shoulder.
- **Self-calibration** (`selfcal` job, `camera_director/selfcal.py`): targetless bundle adjustment of camera tilt/offset + joint zero offsets from views of any static object; synthetic test passes (new object within 0.4 cm), simulator job passes; **not yet run on the real arm**. This is the fix for the model errors above and replaces tape measurements.
- **Claude planner** for scenes and cinematic order (`planner: "claude"`): implemented with structured output and local validation; never exercised with a real key tonight.
- **Keep in view** (cut away from a covering hand) and MCP tools `director_job` / `director_status`: simulator-tested only.
- Faster detection (search near the last box, FLANN, feature cap): measured 70 → 24 ms on a real frame; running live since `7332ef3` (the Director Show used it).

## Current machine state (when this was written)

- Services running: Station (`sg dialout`, :8889/:8888), `capture_server.py --bus 5B79016329 --director` (:8890, needs a restart to load commits after `adfee2c`), `mcp_server.py` (:8200), `voice_bridge.py` (:8210), `laptop_dashboard.py` (:8300).
- Arm: at `top`, holding, no faults, gripper closed (1258).
- Local, ignored files (not in git): `director_safety.json` (PHYSICAL approval, lines `right-top`, `right-wide`, `top-wide`, max fraction 0.32, `computed_paths: false`); `director_data/` (sticker crop, `camera_calibration.json` with per-preset offsets `top`/`home`/`wide` and learned `shoulder_reach_limit_m` 0.308, `trail.json`, thumbnails). The per-preset offsets were measured with the sticker at 31 cm; the sticker has since been moved away. After a successful real self-calibration they are ignored (`model` present).

## Tests (22–23 Nov, all passing at `e9028cb` or just before)

Director 32+ tests, motion sim 129 checks, motion opt 1716 checks, MCP sim, framing 28 checks, fk, laptop gateway 7, frontend 34.

## Code map (new or changed)

| File | What |
|---|---|
| `camera_director/vision.py` | SIFT + RANSAC homography, template fallback (≥ 0.90), tracker, settled-view score, ROI-first detection, worker entry points `look` / `look_many`; frames downscaled to 960/640 px; OpenCV single-threaded |
| `camera_director/jobs.py` | `Director`: frame cache, selection, autofind, gate, jobs (`survey`, `recall`, `cinematic`, `tour`, `center`, `watch`, `calibrate`, `scene`, `return`, `snap`, `gimbal`, `selfcal`), vision worker process, trail persistence, shoulder-limit lock |
| `camera_director/predict.py` | localization (desk plane, views weighted by angle, triangulation as a check), view prediction, per-preset offsets, seamless-transition planning |
| `camera_director/orbit.py` | IK camera goals, keyframe paths, reach map, `plan_shot` (safe variants), gimbal aim and `gimbal_shot`, shoulder reach |
| `camera_director/selfcal.py` | targetless self-calibration fit; `call` applies a calibration in the worker |
| `camera_director/sim_camera.py` | rendered SIMULATED camera with a hand occluder |
| `motion.py` | `go_to_view(along=)` stops on checked lines (also from the far end); `go_through` computed paths (monotone cubic, `path_problem` checks, shoulder-load guard `PATH_TRIP_GUARD`, `LoadGuardError`) |
| `framing.py` | `solve_pose(limits=)`; `CAMERA_CAL` self-calibration hook in `camera_pose` |
| `capture_server.py` | `--director`, director API, STOP/manual preempt, `start_path` (first-run speed), sim frames on `/stream` |
| `laptop_dashboard.py` | `/laptop/director/*` relay |
| `dashboard/apps/dashboard/src/director.ts` | panel: select box, controls, cards, storyboard, scene results, phase-based recording |
| `mcp_server.py` | `director_job`, `director_status` |
| `tests/test_camera_director.py` | ~30 tests: vision, prediction, computed paths (no overshoot, desk, jumps), gate, simulator jobs (survey, recall, cinematic, STOP, manual takeover, watch with a hand, scene, return, guard lock, snap, self-calibration) |

## Open issues and next steps (priority order)

1. **Run `selfcal` on the real arm** (object visible from `top`/`wide`/`home`; moves only between views and on approved lines). Check `rms_after`, then that a moved object localizes sensibly without measurements. If the fit is poor, add more views (more line fractions) before trusting it.
2. With a good calibration, retry **gimbal takes** with centred framing and wider yaw, then decide on scenes.
3. **Shoulder limit**: the reliable fixes are outside software: raise motor 2's overload threshold (register 0x24; norma-core rewrites it at each calibration freeze, so the durable fix is upstream) or reduce its load (counterweight/spring, stronger servo such as STS3250). The owner preferred not to change EEPROM. Gimbal mode stays within the limit.
4. Known gaps: a guard lock at a heavy pose can still trip on the following move; the reach map takes ~73 s; scene/IK planning takes 5–30 s; the wide holds of the show cannot see a small object far away (recognition needs ~40 px).
5b. **Detection** is SIFT + homography against the operator's crop (works for any marked object, no training). Far objects: frames are downscaled to 960/640 px for speed, which made the sticker unrecognisable from `wide`; a full-resolution retry (1280×720) finds it (12 inliers, ~250 ms) and is used automatically when the fast search fails (`vision.look`, `look_many`). Stock YOLO does not fit "track what the user boxed" (fixed classes); candidates if SIFT is not enough: open-vocabulary detectors (YOLO-World, OWL-ViT) or OpenCV's learned single-object trackers (TrackerVit / TrackerNano, ONNX model files), run in the vision worker.
6. Director craft to build on (researched: establish wide → closer, slow eased push-ins, arcs around a stationary subject, steady 2–4 s holds as edit points, move only with a reason, keep the subject's framing consistent). Ideas within the shoulder limit: gimbal takes from `wide` (lighter shoulder, smaller parallax), dutch roll in gimbal takes (`roll`), rule-of-thirds framing instead of centring (aim offset), a reveal (start aimed beside the object, pan onto it).
5. Commit `docs/evidence/camera-director-2026-11-22.md` updates for any new real runs; keep `director_safety.json` truthful.

## Commands

```bash
# start (Station needs the dialout group)
cd ~/station && sg dialout -c "./station --web --tcp"
.venv/bin/python capture_server.py --bus 5B79016329 --director
.venv/bin/python mcp_server.py; .venv/bin/python voice_bridge.py; .venv/bin/python laptop_dashboard.py

# director jobs (same as the dashboard buttons)
curl -X POST -H 'Content-Type: application/json' http://127.0.0.1:8890/api/director/job -d '{"kind":"selfcal"}'
curl -X POST -H 'Content-Type: application/json' http://127.0.0.1:8890/api/director/job -d '{"kind":"gimbal","yaw":500}'
curl -X POST -H 'Content-Type: application/json' http://127.0.0.1:8890/api/director/job -d '{"kind":"snap"}'
curl http://127.0.0.1:8890/api/director/state
curl -X POST -H 'Content-Type: application/json' http://127.0.0.1:8890/api/stop -d '{}'

# tests
.venv/bin/python -m unittest tests/test_camera_director.py
.venv/bin/python tests/test_motion_sim.py; .venv/bin/python tests/test_mcp_sim.py; .venv/bin/python tests/test_motion_opt.py
(cd dashboard/apps/dashboard && npm run build && npm test)
```

## Recovery when motor 2 latches (status 0x20)

The motion layer refuses every move. Owner: support the arm, Arm controls → **Go limp** → **Hold here**. Then near a view: `snap`; otherwise Arm controls **Recover** (lift to transit, first-run speed). Before restarting a service, check nothing is moving (`/api/state`).
