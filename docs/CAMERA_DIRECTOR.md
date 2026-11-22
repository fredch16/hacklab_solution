# Camera director (beta)

Mark any object in the robot camera, find the best view of it near every approved preset, recall a best view, play the best views as a cinematic sequence, and keep the object in view when a hand covers it. The Hack Lab sticker is only an example target.

**A director, not a search.** The arm does not sweep candidate poses with the camera rolling. The object is localized on the desk from the measured servo positions and Station's live calibration (through `fk.py`'s ElRobot model), every approved pose is scored by prediction without moving, and each preset gets at most one deliberate move to its predicted best pose, which the camera then verifies. Everything runs locally in the arm server: no phone, voice, Claude key or internet. Design and audit: [implementation map](IMPLEMENTATION_MAP.md). Demo flow: [runbook](DEMO_RUNBOOK.md).

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
| **Keep in view** | Holds the best shot and watches. When the object stays covered (3 of 4 looks over 2 s, compared with the matched-feature count when the arm settled), it announces and makes one cut to the best other view that looks from at least 20° different, then dwells ≥ 8 s, never returns to a covered view within 30 s, and after 15 s tries the preferred view once (going back if it is still covered). |
| **Recall best** (per card) | Back to that preset's verified view along approved routes, then a fresh look must score at least 85% of the saved score. |
| **Cinematic** | Visits the verified best views in preset order, re-acquires the object and holds each shot for 3 s; holds if it is lost. |
| **Record the shots** | Records with the card's own robot recorder (same tab and visibility rules) during the cinematic and keep-in-view phases only, never while estimating or verifying. |
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

**Scene** moves the camera around the marked object instead of between saved views: each move is keyframes in azimuth, elevation and distance, solved by IK on the ElRobot model, checked along the whole curve (envelope, 4 cm desk floor, self-clearance, step size) and run as one continuous move. Claude can design the moves (Plan with Claude; planner only, validated locally); otherwise a local scenemaker uses the reach map. **Return** retraces a scene's path; **Snap** puts the arm back onto a saved view it stopped within 200 steps of, with one short move instead of lifting to transit.

On the real arm (22 Nov) scenes are **off** (`computed_paths: false` in `director_safety.json`). Station writes every ST3215 at each calibration freeze with norma-core's defaults (`software/drivers/st3215/src/presets.rs`): overload torque 25 %, protection time 2 s, max torque and torque limit 50 %. The shoulder (motor 2) carries the whole arm and tripped that protection (status 0x20) during computed approaches from `top`, even with the gripper closed and at first-run speed (load 28-32 %). A shoulder-load guard (23 % for 1.2 s) stops computed moves and locks at the last pose the shoulder can hold, but a stop at a heavy pose can still trip on the way back. Scenes need either motor 2's overload threshold raised (an EEPROM change, overwritten at the next calibration freeze) or less load on the shoulder. Everything between saved views (survey, recall, cinematic glides on checked lines) runs within the limit.
