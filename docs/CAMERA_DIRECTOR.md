# Camera director (beta)

Mark any object in the robot camera, find the best view of it near every approved preset, recall a best view, play the best views as a cinematic sequence, and keep the object in view when a hand covers it. The Hack Lab sticker is only an example target.

Candidate poses are evaluated before motion. The director estimates the object's position from measured joints and camera frames, then verifies the chosen shot with the camera. Vision, scoring and motion run locally in the arm server. A Claude key and internet connection are required for free-form language planning. [Implementation map](IMPLEMENTATION_MAP.md) · [Demo runbook](DEMO_RUNBOOK.md)

## Start

```bash
.venv/bin/python capture_server.py --bus 5B79016329 --director     # real arm (dry run until approved)
.venv/bin/python capture_server.py --sim --port 8890 --director     # simulated arm + rendered camera, labelled SIMULATED
.venv/bin/python laptop_dashboard.py                                # then open http://127.0.0.1:8300/
```

Without `--director` nothing changes: the director API answers 404 and the dashboard panel says it is off.

## Dashboard controls (robot camera card)

| Control | What it does |
|---|---|
| **Select object** | Freezes the current frame; drag a box around any object, optionally name it, **Confirm box**. The same tracker, localization and director handle every object. **Save as Hack Lab sticker** stores the crop for Auto-find. |
| **Auto-find sticker** | Shown once a sticker crop is saved: looks for it in three fresh frames and needs two consistent hits. |
| **Keep in frame** | Centre error and the correction `framing.py` would compute. Preview only: moves to computed poses are not enabled. |
| **Optimize all views** | 1. *Estimate* (no motion): localize the object from the current settled view (triangulated once there are views from different directions; the residual is shown) and predict every approved pose: each checked preset and stops 8–32% along its checked lines. 2. *Verify*: presets predicted at least 0.6 and clear of the picture edge get one move to their predicted best pose and a camera check; each new sighting refines the localization. If the view was better on the way in (a hand covers the destination), the arm pulls back along the same line. Card states: `VALID` (verified), `PREDICTED` (not worth a move, with the reason), `NOT CONFIRMED` (predicted but not seen), `SKIPPED` (predicted not visible), `BLOCKED` (unchecked preset). |
| **Keep in view** | Uses local target matches and feature-count changes to recognize sustained obstruction. It pauses automatic recording after three obstructed looks out of four, then tries one better angle. It prefers a verified shot with at least 20° of separation. With computed paths approved, it can also plan a new angle from its current pose. Recording resumes after three clear looks. A clear fallback stays in use. If the cut also fails, it waits for the subject to reappear. |
| **Film with Claude** | Enter a filming brief for the selected object. Claude proposes continuous camera angles, screen position, roll and hold time using the current measured camera context. Local IK and visibility checks decide whether the shot can be executed. |
| **Recall best** (per card) | Back to that preset's verified view along approved routes, then a fresh look must score at least 85% of the saved score. |
| **Cinematic** | Visits the verified best views in preset order, re-acquires the object and holds each shot for 3 s; holds if it is lost. |
| **Record the shots** | Saves clean holds and deliberate moves via the robot recorder, with the same tab and visibility rules. Setup, occlusion and cuts separate clips. Completed MKVs stay under `Camera recordings/`. The card links to the latest clip. |
| **Director Tour** | (Auto-find if nothing is selected) → Optimize all views → Cinematic. |
| **STOP** | The arm server's STOP: cancels any director job and move and holds the arm. Any manual arm command also ends a director job. |

Badges: `SIMULATED` (sim), `DRY RUN` (real arm, not approved), `PHYSICAL` (approved). Target: `TRACKING n%`, `TARGET LOST`, `NO TARGET`.

## Score

Measured: `Q = 0.30 composition + 0.25 edge clearance + 0.20 target size + 0.15 sharpness + 0.10 stability`, the median over 5 frames after the arm has settled. A view without the object in at least 3 of them has no score. Target size aims at 8% of the frame.

Predicted: the same terms from the projected outline, with legibility (cosine of the angle from straight down) in place of sharpness. Views more than 55° from straight down, under 40 px, behind the gripper jaws or outside the picture are not visible. Predictions rest on `fk.py`'s model and the camera estimates in `framing.py`; a prediction never counts as a verified view, and the card shows model and camera scores side by side so the model error is visible.

## Approving physical moves

On the real arm the director only plans and scores the current view until the operator writes `director_safety.json` in the repo root (ignored by git):

```json
{
  "physical_motion": true,
  "approved_by": "<operator name>",
  "approved_at": "2026-11-23T09:00",
  "bus": "5B79016329",
  "camera_queue": "usbvideo/<the gripper camera queue shown in the panel>",
  "lines": ["top-wide", "right-wide"],
  "max_fraction": 0.12
}
```

Write it only after watching each listed line run on the real arm with the desk laid out for the demo. Check that the stalk, cables and objects on the desk stay clear for the whole move between the two presets: the director stops at most `max_fraction` of the way along those moves. The geometry model does not model the camera stalk or cables. Delete the file to go back to dry run.

## Data

`director_data/` (ignored by git): `sticker.png/.json` (the confirmed crop), `catalogue.json` and `thumbs/` from the last job. A new target clears the results of the previous one.

## Tests

```bash
.venv/bin/python -m unittest tests/test_camera_director.py      # vision, selection, survey, recall, cinematic, STOP, gate (~2 min)
(cd dashboard/apps/dashboard && npm run build && npm test)       # box mapping for contain/cover/rotation/mirror/resize
```

## Free camera (scenes) and the ElRobot shoulder

Mark the subject, keep the camera card visible, and enter a brief such as “Ease left, move the subject from the right edge to the upper third, and hold.” Use **Film with Claude**. The same entry point is exposed as MCP `director_job(kind="film", brief="…")`. The existing named view controls remain available for exact preset moves.

Claude receives the currently available views and jobs. It can reuse a checked preset, hold, watch, verified cinematic sequence, or Director Show when that fits the request. Custom takes aim from at least 45° above the subject and run through the desk-clearance and shoulder guards.

The planner receives the brief, target label, normalized box and numeric camera context. Frames and servo commands stay local. With computed paths disabled, the shoulder and elbow stay parked and the shot can pan, roll or reframe. With computed paths approved, the shot may also change elevation and distance. The arm holds at the end for the next instruction. An unavailable camera path or sustained loss of the target ends the take and reports the reason.

For a camera mounted upside down relative to the pose model, the local ignored `director_data/camera_calibration.json` supports `"roll_180": true` inside its `"model"` object. This rotates both image axes while leaving the optical axis and camera origin unchanged. The physical follower uses this option. Validate the direction of a moved subject before trusting free-camera localization. [Physical measurements](evidence/camera-director-2026-11-22.md#camera-orientation-and-wake-word)

The design applies SayCan's split between language intent and physically feasible motion, and ReKep's use of visual constraints as inputs to local planning. The ElRobot continues to use its existing motion owner, model checks, telemetry and STOP path. [SayCan](https://arxiv.org/abs/2204.01691) · [ReKep](https://arxiv.org/abs/2409.01652) · [MoveIt Servo guard design](https://moveit.picknik.ai/humble/doc/examples/realtime_servo/realtime_servo_tutorial.html)

**Scene** moves the camera around the marked object instead of between saved views: each move is keyframes in azimuth, elevation and distance, solved by IK on the ElRobot model, checked along the whole curve (envelope, 4 cm desk floor, self-clearance, step size) and run as one continuous move. Claude can design the moves (Plan with Claude; planner only, validated locally); otherwise a local scenemaker uses the reach map. **Return** retraces a scene's path; **Snap** puts the arm back onto a saved view it stopped within 200 steps of, with one short move instead of lifting to transit.

On the real arm, free-camera scenes are **off** (`computed_paths: false` in `director_safety.json`). Station writes every ST3215 at each calibration freeze with norma-core's defaults (`software/drivers/st3215/src/presets.rs`): overload torque 25%, protection time 2 s, max torque and torque limit 50%. Motor 2 tripped that protection (status 0x20) during computed approaches from `top` at 28–32% load. The software guard (23% for 1.2 s) ends the job at its confirmed hold. The servo can still latch if a stopped pose remains too heavy. Real self-calibration and the owner-operated short arc remain the next hardware checks.
