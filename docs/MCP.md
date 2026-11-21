# MCP server camera-arm

`mcp_server.py` gives Claude the tools to point the camera. It moves nothing itself: every move goes through the web server `capture_server.py` (`POST /api/goto`), so the motion layer's routes and guards apply ([MOTION.md](MOTION.md)), and STOP on the `/control` page also stops a move Claude started. Claude can only pick saved views; there are no free joint or Cartesian moves. Moves return at once, `get_status` tells how they went. The scene (objects Claude has seen in camera images) is stored here in `scene.json`. Design: [ARCHITECTURE.md](ARCHITECTURE.md).

## Setup

The MCP server needs the official SDK (`mcp` 2.3, listed in `pyproject.toml`). Everything that touches the arm keeps using only the standard library.

```bash
uv sync                                   # or: python3 -m venv .venv && .venv/bin/pip install 'mcp==2.3.0'
```

## Run

```bash
python3 capture_server.py --bus 5B79016329               # 1. the web server, the one program that moves the arm
.venv/bin/python mcp_server.py                           # 2. MCP on http://127.0.0.1:8200/mcp
claude mcp add --transport http camera-arm http://127.0.0.1:8200/mcp    # 3. once, in Claude Code
```

Without hardware:

```bash
python3 capture_server.py --sim --port 8891
.venv/bin/python mcp_server.py --arm-url http://127.0.0.1:8891
```

The MCP server reads the view names from the web server at start; restart it after capturing or deleting views. With `--sim` there is no camera, so `/api/still.jpg` returns the saved snapshot of the view the simulated arm is at, and the image and scene workflow can be tried without hardware. Note: on `--sim` unchecked views run at first-run speed, so a move takes 30–60 s there too.

| Option | Default | |
|---|---|---|
| `--arm-url` | `http://127.0.0.1:8890` | `capture_server.py` (the `--sim` server usually runs on 8891) |
| `--host` / `--port` | `127.0.0.1` / `8200` | where MCP listens (Streamable HTTP, stateless, path `/mcp`) |
| `--scene` | `scene.json` | scene file |
| `--wait` | `10` | seconds to wait for the web server at start |

## Tools

| Tool | Does | Returns |
|---|---|---|
| `list_views()` | saved views and where the camera is | `{views: [{name, description, aliases, checked, shows}], current_view, moving, can_move, blockers}` |
| `go_to_view(view)` | starts a move; `view` is an enum of the saved views (not `transit`) | at once: `{started, view, speed, first_run_speed, eta_hint, hint}`; `started: false` if already there |
| `get_status()` | how the current or last move is going | `{moving, target_view, phase, route, progress, current_view, can_move, blockers, stopping, hold, error, last_result: {ok, view, error, duration, route}}` |
| `stop()` | halts and holds, any time (`POST /api/stop`) | `{ok, hold: confirmed\|unconfirmed, was_moving, view, error}` |
| `get_camera_image()` | current still, only while not moving (`GET /api/still.jpg`) | JPEG image + `{"still", "view", "image_size", "taken_at"}` |
| `get_scene()` | recorded objects | `{objects: [{id, label, description, seen: [{view, camera, bbox, visible, at, age_s}]}], count}` |
| `update_scene(objects, replace=false)` | records what Claude saw | the scene as above |
| `remove_scene_object(id)` | drops an object | the scene as above |
| `best_view(object_id, move=true)` | ranks the saved views by the object's sightings and moves to the best | `{object, best_view, ranking, move?}` |
| `preview_frame(object_id, framing="fill", margin=0.1)` | **preview only**: where the camera would go to frame the object ([FRAMING.md](FRAMING.md)) | `{ok, table_point_m, camera_distance_now_m, camera_distance_m, joint_travel, min_height_m, notes}` or a refusal |

Resources: `camera-arm://guide` (operating guide for Claude: moving, images, scene, choosing views), `camera-arm://scene` (the scene as JSON).

How a move is started: `go_to_view` reads `/api/state` and sends its `generation` with `POST /api/goto`, exactly like a click on the control page, so a move requested before a STOP can never start after it. Refusals from the web server come back as tool errors with its readable text, e.g. `{"status": 409, "error": "refused", "detail": "moving to top: press STOP first"}`. Codes: `busy` (checked before asking the server), `refused` (409), `not_found`, `invalid_request`, `unavailable` (503, e.g. no camera), `timeout`, `arm_server_unreachable`.

The web server addition for this: `GET /api/still.jpg` in `capture_server.py` returns one JPEG with headers `X-View`, `X-Joints`, `X-Taken-At`, only while nothing moves or stops (409 otherwise), 503 if there is no fresh frame. It is read-only and never commands the arm.

## Scene format

`bbox` is normalized to the image: `cx`, `cy` = centre (0,0 = top left), `w`, `h` = size, all 0..1. `visible` = fraction of the object not covered (0..1). Objects are merged by `id`; a new sighting replaces the old one from the same view and camera, up to 10 sightings per object and 30 objects. Unknown views, boxes outside 0..1 and ids other than `[a-z0-9_-]{1,40}` are rejected and leave the scene unchanged.

```json
{"id": "pcb", "label": "Circuit board", "description": "green PCB",
 "seen": [{"view": "top", "camera": "arm", "bbox": {"cx": 0.52, "cy": 0.47, "w": 0.38, "h": 0.30}, "visible": 0.9}]}
```

## Tests without hardware

```bash
.venv/bin/python tests/test_mcp_sim.py    # about 10 s, ends with ALL OK
```

`capture_server`'s `State` and HTTP server run in the test process on the simulated arm, with the motion speeds raised for that process only. The MCP server is reached with the SDK's in-process client, so each call goes MCP → `/api/...` → `State` → `motion.go_to_view` → `SimArm`. Covered: tool list, view enum in the schema (no `transit`), list_views without joint values, unknown view, no-op at the current view, go_to_view returns at once, busy, no image while moving, arrival via rise/turn/descend, sim still of the right view, stop mid-move with confirmed hold, scene merge and validation, remove, both resources, web server down.

## Try it in Claude Code

With the web server and the MCP server running and the server registered, ask Claude Code for example:

- "Which camera views are there?"
- "Show me the desk from above." → `go_to_view("top")`, later `get_status`
- "What do you see?" → `get_camera_image`, then `update_scene`
- "Stop!" → `stop()`
