# Laptop dashboard

Open [http://127.0.0.1:8300/](http://127.0.0.1:8300/) on the computer connected to the arm. The laptop serves the existing TypeScript dashboard and its control APIs. Web UI changes need a rebuild of the dashboard assets and a browser reload. They do not need an APK build.

## Start the stack

From the HackLab checkout:

```bash
uv sync --locked
(cd dashboard/apps/dashboard && npm ci && npm run build)
```

Install FFmpeg on the laptop. Follow [Station and calibration setup](HARDWARE.md#station) before starting the arm. Start these services in separate terminals:

```bash
.venv/bin/python capture_server.py --bus 5B79016329
.venv/bin/python mcp_server.py
.venv/bin/python voice_bridge.py
.venv/bin/python laptop_dashboard.py
```

All four services listen on loopback by default. The dashboard's **Stop arm** uses the existing control service directly, and the **Arm controls** link opens its operator page. **Claude API key settings** opens the configured voice bridge's local settings page. You can run a second MCP and voice bridge on alternate ports without restarting a running instance or losing its in-memory Claude key:

```bash
.venv/bin/python mcp_server.py --port 8201
.venv/bin/python voice_bridge.py --port 8211 --mcp-url http://127.0.0.1:8201/mcp
.venv/bin/python laptop_dashboard.py --voice-bridge http://127.0.0.1:8211
```

If the USB feed is unavailable, check that the real arm server's `/api/state` reports `camera_state.live: true`. A one-frame V4L2 check can work while Station lacks access to the camera's raw USB node. Check the Station log for device permissions and use the current bus/device path shown by `lsusb`.

## Pair the phone

1. Connect the phone and laptop to the same Wi-Fi. Open Local Vision on the Pixel and tap **Start session**.
2. Open the laptop dashboard. A single discovered phone is selected automatically. When multiple phones are visible, select the one you want.
3. On the phone, open **Link → Scan to pair**, scan the laptop QR code, then tap **Approve**. Keep Local Vision in the foreground. Wake detection and offline transcription start as soon as the paired dashboard connects and microphone permission is granted. Click **Start video** when you want the phone camera feed.
4. If discovery is blocked on the network, open **Connect manually** and enter the HTTPS address and SHA-256 fingerprint from the phone’s **Link** screen. **Find phone again** refreshes discovery if an automatically selected phone changes networks.

Android advertises the active foreground session with DNS-SD on `_localvision._tcp.local.`, including the HTTPS port and the certificate hash. The laptop treats the advertisement as an untrusted connection hint. It generates a one-use random value, puts it in the QR shown on the laptop, and sends only its SHA-256 hash over the network before approval. After the scan and approval, the laptop accepts the pairing token only when the phone returns that value on its pinned TLS connection. Six-digit pairing remains available after manually verifying a certificate fingerprint.

A short phone connection loss pauses listening and leaves an ongoing master recording on the phone. Listening resumes when the paired dashboard reconnects. Reconnect restores history for display without re-executing commands. Restarting the laptop gateway requires pairing again.

## Recording and speech

| Request | Camera and storage |
|---|---|
| “Start recording,” “start camera recording,” “start phone recording,” or “phone camera recording” | Phone records at its existing quality with audio. The master stays on the phone. |
| “Start robot recording” or “robot camera recording” | Laptop saves video-only from the existing Station preview stream. |
| “Stop recording,” “stop camera recording,” or “stop phone recording” | Stops the phone recorder. |
| “Stop robot recording” or “stop robot camera recording” | Stops the laptop recorder. |

The two recorders are independent. Use **Record phone camera** and **Record robot camera**, or say the wake phrase followed by a request. Named recording commands use the phone's acoustic cross-check. With Claude enabled, the voice bridge can classify loose requests such as “could you film this with the robot camera?” and invoke MCP's `record_camera(camera, action)` tool. Recording without a named camera defaults to the phone. A robot request never falls back to the phone.

The robot recording belongs to the dashboard tab displaying the live robot feed. Hiding, closing or refreshing that tab, scrolling the feed out of view, or losing the feed finalizes the clip. An interrupted tab that cannot close its connection loses its viewing lease after about three seconds. Opening a tab never resumes a recording. Completed Matroska clips are in `Camera recordings/` in the HackLab checkout by default, with a download link for the latest clip. Only one robot clip records at a time.

## Architecture and verification

- `laptop_dashboard.py` serves the UI and proxies the allowlisted phone APIs and events over pinned TLS. Pairing credentials live in the laptop process. The phone video goes directly from the Pixel to the browser via WebRTC. The microphone and speech inference remain on the phone.
- Station is the single USB camera owner. The gateway consumes `capture_server.py`'s JPEG feed and writes the same frames to FFmpeg during a valid robot recording. The laptop owns finalizing and downloading clips.
- Recording control reaches the phone's authenticated command relay or the laptop's tab-scoped robot recorder. The MCP tool invokes the same gateway API. Arm movement still goes through the existing `capture_server.py` controls and safety checks.
- The HackLab APK still includes the legacy phone-served UI for owner acceptance and rollback. Once the laptop flow passes the real-device checks, retire that UI from the HackLab APK build. Dashboard development now builds to `dist/`; an explicit `node build.mjs --phone` is available for the compatibility UI.
- See [validation evidence](evidence/laptop-dashboard-2026-11-22.md) for the automated checks and physical acceptance steps.
