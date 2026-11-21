# MCP server camera-arm

`mcp_server.py` gives Claude the tools to point the camera. It moves nothing itself: every move goes through the control service's REST API, so all safety checks there apply ([CONTROL.md](CONTROL.md)). Claude can only pick saved views; there are no free joint or Cartesian moves. Moves return at once, `get_status` tells how they ended. The scene (objects Claude has seen in camera images) is stored here in `scene.json`. Design: [ARCHITECTURE.md](ARCHITECTURE.md).

## Setup

The MCP server needs the official SDK (`mcp` 2.3, listed in `pyproject.toml`). The control service needs nothing beyond the standard library.

```bash
uv sync                                   # or: python3 -m venv .venv && .venv/bin/pip install 'mcp==2.3.0'
```

## Run

```bash
python3 control_server.py --sim           # 1. control service (or --bus 5B79016329 for the real arm)
.venv/bin/python mcp_server.py            # 2. MCP on http://127.0.0.1:8200/mcp
claude mcp add --transport http camera-arm http://127.0.0.1:8200/mcp    # 3. once, in Claude Code
```

The MCP server reads the view names from the control service at start; restart it after adding views. With `--sim`, `get_camera_image` returns the saved snapshot of the view the simulated arm is at, so the image and scene workflow can be tried without hardware.

| Option | Default | |
|---|---|---|
| `--control` | `http://127.0.0.1:8100` | control service |
| `--host` / `--port` | `127.0.0.1` / `8200` | where MCP listens (Streamable HTTP, stateless, path `/mcp`) |
| `--scene` | `scene.json` | scene file |
| `--wait` | `10` | seconds to wait for the control service at start |

## Tools

| Tool | Does | Returns |
|---|---|---|
| `list_views()` | saved views and where the arm is | `{views: [{name, description, aliases, shows}], nearest_view, state}` |
| `go_to_view(view)` | starts a move; `view` is an enum of the saved views | at once: `{motion_id, view, path, state: "moving", hint}` |
| `get_status(motion_id?)` | how the last or a given move went | `{state, motion: {state, progress, error, ...}, nearest_view, last_error}` |
| `stop()` | halts and holds, any time | `{state, motion, nearest_view}` |
| `get_camera_image()` | current still, only while not moving | JPEG image + `{"nearest_view", "taken_at"}` |
| `get_scene()` | recorded objects | `{objects: [{id, label, description, seen: [{view, camera, bbox, visible, at, age_s}]}], count}` |
| `update_scene(objects, replace=false)` | records what Claude saw | the scene as above |
| `remove_scene_object(id)` | drops an object | the scene as above |

Resources: `camera-arm://guide` (operating guide for Claude: moving, images, scene, choosing views), `camera-arm://scene` (the scene as JSON).

Errors from the control service come back as tool errors with the same code, e.g. `{"status": 409, "error": "busy", "detail": "already moving to 'left'; stop first"}`. Codes: `busy`, `unknown_view`, `not_ready`, `moving`, `no_camera`, `camera_error`, `control_unreachable`.

## Scene format

`bbox` is normalized to the image: `cx`, `cy` = centre (0,0 = top left), `w`, `h` = size, all 0..1. `visible` = fraction of the object not covered (0..1). Objects are merged by `id`; a new sighting replaces the old one from the same view and camera, up to 10 sightings per object and 30 objects. Unknown views, boxes outside 0..1 and ids other than `[a-z0-9_-]{1,40}` are rejected and leave the scene unchanged.

```json
{"id": "pcb", "label": "Circuit board", "description": "green PCB",
 "seen": [{"view": "top", "camera": "arm", "bbox": {"cx": 0.52, "cy": 0.47, "w": 0.38, "h": 0.30}, "visible": 0.9}]}
```

## Tests without hardware

```bash
.venv/bin/python tests/test_mcp_sim.py    # ends with ALL OK
```

The control service runs in-process on the simulated arm, the MCP server is reached with the SDK's in-process client, so each call goes MCP → REST → motion manager → simulated arm. Covered: tool list, view enum in the schema, list_views without joint values, unknown view rejected, go_to_view returns at once, busy, no image while moving, reached, image with nearest view, stop mid-move, scene merge, scene validation, remove, both resources, control service down.

## Try it in Claude Code

With both services running and the server registered, ask Claude Code for example:

- "Which camera views are there?"
- "Show me the desk from above." → `go_to_view("top")`, then `get_status`
- "What do you see?" → `get_camera_image`, then `update_scene`
- "Stop!" → `stop()`
