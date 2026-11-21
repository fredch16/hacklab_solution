# Slice 05: spoken arm requests through the dashboard-side MCP bridge (2026-11-21)

SLICE: dashboard → `voice_bridge.py` → camera-arm MCP / branch `dashboard-mcp-bridge`

## What works

A live final that the recording grammar does not take is posted from the browser to `voice_bridge.py` on the same PC. The service maps it to an MCP tool and the result is shown beside the transcript and in the voice popup:

| Said | Tool | Shown |
|---|---|---|
| "stop", "halt", "wait", "hold on" (anywhere in the phrase, checked first) | `stop` | Stopped the arm |
| "go to top", "move to the wide view", "top" | `go_to_view` (checked views only) | Moving to top |
| "go to left" while `left` is unchecked | none | ✕ left isn't checked on the real arm yet |
| "where are you" | `get_status` | At top / Moving to wide (40%) |
| "list views" | `list_views` | Views: home, right, top, wide |
| "stop recording" that failed the acoustic check, "tell me a joke" | none | No matching command |

Owner decisions (21 Nov, relayed): spoken moves run at once, no confirmation click; local grammar only, no LLM.

## Tested on

Ubuntu laptop (Linux 6.8), headless Chromium 131 (Playwright 1.49.1) and `dev/server.mjs` as the mock phone, `capture_server.py --sim` on 8891, `mcp_server.py` on 8201, bridge on 8211.

- `.venv/bin/python tests/test_voice_bridge.py`: ALL OK. Grammar and origin rules, then HTTP → MCP → capture_server → SimArm: preflight with `Access-Control-Allow-Private-Network`, 403 for a foreign origin, 415 for non-JSON, a real `go_to_view` arriving, `stop` mid-move, unchecked view refused, recording text ignored, duplicate utterance returns the first result, MCP down → `ARM_UNREACHABLE`.
- `node --test test/*.test.mjs` in `apps/dashboard`: 26 pass (2 new: `armRequest`, arm badge/popup).
- Chromium against the compiled bundle and the sim stack: popups and badges for every row above; spoken stop mid-move → "Stopped the arm" in 0.3 s; `?arm=http://127.0.0.1:9` → "Arm command service not reachable"; `?arm=off` → unmatched as before.
- Read-only against the real MCP server (port 8200): "where are you" → "At top", "list views" → "Views: home, left, right, top, wide". The real arm was not moved.

## Not tested

- NOT TESTED: a spoken move or stop on the real arm.
- NOT TESTED: Pixel + paired PC. The phone CSP change (`connect-src` adds `http://127.0.0.1:* http://localhost:*`) needs a new APK; Flutter is not installed on this laptop, so `flutter test` and the APK build were not run. Chrome may ask once for "local network access" when the LAN https page calls 127.0.0.1; that prompt was not seen in the localhost-only test.

## Risks

- Any process on the PC can call the bridge without a token, as it can already call `capture_server.py` and `mcp_server.py`; browsers are limited to https pages on private or loopback addresses.
- Once on the sim, a `stop` sent right after a `go_to_view` got 500 from `capture_server` ("could not confirm the hold") after 5 s; it did not repeat in later runs. That is the motion layer's, not the bridge's, but worth watching on the real arm.

## Run

```
.venv/bin/python voice_bridge.py            # next to mcp_server.py (8200); listens on 127.0.0.1:8210
```
Open the dashboard from the phone in a browser on the same PC; no setting needed (`?arm=<url>` to point elsewhere, `?arm=off` to disable).
