# Camera Operator

*"Your hands are always in the way of the camera, so the camera moves. And you can tell it what to look at."*

A robot arm holds a camera; Claude acts as the director. You say "show me the board from the left", Claude picks one of the saved viewpoints, and the arm moves the camera there. HackLab #2, NormaCore track, Berlin, 21–22 Nov 2026.

```
you / voice ──► Claude (director.py) ──► go_to_view() ──► motion.py ──► Station ──► ElRobot follower + camera
                    ▲                                                                       │
                    └──────────────────────── camera frames (camera.py) ◄───────────────────┘
```

## Local Vision mobile app and dashboard

Source, build steps, and agent instructions live under [dashboard/](dashboard/README.md). The Flutter app records and transcribes on the phone. The paired browser interprets spoken recording requests and asks the phone's recorder to start or stop. Other spoken requests go to `voice_bridge.py` on the PC, which calls the camera-arm MCP server: run `.venv/bin/python voice_bridge.py` next to `mcp_server.py`. With `ANTHROPIC_API_KEY` set (environment or `.env`), loosely worded requests ("yo, show me the thing from da top") are interpreted by Claude; without it only the fixed phrases work. Voice mode without a phone: with the bridge running, open `http://127.0.0.1:8210/` in Chrome or Safari on the same PC (tap to talk, or hands-free after "Hey Claude"; the browser does the speech recognition). [Current architecture](docs/ARCHITECTURE.md)

The bundled Whisper decoder uses Git LFS. Run `git lfs install` before cloning. In an existing checkout, run `git lfs pull` before building the Android app. The APK bundles the model for offline operation. The dashboard source was imported with 22 commits as the second parent of `fc86036`. Three commit IDs changed when the decoder entered Git LFS.

## Status (Sat 21 Nov, ~16:15)

| Milestone | State |
|---|---|
| M0 arm + camera from our code | ✅ joints read, camera frames grabbed, torque control works on the real arms |
| M1 saved viewpoints | ✅ 7 views + the `transit` pose in `views.json`. `left`/`right`/`side_low`/`home` sit on joint limits and should be re-captured slightly off the stops (and `left`/`right`/`side_low` with a level horizon) |
| M2 move to a view | 🟢 **works on the real arm**: 10 supervised moves on 21 Nov between transit, `top`, `wide` and `right`, from the web page at `/control`. Still to do: `left`, `close`, `side_low`, `home`, the Stop and block tests, normal speed. Details and data in [docs/MOTION_PLAN.md](docs/MOTION_PLAN.md) |
| M3 Claude picks the view | 🟡 `director.py` works end to end on the sim (with a stubbed Claude); needs `ANTHROPIC_API_KEY` for a real run |
| M4 voice | Android on-device wake and transcription plus browser-routed recording commands live in `dashboard/`. Spoken arm requests (stop, go to a checked view, status, list views) go from the dashboard through `voice_bridge.py` to MCP, with Claude interpreting free phrasing; tested on the simulated arm, not yet on the real arm by voice. |

Docs: **[docs/HARDWARE.md](docs/HARDWARE.md)** (arms, serials, cameras, Station config, calibration) · **[docs/MOTION.md](docs/MOTION.md)** (how moves work, the API) · **[docs/MOTION_PLAN.md](docs/MOTION_PLAN.md)** (design, council review, test protocol) · **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** (current Android capture and dashboard, MCP handoff, previous planning context) · **[docs/MCP.md](docs/MCP.md)** (MCP server for Claude) · [PLAN.md](PLAN.md) (team plan, milestones) · [patches/](patches/) (Station web UI tweaks).

## Hardware in one paragraph

Two **NormaCore ElRobot** arms (8 servos each, 7+1 DoF; *not* SO-101s as originally planned). **Follower `5B79016329`** carries the camera on its gripper and is the arm our code drives; **leader `5B61033001`** is moved by hand. Each arm has a servo-board USB, a camera USB and its own power supply. Both are calibrated in Station. Details and gotchas: [docs/HARDWARE.md](docs/HARDWARE.md).

## Setup

1. **Station** v0.1.0-beta.10 (`station-macos-arm64.zip` from https://github.com/norma-core/norma-core/releases). Copy [`config/station.yaml`](config/station.yaml) next to the binary (camera resolution settings), then `./station --web --tcp`. Web UI: http://localhost:8889.
2. **Python**: the arm scripts use only the standard library (`python3 joints.py` works as-is). For the Claude director: `uv sync` (Python 3.13 + `anthropic`).
3. **Claude**: `export ANTHROPIC_API_KEY=...` (HackLab credits).

The Station Python client, protobufs and the ElRobot URDF/manuals are vendored in `vendor/normacore/` (MIT, matching beta.10).

## Scripts

| Command | Milestone | What it does |
|---|---|---|
| `python joints.py` | M0 | Live joints of every arm, and which is limp vs holding |
| `python calib_watch.py` | M0 | Live calibration progress per joint |
| `python camera.py [--list]` | M0.8 | Save one frame to `frame.jpg` / list cameras |
| `python torque.py off\|on` | M1.1 | Go limp / hold the present pose (joints 1–7; the gripper stays limp) |
| `python capture_server.py --bus 5B79016329` | M1 + M2 | **Web UI** at http://localhost:8890. `/` captures views (live camera, Go limp / Hold here, Save). **`/control` moves the arm: click a view, STOP always visible.** |
| `python capture_server.py --sim --port 8891` | | The same UI on a simulated arm, no hardware |
| `python go.py --where` / `top` / `--tour 5` / `--mark-checked top` | M2 | Command-line moves; `--sim` for the simulated arm; Ctrl-C holds |
| `python fk.py` | M2 | Geometry model: height of the arm in every view and along every route |
| `python3 arm_view.py` | M2 | **Watch the arm move as a stick figure** (side/top/front) at http://localhost:8896; follows the server on :8891 (sim) or `--source http://localhost:8890` (real arm). Read-only |
| `.venv/bin/python mcp_server.py` (`--arm-url http://127.0.0.1:8891` for `--sim`) | M3 | **MCP server** camera-arm on localhost:8200/mcp for Claude: views, moves (through the web server), status, stop, camera still, scene. See [docs/MCP.md](docs/MCP.md) |
| `python3 framing.py top 0.6 0.55 0.3 0.25` | M5 | **Framing preview** (stage 2): where the camera would go to frame a box from a view; never moves. See [docs/FRAMING.md](docs/FRAMING.md) |
| `uv run python director.py [--sim]` | M3 | Type a request, Claude picks a view, the arm moves |
| `python3 tests/test_motion_sim.py`, `python3 tests/test_server_sim.py [port]`, `python3 tests/test_fk.py` | | Motion layer, web-server race and geometry model tests (no hardware; about 75 s, 2 min and 5 s) |

## Interfaces between the pieces

- `motion.go_to_view(arm, name) -> {"ok", "view", "error", "duration", "route"}`: the one call everything else uses. It refuses with a readable `error` instead of moving when anything is off.
- `views.json`: named poses (raw encoder steps per motor id) + description + snapshot path. Format in [docs/MOTION.md](docs/MOTION.md#contract-please-keep-these-stable).
- `camera.Camera.get_frame() -> JPEG bytes`: latest frame from a Station camera queue.
- `events.jsonl`: `director.py` appends `{"t", "kind": "heard"|"decided"|"moved"|"reply"|"error", "text"}` per step, for the overlay (E) and spoken confirmation (D).

The director uses Claude Opus 5.5 (`claude-opus-5-5`, effort `low` for snappy replies) with one strict tool, `go_to_view`, whose `view` parameter is an enum of the saved view names, so Claude can't invent poses. Server-side refusal fallback is enabled (`fallbacks: "default"`).

## Safety rules

- Set the follower to **"(Self-controlled)"** in Station before moving it; the code refuses while Station mirrors a leader onto it.
- Every move goes **rise → turn → descend** through the taught `transit` pose, only from a known pose, only with the arm already holding. The gripper (camera mount) is never moved.
- A move stops and holds if a joint doesn't follow, pushes at full effort, draws too much current, stops answering, goes limp, is commanded by something else, or the data goes stale. **STOP** on the page, Space/Esc, or Ctrl-C holds the arm where it is, and the page says if that hold could not be confirmed.
- Views nobody has watched on the real arm run at first-run speed only (100 steps/s, 30–60 s per move). After a clean there-and-back run, mark the view as checked.
- Use **one controlling program** per session (the web server). STOP on the page cannot stop a move started by `go.py` or `director.py` in another terminal.
- With several arms connected, `--bus auto` may refuse to choose: pass `--bus 5B79016329`.
- **Recalibrating the follower invalidates its saved views** (they are raw motor positions); the code detects this and asks for a re-capture.
- First runs of anything new: slow, one person at the arm, power plug within reach.
- **Never leave a powered arm unattended. Ask a mentor before touching the camera mount.** (hackathon rules)

## What's ours vs. starter code

NormaCore provides Station (robot runtime + web UI), its Python client and the ElRobot hardware files; those are vendored unchanged in `vendor/normacore/` (plus the small viewer patch in `patches/`). Everything else in this repo was written this weekend: arm wrapper, camera access, view capture and control web UI, the motion layer and its geometry model, the simulator and the Claude director.
