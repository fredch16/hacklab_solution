# Operator Runbook - Camera Director Demo (2026-11-23)

This is a demonstration checklist, not evidence that these features exist yet.

## Start: robot only

- Open the laptop dashboard with robot/NormaCore Station connected; leave Pixel unpaired and Claude API key unset for the local proof.
- Keep dashboard tab and selected robot live feed visible. Under the existing reported behavior, a robot recording stops when the tab or live feed is no longer visible.
- Verify the selected camera is **physically mounted on the moving arm** before centering; do not assume the external camera moves with the arm.
- Verify human Stop, hardware E-stop, cabling, table clearance and verified local safe movement envelopes. Clear the workspace, keep people away from swept arm volume and supervise all first physical moves.

## Recommended demo beats

1. **Target acquisition:** Select `topview` by the existing safe preset path; click `Auto-find Hack Lab sticker`. Show bounding box and confidence. If unable to match, teach the real sticker once with manual ROI; never move based on a guessed match.
2. **Keep in frame:** Show tracked target, normalized center error, and a few bounded safe camera corrections. Press STOP once as a deliberate demonstration only when safe to do so; restart via normal workflow.
3. **Best view at each position:** Click `Optimize all approved views`. Show live progress and per-preset cards: candidate snapshots, selected best measured servo pose, before/after score, visible/skipped reason. Do not try a missing or unapproved pose.
4. **Cinematic:** Choose validated best shots and click `Cinematic`. If verified safe transitions exist, record smooth transitions and held shots. If not, show the storyboard cut sequence and explain route safety is blocking physical moves.
5. **Bonus:** `Recall best` or compare before-and-after thumbnails. Optional Claude MCP command can invoke the exact same local job, after the offline proof is done.

## Dashboard controls for demo

`Auto-find sticker` | `Select object` | `Track` | `Keep in frame` | `Optimize all views` | `Best shots` | `Cinematic` | `Stop`.

Show `source`, `target confidence`, `current preset`, `candidate progress`, `score`, `measured pose`, `motion permission`, and `target lost/stop reason`.

## Fallback ladder

- Sticker cannot be detected: one-time manually confirmed crop; thereafter use same tracker. If still uncertain: manual ROI selection.
- A preset does not see target: `SKIPPED_TARGET_NOT_VISIBLE`, show other shots; no blind arm search.
- A route or collision envelope is not verified: `BLOCKED_BY_SAFETY`; use a truthful best-shot gallery/replay; never unlock motion just to record a cinematic clip.
- Tracker unreliable while moving: pause/settle/reacquire; use stable shot-and-cut montage instead of continuous orbit.
- Claude API/voice fails: click dashboard buttons; no effect on local CV/robot controller.
- Pixel not available: irrelevant for core demo.
- No robot/hardware access: show replay with `SIMULATED` badge; never present as hardware proof.

## Evidence to collect

- Sticker topview screenshot with rectangle and confidence.
- Target selected manually on a different object.
- Before/after images and scores for at least two safe presets if physically possible.
- Measured joint poses and approved route IDs, not just sent command values.
- Tracking / controller update times, target-loss and STOP logs.
- A real cinematic robot recording if a validated path exists; otherwise clearly labeled storyboard proof.
- Current branch/commit and test outputs for each completed slice.

## Do not claim

- Collision-free motion from joint limits alone.
- Metric 3D object localization from an uncalibrated RGB frame.
- Full 360-degree object orbit from a pan/tilt-like motion.
- All presets gave better scores if some target views were missing.
- Offline Claude planning if using hosted Claude API.
