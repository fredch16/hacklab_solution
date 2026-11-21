# Camera Operator: team plan

HackLab #2 · NormaCore track · Berlin, 21–22 Nov 2026 · team of 5

Submission: Sun 16:30 at tally.so/r/GexoxQ (aim for 16:00). Feature freeze Sun 12:00.

## Team split (5 people)

| Who | Owns | Milestones | Then (after M4) |
|---|---|---|---|
| **A: Arm** | Station install, connection, joint read/write, safe motion | M0.1, M0.4–M0.5, M0.7, M0.9, M2 (`go_to_view`, interpolation, speed cap, joint limits, stop) | Get arm 2 running (M8), low-level `nudge(dx, dy)` for M5/M6 |
| **B: Views + camera** | Camera in Station, frame grab, the pose library, collision testing | M0.2, M0.6, M0.8, M1 (all), M2.6–M2.7 | M7: occlusion check, "best unblocked view" table |
| **C: Director** | Claude tool loop and prompt, against a mock arm until A is ready | M3.1–M3.6 | M5 `focus_on(object)` (Claude vision on frames) |
| **D: Voice** | faster-whisper, push-to-talk, spoken confirmation | M4 (all) | M6 `follow_hands` (MediaPipe local loop) |
| **E: Demo + glue** | Repo, README, heard → decided → moved overlay, backup clips, video, slides | M0.3, M3.5, M3.7, submission checklist | Integration testing, demo script rehearsal |

**Hardware:** A + B share arm 1 Saturday morning. Once M0 works on arm 1, set up arm 2 the same way so C and D can test against a real arm without waiting.

**Safety:** whoever is running an arm watches it. Nobody runs motion code on an arm someone else is posing by hand.

## Interfaces (agree on these first, then work in parallel)

```python
# A → everyone
go_to_view(name: str) -> {"ok": bool, "view": str, "error": str | None}
list_views() -> list[str]          # names from views.json
stop() -> None                     # hold position

# B → C (M5), D (M6), B (M7)
get_frame() -> bytes               # latest JPEG from the arm camera

# C → E (overlay) and D (confirmation)
# one JSON line per event appended to events.jsonl
{"t": 1732190000.1, "kind": "heard" | "decided" | "moved", "text": "..."}
```

C starts with `mock_arm.py` (prints, sleeps 1.5 s, returns ok) behind the same signatures, then swaps in A's module.

## Rough timeline

| When | A | B | C | D | E |
|---|---|---|---|---|---|
| Sat 11–13 | M0 arm | M0 camera, ask mentor about mount | M3 on mock | M4.1 whisper on WAV | Repo, README stub, overlay skeleton |
| Sat 13–16 | M2 | M1 poses | M3 prompt + test phrases | M4.2 push-to-talk | Overlay reads events.jsonl |
| Sat 16–19 | M2 hardening, arm 2 | M2.6–2.7 testing | **M3 on real arm** | M4.3 pipe into M3 | **Record M3 backup clip** |
| Sat evening | nudge API | M7 prep | M5 | M4 done, M6 | Video storyboard, slides draft |
| Overnight | M5–M7 with C/D/B | | | | |
| Sun 9–12 | Robustness | Re-capture poses at final setup | Prompt polish | Noise tuning | Rehearse demo |
| Sun 12–16 | Freeze | Film | Film | Film | Edit, README, slides, **submit by 16:00** |

## Milestones

See the original checklist (M0–M8, submission checklist) in the team doc. Done criteria:

- **M0:** script prints joints, saves a frame, wiggles one joint.
- **M1:** `views.json` holds ~7 named poses (top, wide, left, right, close, side_low, home).
- **M2:** any view → any view runs smoothly, 5 times in a row.
- **M3 ⭐:** "show me it from the left" → arm moves to left.
- **M4:** say "top-down view" → arm moves.

## Station notes (from the norma-core repo)

- Run: `station --web --tcp` → web UI at http://localhost:8889, TCP API on :8888. macOS: `station-macos-arm64.zip` from the releases page.
- Python SDK: `software/station/shared/station_py` (stdlib only) plus generated protobuf code under `target/gen_python`.
- Motor state: `client.follow("st3215/inference", queue)`, parse with `st3215.InferenceStateReader`.
- Motor commands: `st3215.Command(write=ST3215WriteCommand(motor_id, address=0x2A, value=...))` wrapped in `commands.DriverCommand`, sent with `send_commands`.
- Best starting points: `software/station/examples/so101-autocalibration-py` and `st3215-remote-teleop-py`.
- Camera: `usb-video` driver in `station.yaml`. The queue name for frames is still to find (ask the mentor, M0.1).
