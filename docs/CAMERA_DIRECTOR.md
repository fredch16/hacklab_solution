# Camera director (beta)

Find a target in the robot camera, find the best view of it near every approved preset, recall a best view, and play the best views as a cinematic sequence. Everything runs locally in the arm server: no phone, voice, Claude key or internet. Design and audit: [implementation map](IMPLEMENTATION_MAP.md). Demo flow: [runbook](DEMO_RUNBOOK.md).

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
| **Select object** | Freezes the current frame; drag a box, optionally name it, **Confirm box**. Tick **Save as Hack Lab sticker** to store the crop as the sticker reference (needed once). |
| **Auto-find sticker** | Looks for the saved sticker reference in three fresh frames and needs two consistent hits. Without a reference it opens the box selection. |
| **Keep in frame** | Centre error and the correction `framing.py` would compute. Preview only: moves to computed poses are not enabled. |
| **Optimize all views** | Visits every preset. At each checked preset with the target visible it scores the preset and stops along its checked lines (12% and 25% of the way to each other checked view), returns to the best and looks again. Unchecked presets show `BLOCKED`; presets without the target show `SKIPPED`. |
| **Recall best** (per card) | Back to that preset's best view along approved routes, then a fresh look must score at least 85% of the saved score. |
| **Cinematic** | Visits the verified best views in preset order, re-acquires the target and holds each shot for 3 s; holds if the target is lost. Tick **Record the cinematic take** to record it with the card's own robot recorder (same tab and visibility rules). |
| **Director Tour** | Auto-find (if no target) → Optimize all views → Cinematic. |
| **STOP** | The arm server's STOP: cancels any director job and move and holds the arm. Any manual arm command also ends a director job. |

Badges: `SIMULATED` (sim), `DRY RUN` (real arm, not approved), `PHYSICAL` (approved). Target: `TRACKING n%`, `TARGET LOST`, `NO TARGET`.

## Score

`Q = 0.30 composition + 0.25 edge clearance + 0.20 target size + 0.15 sharpness + 0.10 stability`, the median over 5 frames after the arm has settled. A view without the target in at least 3 of them has no score. Target size aims at 8% of the frame. A bounding box says nothing about hidden sides or true occlusion.

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
