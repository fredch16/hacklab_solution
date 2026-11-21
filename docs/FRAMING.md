# Framing an object (stage 2): preview

"Bring the PCB fully into the picture" without a saved view for it. This branch computes and checks where the camera would have to go. **It never moves the arm**: moving to computed targets (stage C) needs the motion layer to accept targets other than saved views, which is the team's safety-critical code and is left for a decision with Fred.

## How it works

```
1  get_camera_image (MCP)      -> JPEG, image number "still", view, image size; the MCP server
                                  remembers the arm's joints for that image (X-Joints header)
2  Claude marks the object     -> update_scene: {id, bbox in that image, still}
3  preview_frame(object_id)    -> POST /api/frame {bbox, framing, margin, still_joints, image_size, dry_run: true}
4  capture_server.py           -> refuses if the arm moved since the image (any joint > 20 steps off)
                                  or is moving; then framing.plan() with the arm's own present joints
5  framing.py                  -> ray through the box centre -> point on the table
                                  -> camera pose: same viewing direction, centred on that point,
                                     at the distance where the object fills the frame ("fill")
                                     or at the present distance ("center")
                                  -> joints by damped least squares on fk.py's model
                                  -> checks; "fill" backs off step by step until a target passes
```

Claude never sends coordinates or joint angles. It names a box in an image; the web server works out the geometry from its own reading of the arm.

## Checks on a target

| Check | Limit | Error |
|---|---|---|
| Box centre looks down onto the table | ray points down, table point < 1.5 m away | `not_on_table` |
| Table point within reach | 0.45 m from the base axis | `out_of_reach` |
| Model reaches the camera pose | 1 cm, 3 degrees | `unreachable` |
| Joints stay in their calibrated range | Station's ranges | `at_limit` |
| Joint travel from the present pose | 500 steps (~44 deg) per joint | `outside_envelope` |
| Lowest point of the arm (target and straight path) | 4 cm above the table, or no lower than the arm is now | `too_low` |
| Camera distance to the object | 0.12 to 0.45 m (the jaws reach further down than the camera) | backs off |

For a computed target the height check is stricter than `fk.check_path`: that one also accepts a path that is no lower than its end poses, which only makes sense for poses the real arm has been in.

## What is estimated

Everything rests on `fk.py`'s model plus:

| Value | Where | Status |
|---|---|---|
| Camera offset and tilt on the gripper | `fk.CAMERA_OFFSET`, `fk.CAMERA_TILT_DEG` | fitted from `views/top.jpg`, about ±2 cm, ±5° |
| Horizontal field of view | `framing.HFOV_DEG = 62` | guessed for a small USB camera; the iPhone will differ |
| Table height | `framing.TABLE_Z = 0.0` | assumed = the plane the base stands on |
| Image not mirrored | `framing.MIRRORED = False` | not checked |
| Absolute joint angles | `fk.py` mapping | not measured; heights maybe 2–3 cm off |

### Checking it on the real arm (before stage C)

1. Arm at `top`. Put a small object (a coin) on the table, take `get_camera_image`, mark it, `preview_frame` with `center`.
2. Compare `table_point_m` with the coin's real position (ruler from the base axis: +y forward, +x to the right seen from behind the arm). Off by more than 2–3 cm: adjust `CAMERA_OFFSET` / tilt / `HFOV_DEG`.
3. Move the coin to the right edge of the image: `table_point_m` must move to +x. If it moves to −x, set `MIRRORED = True`.
4. Repeat from `wide`. Only when both agree is the preview worth a real move.

## Interfaces

**Web server** (`capture_server.py`):

| Call | |
|---|---|
| `GET /api/still.jpg` | now also `X-Image-Size: 1280x720` |
| `POST /api/frame` | `{"bbox": {cx, cy, w, h}, "framing": "fill"\|"center", "margin": 0.1, "still_joints": {...X-Joints...}, "image_size": "1280x720", "dry_run": true}` → 200 preview · 400 `invalid_request` · 409 `stale_image` / `moving` · 422 with a check's error and the partial preview · 501 without `dry_run` |

**MCP** (`mcp_server.py`):

| Tool | |
|---|---|
| `get_camera_image()` | meta now `{still, view, image_size, taken_at}` |
| `update_scene(objects)` | a sighting may give `still` instead of `view`; the server stores the arm pose and image size with it (`has_pose` in `get_scene`) |
| `preview_frame(object_id, framing="fill", margin=0.1)` | preview from the newest sighting with a pose; nothing moves |
| `best_view(object_id, move=true)` | ranks the saved views by the sightings (large, centred, visible, recent) and moves to the best one |

Preview result (shortened):

```json
{"ok": true, "framing": "fill", "table_point_m": [-0.009, 0.352, 0.0], "object_size_m": [0.118, 0.059],
 "camera_distance_now_m": 0.218, "camera_distance_m": 0.18,
 "joint_travel": {"1": -12, "2": -181, "3": 206, "4": 287, "5": -3, "6": -302, "7": -14},
 "min_height_m": {"now": 0.08, "target": 0.048, "path": 0.048, "floor": 0.04},
 "notes": ["backed off to 0.18 m: closer failed (outside_envelope: ...)"], "dry_run": true, "from_view": "top"}
```

## Command line

```bash
python3 framing.py top 0.6 0.55 0.3 0.25     # preview from a saved view: box cx cy w h
```

## Tests (no hardware)

```bash
python3 tests/test_framing.py        # camera model round trips, centre/fill results, refusals (~4 s)
.venv/bin/python tests/test_mcp_sim.py   # also: image numbers, preview_frame does not move, /api/frame errors, best_view
```

## What stage C would need

- `motion.go_to_pose(joints)` next to `go_to_view`: the same route through `transit`, guards, STOP generations and first-run speed for every computed target.
- `POST /api/frame` without `dry_run`, starting that move like `/api/goto` (with `generation`).
- The real-arm checks above passed, the estimates replaced by measurements.
- One correction step after arrival: new image, compare the box with the image centre, at most one more small move.
