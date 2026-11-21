# Keeping an object in view (parked draft)

State: Sat 21 Nov, 22:30. **Unfinished. `watch.py` has never run.** Nothing on this branch is connected to `voice_bridge.py` or merged into main. Parked for a review on 22 Nov.

## The idea

"Keep the PCB in view": the camera watches one object and, when a hand stays in front of it, moves to another position where it can be seen again.

## What was agreed in the discussion (21 Nov)

1. **Watch loop over saved views.** Look about once a second. A fast model (Claude Haiku 5.5, vision) says whether the object is in the picture and how much of it is covered. When it is covered in 3 of the last 4 looks, move to the next-best checked view. Stay at least 8 s after a move; do not return to the view just left for 45 s.
2. **Slide along a line, as the second move.** When switching views does not clear it: move in quarter steps along the straight line between two checked views (for example `left` → `top`), judge each stop, stop when the object is visible, reverse when it gets worse. Those lines are already verified by the geometry check for direct moves.
3. **Not doing:** free nudging to unchecked poses, and computed "closer" poses (`framing.py` stays preview-only). "Closer" is the saved view `close`, once it is checked on the real arm.

Why not free nudging: every nudge is a pose nobody has checked, next to a hand; between two pictures the hand moves more than a 2 cm nudge changes the view; and a small shift rarely clears a covered object.

## How the next view is chosen

Each view remembers how large and central the object was when it was last clearly visible from there. Order: best remembered view, then views not looked from yet, then views where the object was not found. `home` and `transit` are never chosen. This is a memory, not a live look: the camera cannot know whether the hand also covers the object from the other view until it is there.

## Safety design (in the draft)

The loop moves the arm on its own, at the moment a hand is in the workspace. So:

- Every automatic move is announced for 2.5 s first and can be cancelled.
- Every automatic move carries the STOP generation seen at the look that caused it. The control server refuses the move if anyone pressed STOP since then, from anywhere (voice, the red button, `/control`, Claude). Following then turns itself off.
- A move of its own that does not arrive turns following off. No retries.
- Only saved, checked views. All of the motion layer's checks apply as for any move.

## What is on this branch

| File | State |
|---|---|
| `mcp_server.py` | `get_status` also returns `generation`; `go_to_view` takes an optional `since_generation`. Small, additive. `tests/test_mcp_sim.py` still ends with ALL OK; the new parameter itself has no test yet. |
| `watch.py` | First draft of the judge, the rules and the loop. Never run, no tests. Known gaps: the list of checked views is never loaded (`refresh_views` is not called), and a lock is held across a network call in `_go`. |

## Still to do

1. Finish and test `watch.py` on the simulated arm (scripted judge, like the scripted interpreter in `tests/test_voice_bridge.py`).
2. Wire it into `voice_bridge.py`: "keep the X in view", "look around for the X", "stop following"; any stop also ends following. Status and a cancel button on the voice page.
3. Tune the judge on real pictures. Needed: about 15 stills from the arm camera with the PCB on the desk, from `top`, `right` and `wide`: clear, half covered, fully covered, hands near but not covering, hand moving through. Command to take them:
   ```zsh
   mkdir -p ~/Projects/hacklabhackathon/stills && cd ~/Projects/hacklabhackathon/stills
   while read "name?name: "; do curl -sf -o "$name.jpg" http://127.0.0.1:8890/api/still.jpg && echo "saved $name.jpg" || echo "no picture (arm moving?)"; done
   ```
4. Motion layer: a move to a point on the line between two checked views (for the slide). Not started.
5. Real arm: check `close` and `side_low`, run a survey, tune the thresholds. About half an hour with a person at the arm.

## Open questions for the review

- Go ahead at all, given the Sunday 16:30 deadline?
- If yes: only the watch loop, or also the slide along a line?
- Reaction time is 4–5 s by design (one look takes 1–2 s). Is that acceptable for the demo?
