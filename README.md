# Camera Operator

*"Your hands are always in the way of the camera, so the camera moves. And you can tell it what to look at."*

A robot arm holds a camera; Claude acts as the director. You say "show me the board from the left", Claude picks one of the saved viewpoints, and the arm moves the camera there. HackLab #2, NormaCore track, Berlin, 21–22 Nov 2026.

```
you / voice ──► Claude (director.py) ──► go_to_view() ──► motion.py ──► Station ──► ElRobot follower + camera
                    ▲                                                                       │
                    └──────────────────────── camera frames (camera.py) ◄───────────────────┘
```

## Status (Sat 21 Nov, ~13:45)

| Milestone | State |
|---|---|
| M0 arm + camera from our code | ✅ joints read, camera frames grabbed, torque control works on the real arms |
| M1 saved viewpoints | ✅ 7 views in `views.json` + `views/*.jpg`. `left`/`right`/`side_low` come out sideways and should be re-captured with a level horizon |
| M2 `go_to_view` | 🟡 implemented + tested on the simulated arm; **not yet run on the real arm**, no collision checks. Motion team: [docs/MOTION.md](docs/MOTION.md) |
| M3 Claude picks the view | 🟡 `director.py` works end to end on the sim (with a stubbed Claude); needs `ANTHROPIC_API_KEY` for a real run |
| M4 voice | ⬜ not started (build against `director.py --sim` and `events.jsonl`) |

Docs: **[docs/HARDWARE.md](docs/HARDWARE.md)** (arms, serials, cameras, Station config, calibration) · **[docs/MOTION.md](docs/MOTION.md)** (motion planning handoff) · [PLAN.md](PLAN.md) (team plan, milestones) · [patches/](patches/) (Station web UI tweaks).

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
| `python calib_watch.py` | M0 | Live calibration progress per joint; shows which arm is moving |
| `python camera.py [--list]` | M0.8 | Save one frame to `frame.jpg` / list cameras |
| `python torque.py off\|on` | M1.1 | Make an arm limp / hold (hold it first!) |
| `python capture_server.py --bus 5B79016329` | M1 | **Capture web UI** at http://localhost:8890: live camera, Go limp / Hold here, guided steps, Save/Retake/Delete |
| `python capture_view.py --bus 5B79016329` | M1 | Same as above in the terminal (Enter per view) |
| `python go.py top` / `--tour 5` / `--speed 250` | M2 | Move the follower to saved views; Ctrl-C holds where it is |
| `uv run python director.py [--sim]` | M3 | Type a request, Claude picks a view, the arm moves; `--sim` needs no hardware |

## Interfaces between the pieces

- `motion.go_to_view(arm, name) -> {"ok", "view", "error", "duration"}`: the one call everything else uses.
- `views.json`: named poses (raw encoder steps per motor id) + description + snapshot path. Format in [docs/MOTION.md](docs/MOTION.md#contract-please-keep-these-stable).
- `camera.Camera.get_frame() -> JPEG bytes`: latest frame from a Station camera queue.
- `events.jsonl`: `director.py` appends `{"t", "kind": "heard"|"decided"|"moved"|"reply"|"error", "text"}` per step, for the overlay (E) and spoken confirmation (D).

The director uses Claude Opus 5.5 (`claude-opus-5-5`, effort `low` for snappy replies) with one strict tool, `go_to_view`, whose `view` parameter is an enum of the saved view names, so Claude can't invent poses. Server-side refusal fallback is enabled (`fallbacks: "default"`).

## Safety rules

- Set the follower to **"(Self-controlled)"** in Station before `go.py` / `director.py`, or leader mirroring fights our moves.
- Moves stay inside each joint's calibrated arc, are capped at ~44°/s, hold position on Ctrl-C, and never move the gripper (camera) unless `--gripper` is passed.
- First runs of new motion code: half speed, one person at the arm, power plug within reach.
- **Never leave a powered arm unattended. Ask a mentor before touching the camera mount.** (hackathon rules)

## What's ours vs. starter code

NormaCore provides Station (robot runtime + web UI), its Python client and the ElRobot hardware files; those are vendored unchanged in `vendor/normacore/` (plus the small viewer patch in `patches/`). Everything else in this repo was written this weekend: arm wrapper, camera access, view capture (CLI + web UI), the motion layer, the simulator and the Claude director.
