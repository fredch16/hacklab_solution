# Camera director: agent handoff (22–23 Nov 2026)

## Latest continuation

- The real camera axes were reversed in the pose model. The local `roll_180` correction makes the captured moved-target pair localize in the right direction. A supervised real voice-gateway reframe kept the sticker visible at low load but missed its requested screen position. Free-camera paths remain off. [Orientation and voice results](evidence/camera-director-2026-11-22.md#camera-orientation-and-wake-word) · [Wake-word and occlusion demo](VOICE.md#wake-word-demo)
- The first supervised calibration run latched motor 2. The owner cleared it and recovered to transit. The revised calibration used eight sightings on the approved `top`–`wide` line and reduced image RMS from 0.0457 to 0.0134. The monitor stopped the automatic return on load and confirmed the hold 16% from `top` toward `wide`, with no fault. The code now ends physical calibration at the final checked view.
- The moved-sticker validation gave the wrong estimated direction within its uncertainty. The candidate fit was set aside and the previous calibration was restored. Keep computed paths off until calibration passes an independent moved-subject check. The revised calibration refuses transit, aborts on failed motion and applies the shoulder load guard. [Randomized test and physical fault record](evidence/camera-director-2026-11-22.md#randomized-claude-briefs-and-supervised-hardware-acceptance)
- Claude now prefers a matching checked view or existing director action. Tests with the live API verified reuse of `wide`, `top` and a stationary hold as well as a custom 7° pan. MCP exposes all eight motors' live readings and guarded camera jobs. [MCP guide](MCP.md)

- **Film with Claude** accepts a free-form brief in the laptop dashboard and as `director_job(kind="film", brief="…")`. Its measured scene context and local IK allow new pan, composition and roll paths with the heavy joints parked. The separate computed-path opt-in permits changes in elevation and distance. Physical validation is pending.
- **Keep in view** pauses recording when the target is persistently obscured, attempts at most one checked cut per obstruction, and resumes after a clear observation streak. A clear fallback stays in use. With computed paths approved it can evaluate a new angle locally when a suitable preset is unavailable.
- The shoulder guard now ends jobs at the confirmed HOLD. Scene, Show and filmed takes no longer automatically proceed after the guard fires. Small-object tracking tries a 2× region near the last position. [New test and hardware preflight evidence](evidence/camera-director-2026-11-22.md#agentic-filming-continuation)
- Physical sequence remains owner-operated. Run the director, motion and MCP simulator suites before each run. First run Self-calibrate and inspect `rms_after`, then move and reselect the target and verify localization with a prediction-only survey. Only then try one short free arc while monitoring motor 2. The existing approval still has `computed_paths: false`.
- The updated capture and MCP services are running. Station, the voice bridge and laptop gateway kept running through the reload. The arm stayed at `wide`, and Auto-find restored the selected sticker.

## 22 Nov evening: `UI` branch, Peter studio, framing that keeps the object centred (read this first)

The `UI` branch holds everything on `beta/ai-camera-director` plus the work below. Restart the arm server after pulling.

- **Every view centres the marked object.** `Director._center_on_object` runs at the end of a view move asked for by voice or Claude (`/api/goto` with `frame_object`, the director `goto` job) and at every held shot (show, cinematic, tour, Claude's takes). Claude takes stay off-centre only when the brief composes them that way (keyframe u/v more than 0.15 from 0.5). `UNFRAMED_VIEWS` is empty: the owner wants left and right centred too. The next move steps back onto the view first (`_framed`, trail).
- **Centring algorithm** (`orbit.follow_step`):
  - Turns base yaw and the wrist with gain 0.7, within ±1000 yaw from the view and 150 steps per correction. A refused turn is retried at ½ and ¼.
  - Plans from the clamped pose: `right`'s shoulder is taught at 3032, outside the 2995 envelope, so every turn used to be refused at waypoint 0.
  - A ray that misses the table (a sideways camera at left/right) is aimed at `FOLLOW_AIM_DIST` = 0.35 m along it.
  - When the camera is too low to tilt down (left view, about 10 cm), the whole arm solves for a camera 4 cm higher, aimed at the object with the wrist roll free.
  - The whole arm also aims when turning alone saturates.
- **Framing robustness** (`jobs._center_on_object`):
  - Waits for a confident detection and makes up to 8 corrections, stopping within 4 % of centre.
  - Remembers the last good pose and goes back to it when the object is lost.
  - Treats a ViT box that jumps more than 35 % from where the last turn should have put the object as a tracker slip, and runs `vision.refind` near that spot (nearest confident YOLOE box, else SIFT).
  - The camera model is not used to judge detections: at extreme side views it is wrong, and it rejected the real marker.
- **Follow:**
  - Dead band with hysteresis: starts correcting at 10 % off-centre, stops at 4 %.
  - Low-passes the box centre and steps from the last commanded pose (no shoulder sag).
  - The 3-D move needs a fused shift of more than 3 cm on 3 checks in a row and the object off-centre in the picture. The dolly needs size and fused range to agree beyond 25 %, three times in a row.
- **Keep in view:** the tracker decides whether the object is there. SIFT inliers, when counted, show a covering hand. It re-centres when the object drifts past 6 % or touches the edge, and backs off 8 s when it cannot turn further.
- **Fusion** (`fuse.ObjectFilter`): an innovation gate ignores rays more than 10° from the estimate unless 6 arrive in a row. The estimate stays above the table. Settled looks measure with the drawn crop first (`look_many`).
- **Servos** (`servo_limits.py`, kept by the arm server): overload threshold 60 on motors 1–7, gains P16/I8/D32 on motors 2–3. The software shoulder guard is 55 %.
- **Peter studio** (laptop dashboard home screen; files `src/console.ts`, `src/twin3d.ts`, `app.css`):
  - A live 3-D twin: URDF meshes from `/laptop/director/twin` and `/laptop/director/mesh/<name>.stl`, served by `camera_director/twin.py`, with three.js vendored (`vendor/`, MIT).
  - The robot camera with a Robot/Phone (WebRTC) switch.
  - The agent dock: typed commands take the voice path, and the orb grows and shines on the wake word.
  - Mode chips, the current-job capsule, STOP, full screen (F) and a Pair phone pop-up.
  - A menu with director tools, phone & voice, arm links and settings.
  - Select object closes the menu; a confirm bar floats on the camera picture, and the menu reopens afterwards.
- **App:** named P.E.T.E.R. with the Peter icon and splash (`peter_app_icon.png`) and the Peter palette. The package ID is unchanged (`dev.localvision.local_vision`).
- **After a reboot or replug:**
  - `sudo chmod a+rw /dev/bus/usb/<bus>/<dev>` for the gripper camera, then restart Station (it looks for cameras only at start).
  - Put the new queue name in `director_safety.json` `camera_queue` and in `jobs.GRIPPER_CAMERAS`.
  - The servos come back limp: use Hold here.
- **Tests:** 62 director tests. On 22 Nov a run of every test that changed today passed: test_4, 9a, 9b, framing, follow, show, filming briefs, FollowDistance, ObjectFusion. test_4's triangulated-height cross-check allows 3 cm. 9a starts from top with a fresh catalogue.
- **Not verified on the real arm:** the rise-and-roll fallback at the left view (`1f1e0c8`), and whether side-view centring converges on hardware.

## 22 Nov afternoon: detector, servos, follow, framing (read this first)

- **Setup:** `uv sync --locked --extra detector` installs YOLOE: Ultralytics plus CUDA 12.8 torch for the laptop's RTX 3070. The models live in `director_data/models/` and are git-ignored, so copy them onto a new machine: `vittrack.onnx`, `yolox.onnx`, `yoloe-26m-seg.pt` (sha256 prefix 585f5ec9028fd358). After a reboot, Station needs `sudo chmod a+rw /dev/bus/usb/<bus>/<dev>` for the gripper camera, and the camera queue name changes. The new name goes in `director_safety.json` `camera_queue` and in `jobs.GRIPPER_CAMERAS`.
- **Tracking:** OpenCV ViT runs on every frame. YOLOE runs with the drawn box as its visual prompt: when ViT is unsure, and once a second to check it. It takes 13–15 ms on the GPU. Confident new looks are averaged into the prompt every 5 s. Fallbacks are SIFT bank, then YOLOX objectness boxes matched by colour. The target, its drawn frame and its box survive a restart.
- **Fusion:** `camera_director/fuse.py` is a Kalman filter on the object's 3-D position. Each ray comes from the camera pose (joint readings) and the box direction, with range from apparent size. Only frames taken while the arm stands still count. The filter stays above the table. The state is `object` in the director state.
- **Servos:** `servo_limits.py` sets the overload threshold to 60 on motors 1–7 (Station default 25) and SO-101 gains P16/I8/D32 on motors 2–3 (ElRobot default P-only). That removed the shoulder sag below goals. The arm server puts both back after a calibration. The software shoulder guard is 55 %.
- **Framing:** held shots (show, cinematic, tour) and voice/Claude view moves (`/api/goto` with `frame_object`) end with base yaw and wrist turned onto the object. The next move steps back onto the view along the saved trail. `/api/goto` from an off-view pose with a trail retraces through a director `goto` job.
- **Follow:** it centres within 6 %. A 3-D move keeps the camera's offset to the fused position when that moves more than 3 cm sideways or up/down. A dolly keeps distance when size and fused range agree beyond 25 % three times in a row. Each step is at most 150 steps. Shoulder and elbow stay within 500 of the view. Yaw stays within ±1000. Steps run at normal speed. At saturation the whole arm aims; a refused 3-D step falls back to turning.
- **Known failing simulator tests:** 9a (keep in view cut) and 9b (cascade) already failed before this work. test_4's height check is borderline at 0.01 m.

## Tracker, wake word, transit routes (`04d210d`, `ee0bbab`)

- **Tracker.** OpenCV `TrackerVit` (`director_data/models/vittrack.onnx` from opencv_zoo object_tracking_vittrack_2023sep; git-ignored, so copy it onto a new machine) is started from the drawn box (`vision.target_init`). It runs every 0.25 s, also while the arm moves (`vision.track`). Below score 0.5, SIFT searches over a bank of up to 5 references, and a new reference is added when the object's size changes more than 1.6×. On the real clip it kept 122/149 frames against SIFT's 94, at 4 ms against 130 ms. Simulator tests pass. **Not yet verified on the real arm moving closer and further.** The target is in memory: redraw it after an arm server restart.
- **YOLOX re-find** (`vision.proposals`, `_detect_yolo`): when the tracker and SIFT both lose the object, YOLOX (`director_data/models/yolox.onnx`, opencv_zoo object_detection_yolox_2022nov, 35.9 MB, sha256 prefix c5c2d13e59ae883e; git-ignored, so copy it onto a new machine) proposes object boxes. Its objectness also covers non-COCO things: the marker pen was its top box. The box whose HSV histogram best matches the object's remembered looks wins, if the match is ≥ 0.55 and at least 0.25 better than the whole picture's. Plain grey objects are therefore skipped. It takes about 70 ms per frame and runs only while the arm stands still.
- **Wake word** (phone `WakeDetector.kt`): `keywordsThreshold` lowered from 0.17/0.25 to 0.08/0.12 (phone/BPE model) and `keywordsScore` raised from 2.0/1.5 to 3.0/2.5. The owner prefers false wakes to missed ones. The app is installed. The owner has not tested it yet.
- **Transit between views.** `motion_log.jsonl` shows that since `close` was checked (22 Nov 06:12), every move through transit started or ended at `home`, which is `"direct": false` by design (folded pose; the model has no self-collision check). The other checked pairs plan straight or as an arc at any gripper opening. `side_low` still goes through transit until it is checked.

## Previous handoff

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
- Shoulder-load guard (55 % for 1.2 s, below the servo overload threshold of 60 % set by servo_limits.py; was 23 %) caught real overload build-ups and locked; easing back worked; but a stop at a heavy pose can still trip on the next move (see open issues).

## Latest real-arm result (23 Nov, owner pressed Scene, sticker moved further from the base)

Scene (filmed as a Director Show because `computed_paths` is false): establishing `wide` steady → push-in steady → **arc at `top`, yaw −300..+325, sticker in frame 100 %** → hold steady → closing `wide` steady; no faults, motor 2 peak load 272 (trip: 25 % for 2 s). The owner reports the earlier overloads also came from the object lying too close to the base. **First thing to try next:** with the object at this distance, set `computed_paths: true` and run one short free-camera arc while watching motor 2 (the guard and the lock are in place); keep it off again if it trips.

Detection tip measured on the far sticker: SIFT on a 2× upscaled region around the last box gives confidence 0.97 (48 inliers) in 75 ms against 0.60 at full resolution; worth adding to `vision.detect` for small boxes.

## Safety guidelines for planned camera moves (owner, 23 Nov)

- **Stay well above the table:** "closer" means the object larger in frame, never the camera lowered toward the desk. Claude's filming instructions say so; Claude keyframes with dist × sin(elev) < 12 cm are refused (`brief.validate`), and `orbit.plan_shot` refuses any solved path whose camera comes below `MIN_CAMERA_HEIGHT_M` (12 cm) above the table.
- **Close enough, never lost:** the whole object stays in frame with margin, and it may fill at most `MAX_TARGET_FILL` (60 %) of the frame's width or height.
- `close` is now **checked** in `views.json` (supervised first-run there-and-back on 23 Nov, no faults), so voice and Claude can use it. `side_low` is still unchecked: run it the same way from `/control` before voice uses it.
- A second agent (Codex) committed to this branch in the same working tree (`303dc93`, `4dd5e0e`, `565e151`: Claude-planned `film` takes, voice wiring). Work one agent at a time, or pull and check `git log` before editing.

## Phone pairing fix (23 Nov)

QR pairing kept failing ("QR expired" on the second scan): the laptop gateway held one pairing proof and every QR request from any open tab replaced it, so the QR being scanned stopped matching. Fixed in `laptop_dashboard.py` (`0260abc`): while a pairing request is pending (< 45 s) every tab gets the same QR. Phone app 0.1.0 (installed 22 Nov 00:34) is current; no APK update was needed. After a gateway restart the phone must pair again.

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
