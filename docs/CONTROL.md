# Control service

`control_server.py` is the only process that writes to the arm. Everything else (dashboard, MCP server, `ctl.py`, later voice) talks to it over REST on localhost. Moves never block: a move request returns a `motion_id` at once, progress arrives as Server-Sent Events. Standard library only, no new dependencies. Design: [ARCHITECTURE.md](ARCHITECTURE.md).

| File | What |
|---|---|
| `control_server.py` | HTTP server (REST + SSE) on `127.0.0.1:8100`, camera source for stills |
| `motion_manager.py` | one move at a time as a background task: busy, stop, via, watchdog, timeout, events |
| `ctl.py` | command-line client |
| `control_ui.html` | local control page at http://127.0.0.1:8100/: views with snapshots, STOP (also Esc/Space), joint dials, events |
| `motion.py` | unchanged behaviour; `move_to` got an `on_tick` callback for progress and the watchdog |
| `go.py` | refuses to run while the control service is up (two writers must never drive the arm) |
| `tests/test_control_sim.py` | everything above on the simulated arm, no hardware |

## Run

```bash
python3 control_server.py --sim                      # simulated arm, uses views.json
python3 control_server.py --bus 5B79016329 --speed 250   # real follower, half speed
python3 control_server.py --bus 5B79016329 --camera      # + stills from Station's USB camera
python3 control_server.py --bus 5B79016329 --gateway http://127.0.0.1:8400   # + stills from the iPhone gateway (later)
```

Ctrl-C stops the service; a move in flight is cancelled and the arm holds.

## Control page

Open http://127.0.0.1:8100/ while the service runs (also with `--sim`):

- **STOP** button, or Esc / Space anywhere on the page.
- **Views** with their snapshots; click one to move there. Disabled while a move runs. Green border = the arm is at that view, blue = target of the running move.
- **Joint dials** per motor, in encoder steps 0–4095: shaded arc = calibrated range from Station, needle = present position, orange dot = target of the current move.
- **Current move** with progress bar, and the **event log**.

It polls `GET /ui/state` (status + calibrated ranges + target joints) five times a second and listens to `GET /events`. It is a local test page, not the dashboard: it shows joint targets, which the MCP server never passes on to Claude.

## CLI

```bash
python3 ctl.py health            # Station ok, joint data age, bus, camera
python3 ctl.py status            # state, last motion, nearest view, joints
python3 ctl.py views             # names and descriptions (no joint values)
python3 ctl.py go top            # returns at once
python3 ctl.py go top --wait     # prints progress until the move ends
python3 ctl.py tour 2            # home -> every view -> home, twice
python3 ctl.py stop              # cancel and hold, works at any time
python3 ctl.py watch             # live events
python3 ctl.py still -o still.jpg
```

Same with curl:

```bash
curl -s -X POST localhost:8100/motions -d '{"view":"top"}'
curl -s -X POST localhost:8100/stop
curl -N localhost:8100/events
```

## API

| Call | Response |
|---|---|
| `GET /health` | `{station_ok, station_error, joint_age_s, bus, camera, uptime_s, views}` |
| `GET /status` | `{state: idle\|moving\|holding, motion, nearest_view: {view, distance, at}, joints, last_error, bus}` |
| `GET /views` | `{views: [{name, description, aliases, via, snapshot}]}`, no joint values |
| `POST /motions {"view": "top"}` | `202` motion · `400 invalid_request / invalid_json` · `404 unknown_view` · `409 busy` · `409 not_ready` (stale joint data) · `501` for `{"frame": ...}` (stage 2) |
| `GET /motions/{id}` | `{motion_id, view, path, state, progress, duration_s, error}`; last 50 motions |
| `POST /stop` | always `200` with the status; cancels a move or holds the current position |
| `GET /still.jpg` | JPEG with headers `X-Joints`, `X-Nearest-View`, `X-Taken-At` · `409 moving` · `503 no_camera / camera_error` |
| `GET /` · `GET /ui/state` · `GET /snapshots/{view}.jpg` | control page, its state (status + `ranges` + `target` + `gripper`), view snapshots (only files named in views.json, inside the views folder) |
| `GET /events` | SSE. First a `status` event, then one `motion` event per state change and progress update (max 5/s), `: keep-alive` every 15 s |

Motion states: `moving` → `reached` · `stalled` (a joint ended more than 40 steps short) · `timeout` (a leg took over 15 s; the arm holds) · `stopped` · `error` (watchdog: joint data older than 0.5 s; nothing more is written, the servos keep the last waypoint).

Event example:

```json
{"t": 1795270751.3, "kind": "motion", "motion_id": "m-3", "view": "top", "state": "moving", "progress": 0.42, "text": "top: moving"}
```

State changes (not progress) are also appended to `events.jsonl`, next to the director's events.

### views.json additions (optional)

```json
"top": {"joints": {...}, "description": "...", "aliases": ["von oben"], "via": ["home"]}
```

`via` views are passed first, in order: use it for moves that would otherwise swing through the table.

## Tests without hardware

```bash
python3 tests/test_control_sim.py   # control service, ~15 s, ends with ALL OK
python3 tests/test_motion_sim.py    # motion layer, still passes
```

Covered: start returns at once, busy, reached, stop holds, unknown view and bad input, via path, stop while idle, watchdog on stale data, not_ready, timeout holds, stalled joint, events.jsonl, every REST route, SSE, `ctl.py go --wait` and `ctl.py stop`.

## First test on the real arm

Before: Station running, follower set to **"(Self-controlled)"** in the Station web UI (the service does not check mirroring yet), one person at the follower's power switch, nothing near the arm.

| Step | Command | Expect |
|---|---|---|
| 1 | `python3 control_server.py --bus 5B79016329 --speed 250` | log line `control service on http://127.0.0.1:8100  bus=5B79016329  views=7  nearest=<view> (<n> steps)` |
| 2 | second terminal: `python3 ctl.py watch` | one `status` line: `state=idle`, nearest view |
| 3 | `python3 ctl.py health` | `"station_ok": true`, `joint_age_s` below 0.2 |
| 4 | `python3 ctl.py go <nearest view> --wait` | small move, `reached`; in `watch` the progress bar |
| 5 | `python3 ctl.py go home --wait` | arm folds to home at half speed, `reached` |
| 6 | `python3 ctl.py go top`, then within 1 s `python3 ctl.py stop` | arm stops where it is and holds (no sag), `stopped` |
| 7 | `python3 ctl.py go top` twice quickly | second call: `error: 409 busy` |
| 8 | `python3 ctl.py tour 1` | every view once, all `reached`; watch for paths that swing low (add `via` for those) |
| 9 | with `--camera`: `python3 ctl.py still -o still.jpg` | JPEG saved, nearest view and joints printed |
| 10 | Ctrl-C in the server terminal | `shutting down: holding the arm`, arm stays put |

If a move reports `stalled`, look at which motor and how many steps; the tolerance is 40 steps (`motion.ARRIVE_TOLERANCE`). If it reports `error ... stale`, Station stopped sending joint data: check the Station terminal and USB.
