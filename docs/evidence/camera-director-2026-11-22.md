# Camera director beta: evidence, 22 November 2026

Branch `beta/ai-camera-director`. No real-robot motion was performed by the agent. Station was not running during this session, so nothing here is hardware evidence.

## Real footage replay (vision only, no motion)

Frames from an existing robot clip of 21 Nov (gripper camera, sticker on the desk; the clip itself stays in the ignored `Camera recordings/`). A crop of the sticker from the frame at 2 s was used as the reference.

| Frame | Result |
|---|---|
| 2, 10, 20 s (same view) | found, 42–70 SIFT inliers, confidence 0.94–0.97 |
| 30 s (camera facing the ceiling) | correctly not found |
| 40–90 s (top-down, sticker rotated ~30°, larger) | found in 6 of 6, 26–31 inliers, confidence 0.71–0.82 |

ORB found the rotated sticker in none of the 40–90 s frames, which is why SIFT is used. Template matching alone accepted a different white label at correlation 0.81; its threshold is 0.90 (the real sticker scored 0.96–0.98).

## Simulator (`capture_server.py --sim --director`, rendered camera, labelled SIMULATED)

The rendered camera uses the same camera model as the prediction, so model-versus-camera agreement here says nothing about the real arm's calibration.

- Director Tour from the laptop dashboard in Chromium, on an object marked by hand and named "my object": localized by triangulation from 3 views (26° apart); `top` and `wide` verified with one move each (model 0.92/0.95, camera 0.83/0.91); `home`, `left`, `right` predicted but not worth a move (cut by the edge / low score); `close`, `side_low` blocked (unchecked); two-shot cinematic played.
- Keep in view with a simulated hand above the object: covered at `top` → one cut to `wide` (seen) → hand removed → pull back to `top` (seen).
- Box selection in Chromium on a frozen frame with letterboxing; the browser's native drag cancelled the pointer until `preventDefault` was added.

## Automated tests

See the slice reports in the commit history; `tests/test_camera_director.py` (22 tests), `tests/test_mcp_sim.py`, `tests/test_laptop_dashboard.py` and `dashboard/apps/dashboard/test/director.test.mjs`.

## Not yet verified

- The live gripper camera queue id on this laptop, and that the camera director sees real frames through Station.
- Localization accuracy on the real arm (the residual shown after two or more views is the first check), and whether predicted best views match what the camera sees.
- Any physical director move. These need the operator's `director_safety.json` after watching each listed line run with the demo desk layout.

## Real arm, dry run (22 Nov, ~02:40–03:10, operator present)

Station v0.1.0-beta.10 on the lab Linux laptop (started with `sg dialout` because the agent's session lacked the group), arm server with `--director`, MCP, voice bridge, laptop dashboard. Gripper camera queue `usbvideo/dc84eab2…` at 1280×720, confirmed by eye (jaws in frame). No `director_safety.json`: DRY RUN throughout. The director moved nothing; joints before and after each survey were identical. The operator moved the arm home → top with the arm controls.

- Box selection on the real feed failed with "that picture is too old" (frame cache ~12 s); fixed by pinning the frozen frame.
- The operator marked the real Hack Lab sticker once (saved crop). Tracking at `home`: confidence 0.93–0.98, 39–45 inliers; at `top`: 18–20 inliers. Auto-find from the saved crop worked after every restart.
- Localization from `home` alone: 62.9 cm ahead, uncertainty ±14 cm (camera 69° from straight down). From `top` alone: 35.0–35.3 cm ahead, 0.9–1.0 cm left, ±1.7 cm, about 4.5 cm wide.
- The two views disagree by 28 cm; free triangulation puts the desk point 11 cm above the desk. The model's camera pose is off at the folded `home` pose (the sticker was predicted near the top edge of the `home` image; the camera saw it in the lower middle). The estimate now leans on the near-vertical view and the disagreement is reported as a warning.
- Dry-run predictions from the `top` estimate: top 0.97 (preset), wide 0.80 (32% toward top), right 0.45, left 0.43, home 0.29. Not verified: verification needs physical moves.
- The predictor's 55° obliqueness limit contradicted the real camera (tracked at 69°); now 75°.

Still to check: a tape-measured sticker position against 35 cm / 1 cm; verification moves after the operator's approval.

## Real arm, physical runs (22 Nov, ~03:20–05:00, owner present, PHYSICAL approval)

| Run | Result |
|---|---|
| Calibration tour top → wide → right → left | top and wide offsets recorded; right → left arc: **motor 2 overload (0x20)**, hold confirmed |
| Survey (top, wide, right, home) | top 0.94, wide 0.79 (32 % toward top), home 0.78 verified; right not worth a move; sticker 30.3–30.5 cm ahead (tape: 31) |
| Cinematic top → wide → home | first attempt: Station did not accept a command in time, hold unconfirmed then confirmed by STOP; cause: arm server at 100–127 % CPU from full-size SIFT; fixed (vision worker process, downscaled frames); second attempt: all three shots held, no faults |
| Scene approaches (computed poses), gripper open and later closed, slow and first-run speed | **motor 2 overload** four more times at load 28–32 %; servo protection is norma-core's 25 %/2 s for every motor (read from the servo registers) |
| Shoulder-load guard + lock | caught a build-up at 22 %, eased back one waypoint to 9 % holding, no fault; a later lock at 24 % then tripped on the return move |
| Snap | 144 steps back onto top in 2 s, load ≤ 8 % |
| **Gimbal take from top** | shoulder and elbow fixed, base yaw −200..+200 steps, one 13 s move, sticker in frame 77 % of sampled frames, shoulder peak 14 %, no faults, back on top |
| Moved sticker (away from the base) | localized closer (31 → 25.9 cm): the model's up/down is inverted at top; self-calibration (not yet run on the real arm) addresses this |

## Agentic filming continuation

- Read-only preflight confirmed Station with effective group `dialout`, the capture server with follower bus `5B79016329`, and the MCP, voice and dashboard services. The arm was stationary at `wide` with fresh joints, confirmed hold, no movement blockers, no fault bits and a live 1280×720 gripper frame. Motor 2 read load 60/1000. The robot recorder was idle.
- Read-only benchmark on one live frame at `wide` recovered the saved subject at confidence **0.60 in 55 ms** using the 2× hinted region on the reduced frame, versus **0.52 in 277 ms** at full-frame resolution. The original 0.60 → 0.97 figure above came from another frame and region.
- A follow-up wide-frame test at native resolution measured **0.949 with 41 inliers** in the 2× region versus **0.503 with 11 inliers** for the full frame. The live worker now uses a native-resolution region for small hinted targets. A separate end-to-end worker look returned **0.884 in 71 ms**, including decode and scoring.
- A live Claude structured-output request using a synthetic subject and reveal brief returned one take with three camera keyframes and a four-second hold. Simulator tests exercised the camera path from the live measured pose with the shoulder parked and a forced shoulder fault threshold. The latter confirmed HOLD without a follow-on move.
- A second live Claude request used the normalized box from the stationary `wide` frame and a generic subject label. Its small pan and reframe produced three locally valid waypoints. The arm received no move command.
- Chromium with intercepted director and recorder responses verified the free-form brief payload and the recorder sequence `start → stop → start → stop` across a take, obstruction, transit, clear hold and completion. The laptop gateway tests exercise the actual FFmpeg recorder separately.
- The director suite passed all 41 tests, with socket and worker warnings during simulator cleanup. The final live hint wiring also passed the isolated tracking test and was verified on the stationary real feed. Motion simulator passed 129 checks, motion optimization 1,716, capture server simulator 1,543, framing 28, laptop gateway 7 and dashboard 34. MCP and voice bridge suites passed. The physical approval tests cover missing, empty and malformed files.
- Reloaded capture and MCP after preflight. Joint readings were unchanged, the camera was live, and the same sticker was selected again. Live tracking reported the native 2× region with confidence **0.94 and 39 inliers**. The live MCP schema publishes the `film` job and its brief parameter.
- Real self-calibration, a moved-subject localization check, live occlusion footage and a computed arc are pending owner-operated acceptance.

## Randomized Claude briefs and supervised hardware acceptance

The simulator used real Claude API responses, a synthetic marked label, and seed `20261122` for command order and angles. Each supported brief went through the same `film` job used by the dashboard.

| Brief | Simulator result |
|---|---|
| Gentle 8° left pan, near-centred, five-second hold | Completed with 91 base encoder steps (8.0°). Final subject centre at (0.521, 0.500). |
| Upper-right thirds with the base still | Completed with zero base motion. Final centre at (0.672, 0.329), within 0.6% of the frame width and height of the request. |
| 6° counterclockwise roll, centred, base still | Completed with zero base motion and final centre at (0.499, 0.500). This test recorded the requested roll and joint travel. It did not measure image roll from the output frames. |
| Right-edge reveal toward the upper middle | One take completed. Final centre at (0.500, 0.350). |
| Very small movement with a five-second ending | Completed with 23 base encoder steps (2.0°) and the requested hold. |
| Pick up and turn over the marked object | Refused without motor movement. The planner correctly rejected manipulation but incorrectly inferred that no gripper was attached. |
| Short free arc near the overhead pose | HOLD. The IK checker rejected the first keyframe and the arm held its pose. Claude took 5.05 s, but the total job took about 143 s with the reach map. |

The five completed custom takes kept the shoulder and elbow parked. The subject was detected in all ten sampled motion frames. This sample gives a visibility check and is too small to rate cinematic quality. API responses took 3.08–6.67 s for those takes.

The reuse-first revision was tested with four more live Claude requests in the simulator. “Show the whole desk” used the saved `wide` view, “saved top view” reused `top`, and “keep this exact framing” held without joint movement. A 7° left pan combined with upper-left thirds correctly used custom keyframes and ended at (0.328, 0.328). All four jobs completed.

### Real camera and arm

- At `wide`, five idle telemetry samples showed fresh data, all status bits clear, motor 2 at load 60/1000, and a live 1280×720 gripper camera. A stationary prediction-only survey estimated the sticker at (0.0048, 0.2764, 0) m with 0.035 m estimated uncertainty. The motion history and joint readings were unchanged on the repeated check.
- Claude generated a 6° left pan to centre, slight clockwise roll in place, and upper-left thirds from the real measured `wide` frame. The local path checker accepted all three. These plans were evaluated without commanding the arm.
- A temporary laptop recorder session used the real Station feed. Closing its viewing socket finalized a downloadable 2.36 s Matroska clip with one 1280×720 MJPEG video stream. FFprobe verified the file. The arm's motion history was unchanged.
- The supervised `selfcal` job `ca19e364` saw the target at `wide` and `top`. The move toward transit-only `home` timed out with an unconfirmed hold. The old calibration loop continued to `right`, where motor 2 latched `0x20`. Peak sampled load was 344/1000. The job ended in HOLD with two usable views and no new fit.
- The owner cleared the latch. Motor 2 status returned to 0 and torque was confirmed holding. At that check the arm was at an unknown pose and the motion interlock refused attempts to continue. Computed paths stayed off.
- The revision restricts real calibration to the checked `top`–`wide` line, refuses transit in calibration, and ends on the first refused or incomplete move. The director's preset moves now also run the timed shoulder-load guard. Custom filming and gimbal paths reject viewing angles below 45°.
- The monitored Recover command toward `transit` reached 320/1000 shoulder load. STOP confirmed the hold before a latch. The owner then recovered to transit successfully. The subsequent guarded direct move to `wide` completed with peak load 84/1000 and no faults.
- Revised physical `selfcal` job `1bab028c` used eight usable sightings out of ten on the approved `top`–`wide` line. Image RMS improved from 0.0457 to 0.0134. The independent load monitor stopped the automatic return at a peak of 284/1000. The confirmed hold was 16% of the way from `top` to `wide`, with no faults. The revision now ends calibration at its final checked view.
- Moved-subject validation failed. At the same held arm pose the owner moved the sticker farther from the base. The model estimated its forward coordinate decreasing from 0.2158 to 0.2009 m, a 1.49 cm change in the opposite direction and within the 3.1 cm estimated uncertainty. The candidate fit was backed up outside Git and removed from the active calibration. The previous preset corrections and shoulder limit were preserved. Capture reloaded idle and reacquired the sticker. Computed paths stayed off.
- The real `center` framing preview reported DRY RUN and left the motion history unchanged. On the reloaded stack, live MCP returned all eight motors, all eight calibrated ranges, modelled geometry and all thirteen director job names.
- The updated director suite passed 44 tests, with a simulator socket cleanup warning. Motion simulator passed 131 checks, motion optimization 1,716, and MCP integration passed. The physical computed arc and automatic occlusion cut remain pending.
