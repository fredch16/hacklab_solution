# HackLab handoff — 22 November 2026

This is the current operating guide for the next agent. The laptop hosts the HackLab dashboard, the Pixel provides its camera and offline speech recognition, and Station owns the robot’s USB camera and serial bus. Earlier designs and proposed autonomous framing are reference material. [Architecture](ARCHITECTURE.md) · [Camera director research plan](../ai_camera_director_beta_research_plan.md)

## Where to work

| Area | Source and ownership |
|---|---|
| Laptop dashboard gateway | `laptop_dashboard.py` serves built assets, checks loopback origins, pairs with the phone, relays phone events, and owns laptop clip files. |
| Android app | `dashboard/apps/mobile` owns native phone camera/mic capture, native recording, foreground lifecycle, pairing, and on-device KWS/VAD/ASR. `SessionController` controls voice eligibility. |
| Dashboard app | `dashboard/apps/dashboard/src` contains the TypeScript UI and exact voice grammar. `npm run build` writes laptop assets to `dist/`. |
| Phone discovery | `DiscoveryAdvertiser.kt` advertises the Android session. `phone_discovery.py` resolves local DNS-SD. `PairingService` handles QR proof on Android. |
| Robot USB feed and arm controls | Station owns USB. `capture_server.py` exposes arm state, motion controls, and the selected camera feed. `motion.py` and `fk.py` enforce planning checks. |
| Voice and MCP | `voice_bridge.py` handles fallback speech routing and optional Claude classification. `mcp_server.py` exposes the arm and recording tools. `claude_config.py` shares startup key loading with `director.py`. |

The phone’s bundled dashboard is still available during the laptop cutover. Only the explicit `node build.mjs --phone` command refreshes those compatibility assets. Rebuild the APK for native phone changes. For laptop-only UI changes, rebuild `dist/` and refresh the laptop page. [Dashboard instructions](../dashboard/AGENTS.md) · [Mobile build and test status](../dashboard/docs/STATUS.md)

## Start and check the system

| Service | Default local endpoint | What to check |
|---|---|---|
| Station | [Web UI](http://127.0.0.1:8889/) and TCP port 8888 | The intended follower has fresh joint data and the USB camera has frames. |
| Capture and arm control | [Camera](http://127.0.0.1:8890/) · [Control](http://127.0.0.1:8890/control) | `GET /api/state` reports `connected`, live `camera_state`, and any movement blockers. |
| Camera-arm MCP | [MCP](http://127.0.0.1:8200/mcp) | Use an MCP client to list tools and call the read-only status tool. |
| Voice bridge | [Voice and Claude settings](http://127.0.0.1:8210/) | `GET /api/v1/health` reports the MCP URL, Claude model, and whether a session override is active. |
| Laptop dashboard | [Dashboard](http://127.0.0.1:8300/) | `/laptop/discovery` lists discovery candidates. `/api/v1/session` reports pairing. An authenticated `/api/v1/snapshot` shows the phone’s camera, voice and viewer state. |

Follow the [laptop startup guide](LAPTOP_DASHBOARD.md#start-the-stack) and [hardware setup](HARDWARE.md#station). On the current lab laptop, a second MCP on 8201 and voice bridge on 8211 were used alongside the originals. The laptop dashboard used the 8211 bridge. Inspect the running process arguments and dashboard **Claude API key settings** link before assuming which bridge is active.

Keep Station and arm control loopback-bound. Station uses different permissions for the serial node and the camera’s raw USB node. If the motor bus works but `camera_state.live` is false, check the Station logs and the current camera path from `lsusb`. A successful V4L2 frame can coexist with a denied raw USB open in Station. In the lab, an approved camera ACL and a restart of Station and the arm capture service made the robot feed live. Recheck that the arm is idle and robot recording has ended before restarting. The saved follower calibration is already in the repo. Use the documented import procedure and compare its ranges. [Calibration on another computer](HARDWARE.md#running-the-same-arms-from-another-computer)

## Pairing and voice lifecycle

1. Open Local Vision on the Pixel and start a foreground session on the same Wi-Fi as the laptop. The dashboard automatically selects a single discovered phone. Select the intended phone if two or more appear.
2. On the phone, open **Link → Scan to pair**, scan the QR on the laptop, and tap **Approve**. DNS-SD offers are provisional. The laptop pins the offered HTTPS certificate, sends only the hash of its generated QR value, and requires the scanned value back before accepting a session cookie.
3. With microphone permission and an authenticated dashboard event socket, the phone starts wake detection immediately. **Start video** is only needed for the phone preview. Stopping phone video leaves voice active. Disconnecting, revoking the viewer, backgrounding the phone, or ending the session stops listening. A reconnect resumes fresh voice processing.
4. On a Wi-Fi network that blocks multicast, use **Connect manually** with the HTTPS address and certificate fingerprint from **Link**. Use **Find phone again** if an unpaired phone has moved networks. After a gateway restart or APK reinstall, start Local Vision and expect to pair again.

The phone video stays direct, vision-only WebRTC. Control and final transcripts use the paired HTTPS/WSS relay. A successful pairing replaces the previous viewer. Snapshot transcripts are for display. A relay response from a stale phone session cannot send a delayed recording command to a new phone. [Phone wire protocol](../dashboard/packages/protocol/README.md)

## Recording, voice routing and lift-over

| Action | Behavior |
|---|---|
| **Record phone camera** or “start camera recording” | The phone saves its native high-quality master with audio. Explicit phone recording phrases also target the phone. |
| **Record robot camera** or “start robot recording” | The laptop writes video-only MKV clips from the Station feed to [`Camera recordings/`](LAPTOP_DASHBOARD.md#recording-and-speech). The folder is ignored by Git. |
| “Stop robot recording” or “stop recording the robot camera” | A complete, affirmative final routes directly to the robot recorder for the requesting tab. The stop works even without a matching acoustic hint. |
| “Stop recording” | Targets the phone. Exact voice starts and phone stops retain the acoustic agreement checks. |
| **Stop arm** | Calls the existing arm control service directly. This controls arm motion independently of camera recording, phone connectivity and Claude. |

Robot recording requires the tab to be visible and the robot camera to be live and in view. Closing, refreshing, hiding or scrolling away from the view, or losing the feed, finalizes that clip. If the tab disappears without a clean close, its viewing lease expires after about three seconds. The latest completed clip has a download link. Reopening the dashboard does not resume a clip. The recorders are independent.

The dashboard routes known recording commands locally. Other live commands can reach the laptop voice bridge, which uses its existing grammar and optional Claude classification for eligible free phrasing. MCP’s `record_camera(camera, action)` uses the same laptop recorder API. The gateway applies recording ownership and freshness checks even for MCP calls. Treat the preexisting motion controller as the only writer to the arm. [Voice command behavior](VOICE.md) · [MCP tools](MCP.md)

In **Motion settings**, **Lift over when straight would dip** is always on. The setting is on at startup and after **Back to defaults**. The settings API rejects attempts to turn it off. Existing geometry checks still decide whether an arc is usable. If neither the straight path nor a lifted path passes the checks, the route uses transit. Turning off **Go straight from view to view** uses transit for every move. [Motion planning](MOTION_OPT.md)

## Claude credentials

The tracked [`.env.example`](../.env.example) contains a placeholder. Copy it to a local `.env` if needed and set `CLAUDE_API_KEY` in your editor. Git ignores the local file. Use mode `0600` for that file. The voice bridge and CLI director load it on startup. For the bridge, a saved dashboard key overrides the startup key for the current process. Otherwise an exported `CLAUDE_API_KEY` takes priority over the file. The legacy Anthropic variable names are fallbacks.

Use **Remove session key** to return to the startup credential. A bridge restart clears the override and reloads the file. Verify through the health status or a dummy key in tests. Keep credentials out of command output and diagnostics. Claude is optional, and eligible free phrasing sends transcript text and view context to Anthropic. [Credential setup](VOICE.md#configure-the-claude-key)

## Checks and acceptance

From the HackLab root:

```bash
uv sync --locked
(cd dashboard/apps/dashboard && npm ci && npm run build && npm test)
uv run python -m unittest discover -s tests -p test_laptop_dashboard.py
uv run python tests/test_voice_bridge.py
uv run python tests/test_mcp_sim.py
uv run python tests/test_motion_opt.py
uv run python tests/test_server_sim.py
```

Use the [mobile guide](../dashboard/docs/STATUS.md#build--run-linux-dev-machine) for Flutter analysis, tests, APK build and install. The extended capture-server simulator suite takes about four minutes. Its stale-data and unconfirmed-hold cases deliberately produce warning lines, so check the final test result.

| Observed | Evidence |
|---|---|
| Real Pixel 10 Pro XL on Android 17 discovered over Wi-Fi and paired through QR without manual address entry | [Phone discovery](evidence/phone-discovery-2026-11-22.md) |
| Real phone sent wake and final events with its camera off. Refresh restored the listener after a reported miss. | [Voice lifecycle](evidence/pairing-starts-voice-2026-11-22.md) · [Recording follow-up](evidence/laptop-dashboard-2026-11-22.md) |
| Real Station feed reached the dashboard at 960×720. A 39.32 s video-only robot clip was verified and could still be downloaded after being moved to the named folder. | [Laptop dashboard evidence](evidence/laptop-dashboard-2026-11-22.md) |
| Exact robot-stop regression passed in Chromium and the recorder fixture. Claude startup, placeholder handling and dashboard override passed with dummy credentials. | [Spoken stop evidence](evidence/laptop-dashboard-2026-11-22.md#spoken-robot-stop-follow-up) · [Credential tests](../tests/test_voice_bridge.py) |
| Live camera controls report lift-over enabled and locked. Chromium and the simulator verified reset and toggle rejection. Motion optimization passed 1,716 checks, and the capture server passed 1,543 simulator checks. | [Motion tests](../tests/test_motion_opt.py) · [Server tests](../tests/test_server_sim.py) |

**Owner verification pending:** a complete dual-camera recording and disconnect run on the new laptop page, a repeat of the spoken robot-stop command on the Pixel, and movement checks for any newly chosen lifted path. The cause of the earlier intermittent wake miss was not isolated. Use the phone voice state and ADB wake event timestamps to distinguish a missed wake from an unrecognized command, and compare with the same phrase during and after a robot clip. No iOS behavior has been validated.

Keep the fallback phone-served UI through hardware acceptance. Retire it from the HackLab APK only after the owner verifies the laptop workflow. Record new results in the evidence files and keep this handoff in sync with changes to startup, credentials or route authority.
