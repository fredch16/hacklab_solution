# Camera Operator

A LeRobot SO-101 arm holds a camera; Claude acts as director and moves it to named viewpoints. HackLab #2, NormaCore track. Team plan: [PLAN.md](PLAN.md).

```
you / voice ──► Claude (director.py) ──► go_to_view ──► motion.py ──► Station ──► SO-101 follower + camera
```

## Setup

- Station v0.1.0-beta.10: download `station-macos-arm64.zip` from https://github.com/norma-core/norma-core/releases, run `./station --web --tcp`, open http://localhost:8889. In `station.yaml`, set `usb-video.resize_target: 720` and `formats: [{format: "max@30fps,mjpeg"}, {format: "max@30fps,420v"}]` (defaults give 224px from a 320x240 capture).
- Python: `uv sync` (Python 3.13, `anthropic` SDK). The Station Python client is vendored in `vendor/normacore` (MIT, matches beta.10).
- `export ANTHROPIC_API_KEY=...` for the director.

## Arms: leader + follower

Both arms: power supply (each arm's own, voltages differ) + USB to the laptop. The **leader** is limp and moved by hand; the **follower** copies it and carries the camera. In the web UI, on the follower's card set *control source* to the leader's serial to mirror; set it back to *(Self-controlled)* before running `go.py` / `director.py`.

## Scripts

| Script | Milestone | What it does |
|---|---|---|
| `python joints.py` | M0.7 | Live joints of every arm; shows which is leader (limp) and follower (torque on) |
| `python camera.py` | M0.8 | Save one camera frame to `frame.jpg` (`--list` shows cameras) |
| `python torque.py off\|on` | M1.1 | Make the arm limp to pose by hand (hold it first!) |
| `python capture_view.py` | M1 | Guided capture: pose with the leader, press Enter per view → `views.json` + `views/<name>.jpg` |
| `python go.py top` | M2 | Move the follower to a view; `--tour 5` runs home → every view → home ×5; Ctrl-C holds |
| `uv run python director.py` | M3 | Type requests, Claude picks the view, the arm moves; `--sim` runs without hardware |

`go_to_view(arm, name) -> {"ok", "view", "error"}` (in `motion.py`) is the interface the rest of the team builds on. `director.py` appends `{"t", "kind", "text"}` lines (heard / decided / moved / reply / error) to `events.jsonl` for the overlay and voice confirmation.

## Safety

Moves are interpolated inside each joint's calibrated arc, capped at ~44°/s, and never touch the gripper unless `--gripper` is passed (the camera may be mounted on it). Never leave a powered arm unattended.
