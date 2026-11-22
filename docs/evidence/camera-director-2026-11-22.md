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
