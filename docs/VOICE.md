# Voice control of the camera arm

The wake word opens a voice command window. The final transcript can select a saved view, run the camera director, read status or control a recorder.

```
phone app (Android)  ──text──►  dashboard page  ──┐
                                                  ├──► voice_bridge.py ──► mcp_server.py ──► capture_server.py ──► arm
PC voice page (Chrome/Safari) ──text──────────────┘         │
                                                            └── Claude Haiku 5.5, only for loose phrasing
```

The laptop dashboard handles exact recording phrases locally. Other completed live transcripts go through `voice_bridge.py` to the arm tools and camera director. Exact director phrases route directly to MCP. Claude interprets eligible loose phrasing and plans custom camera shots. [Routing and acceptance](AGENT_HANDOFF.md#recording-voice-routing-and-lift-over)

The laptop dashboard also routes [phone and robot recording requests](LAPTOP_DASHBOARD.md#recording-and-speech) via the local gateway and MCP. This path requires a paired phone for speech and phone recording. For robot recording it additionally requires a visible dashboard tab and live robot feed.

## Wake-word demo

Pair the phone, leave the app in the foreground with microphone permission, and open the robot camera card on the laptop. Mark the object or use **Auto-find sticker**. Say the configured wake phrase, usually **Hey Claude**, then one command. Check the final transcript and its result on the laptop.

| Say after the wake word | Result |
|---|---|
| “Director status”, “Motor status”, “List views”, or “Where are you?” | Reads job, motor or view state. |
| “Keep in frame” or “Preview framing” | Calculates a framing correction in place. |
| “Go to top” or “Go to wide” | Uses the checked saved view. “Recall best wide” uses a previously verified shot. |
| “Find the best view” or “Optimize all views” | Estimates candidates and verifies selected views. |
| “Keep the object in view” or “Automatic best view” | Watches continuously and handles sustained occlusion. |
| “Cinematic”, “Director show”, “Director tour”, “Gimbal take”, or “Start a scene” | Starts the corresponding camera job. Scene uses Director Show when free paths are disabled. |
| “Snap to view”, “Return along the last shot”, or “Self calibrate the camera” | Runs the corresponding recovery or calibration job if its gate and starting pose allow it. Use these with the operator present. |
| “Start robot recording” or “Stop robot recording” | Controls the laptop robot recorder. “Start recording” and “Stop recording” target the phone. |
| “Stop arm” | Halts arm motion. |

For an unfamiliar shot, begin with “Film a shot”. Try “Film a shot, ease the object a little down from the top edge and hold”, “Film a shot, reveal the desk while leaving room on the right”, or “Film a shot, add a slight tilt and hold for four seconds”. With Claude enabled, also try “Could you ease left and reveal the desk while keeping the object at the upper third?”, “Show me the whole desk”, and “Keep the marked object visible even if my hand gets in the way”. Those three phrases classified as a custom take, the saved wide view and Keep in view in the live API test. The job status shows the chosen plan or why it could not run.

### Occlusion demo

1. Select a textured object and start with an unobstructed view. **Optimize all views** builds the verified shot list. Keep the camera card visible and enable **Record the shots** if you want clips.
2. Say “Hey Claude, keep the object in view”. Wait for a clear hold. Obscure the camera's line of sight with a card held outside the arm's path, leaving the object itself in place.
3. Watch the status for recording paused and one cut to an eligible angle. If the alternate sees the object, three clear looks resume recording. If it is still blocked, the arm holds and keeps checking the feed. A later clear hold can start a new occlusion episode.
4. Say “Hey Claude, stop arm” or use the dashboard STOP. Completed clips are available in the robot camera card.

## Start it

On the Mac with the arm, after Station, `capture_server.py` and `mcp_server.py` are running (see the README):

```
.venv/bin/python voice_bridge.py
```

The log should end with `free phrasing: Claude (claude-haiku-5-5)`. Then open **http://127.0.0.1:8210/** in Chrome or Safari on the same Mac and allow the microphone.

## Configure the Claude key

From the HackLab repository root, create a private `.env` from [`.env.example`](../.env.example) if you don't already have one:

```bash
test -e .env || install -m 600 .env.example .env
```

Open `.env` in a local editor and replace `Enter your key` with your Claude API key on the `CLAUDE_API_KEY` line. The voice bridge and CLI director load that file when they start. A `CLAUDE_API_KEY` already exported in the shell takes priority over the value in the file. Existing Anthropic environment variable names still work when no Claude key is configured.

To set a key without restarting the bridge, open **Claude API key settings** from the laptop dashboard. A key saved there takes effect immediately and overrides the startup key for that bridge process. **Remove session key** restores the startup credential. Restarting the bridge clears the dashboard override and reloads the local file.

For agents, verify the ignored path with `git check-ignore .env` and use only placeholder keys in tests. Keep real keys out of diffs, command output, browser fixtures, and commits. Commit only the template. Treat the template placeholder as unconfigured.

Without a configured key, the bridge handles the fixed phrases. `--llm off` disables Claude, and `--llm-model` selects the model.

## The PC voice page

| | |
|---|---|
| Tap to talk | Click the microphone or press Space, say one request, pause. Everything said is sent. |
| Hands-free | Always listening; acts only after "Hey Claude" (the name can be changed on the page). "Hey Claude", a pause, then the request within 8 s also works. |
| Stop | A spoken "stop", "halt", "freeze" or "abort" is sent as soon as the word is heard, without the wake name. Esc and the red button do the same. |
| Typing | The text box sends a request without the microphone. |

The chips at the top show whether the arm's MCP server answers, whether free phrasing is on, and the microphone state. Speech recognition is the browser's own (Chrome sends the audio to Google, Safari uses Apple's) and needs the Internet. Firefox has none. "Claude" is often heard as "cloud" or "clawed"; the page accepts those.

## What you can say

The PC voice page sends requests directly to the bridge. Its fixed arm grammar handles these without a Claude API call:

| Said | Does |
|---|---|
| stop, halt, wait, freeze, abort, hold on (anywhere in the sentence) | stops the arm |
| anything mentioning "record" | nothing here (recording belongs to the phone dashboard) |
| where are you, status | says where the camera is |
| list views, which views are there | lists the checked views |
| go to / move to / show me / look at ... `<view name or alias>` | moves there |

Anything else goes to Claude, which picks one of the same four actions or none. Examples from the live test: "yo claude show me the thing from da top" → top, "give me the white shot" → wide, "whoa whoa cut it out" → stop, "don't go to the top" → nothing, "zoom in a little bit" → nothing.

## Rules that do not depend on the model

- Only views marked as checked on the real arm can be reached by voice. Others answer "isn't checked on the real arm yet".
- A stop never waits for Claude.
- If the Claude call fails or takes longer than 6 s, the request counts as not understood and nothing moves.
- Each utterance runs at most once.
- The bridge accepts browser requests only from its own page (`http://127.0.0.1:8210`), from https pages on private addresses (the phone's dashboard) and from `--allow-origin` entries.
- What leaves the PC: the transcript text and the view names and descriptions go to the Anthropic API. No audio, no images.

## What has been tested

| | |
|---|---|
| `tests/test_voice_bridge.py` | Grammar, origin rules, the Claude step with a canned client, the page's wake-name and stop decisions (under node), and the whole path on the simulated arm. Ends with ALL OK. |
| `tests/test_voice_bridge.py --live` | 15 loosely worded requests against the real Anthropic API: 15 of 15 as expected, about 1 s each (slowest 2.3 s). |
| Real arm, PC voice page | 21 Nov 21:58: four spoken requests in a row started the right move (top, right, wide, top). One was a fixed phrase; three went through Claude ("What if I want to see it from the right? Can you do that for me?" → right). |
| Not tested | A spoken stop during a move on the real arm. Hands-free mode on the real arm. The phone app together with this bridge (needs a new Android build that includes commit `909065a`). |

## Phones

The phone app is Android only. Its dashboard forwards spoken requests to this bridge from commit `909065a` on; build steps are in `dashboard/docs/STATUS.md`. There is no iOS build: the iOS folder is the empty Flutter starter.

## Parked: keeping an object in view

Branch `keep-in-view` holds an unfinished draft and its plan (`docs/KEEP_IN_VIEW.md` on that branch): watch an object such as the PCB and move to another saved view when a hand covers it. It has never run and is not connected to anything on main.
