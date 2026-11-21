# Voice control of the camera arm

State on Sat 21 Nov, 22:00. You speak, and the camera moves to one of the saved views.

```
phone app (Android)  ──text──►  dashboard page  ──┐
                                                  ├──► voice_bridge.py ──► mcp_server.py ──► capture_server.py ──► arm
PC voice page (Chrome/Safari) ──text──────────────┘         │
                                                            └── Claude Haiku 5.5, only for loose phrasing
```

Everything goes through `voice_bridge.py` on the PC that runs the arm. It only ever calls four MCP tools: stop, go to a view, status, list views.

The laptop dashboard also routes [phone and robot recording requests](LAPTOP_DASHBOARD.md#recording-and-speech) via the local gateway and MCP. This path requires a paired phone for speech and phone recording. For robot recording it additionally requires a visible dashboard tab and live robot feed.

## Start it

On the Mac with the arm, after Station, `capture_server.py` and `mcp_server.py` are running (see the README):

```
.venv/bin/python voice_bridge.py
```

The log should end with `free phrasing: Claude (claude-haiku-5-5)`. Then open **http://127.0.0.1:8210/** in Chrome or Safari on the same Mac and allow the microphone.

The API key is read from `ANTHROPIC_API_KEY`, in the environment or in a file `.env` next to `voice_bridge.py` (`ANTHROPIC_API_KEY=sk-ant-...`). `.env` is gitignored: every computer needs its own. Without a key the bridge still runs, with the fixed phrases only. `--llm off` turns the Claude step off, `--llm-model` picks another model.

## The PC voice page

| | |
|---|---|
| Tap to talk | Click the microphone or press Space, say one request, pause. Everything said is sent. |
| Hands-free | Always listening; acts only after "Hey Claude" (the name can be changed on the page). "Hey Claude", a pause, then the request within 8 s also works. |
| Stop | A spoken "stop", "halt", "freeze" or "abort" is sent as soon as the word is heard, without the wake name. Esc and the red button do the same. |
| Typing | The text box sends a request without the microphone. |

The chips at the top show whether the arm's MCP server answers, whether free phrasing is on, and the microphone state. Speech recognition is the browser's own (Chrome sends the audio to Google, Safari uses Apple's) and needs the Internet. Firefox has none. "Claude" is often heard as "cloud" or "clawed"; the page accepts those.

## What you can say

Handled on the PC without any network call, in this order:

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
