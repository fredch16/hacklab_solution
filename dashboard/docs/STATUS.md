# Project status — Local Vision Console (demo)

_Last updated: 2026-11-21. Keep this file current: it is the hand-off for any agent or human picking up the work._

## Working mode (owner)
- One agent does everything itself — **no subagents**.
- The **owner tests on real hardware** and reports results. Agents build, unit-test, install the debug APK and give short test steps; they do not drive the phone UI or verify via screenshots.
- This is a **demo**: best-effort features, no configurability/benchmarking beyond what the demo needs.

## What works today (verified on Pixel 10 Pro XL, Android 17)
| Area | State |
|---|---|
| Slice 0 — phone app shell, embedded HTTPS/WSS server, pairing | ✅ QR pairing (browser shows QR, phone scans + Approve), 6-digit code fallback, revoke/replace, live device status (battery/charging instant, thermal, storage, network) |
| Slice 1 — camera → WebRTC → browser | ✅ Camera starts when the browser clicks **Start video**; H.264 hardware encoder, 640×360 ~25–30 fps, ~0.6–1.3 Mbps, RTT ~26–31 ms over the phone hotspot |
| Slice 2 — local recording while streaming | ✅ Record/Stop from browser (confirm on stop) and phone; best effort 1080p ~60 fps HEVC + AAC 48 kHz stereo → `Movies/LocalVision` |
| Slices 3–4 — wake word + command transcription | Fixed **Hey** prefix with a name entered in dashboard Settings. Dedicated offline Zipformer spotter with local pronunciation for new names. The Pixel clip test passed the Alexa-versus-Google demo criterion. The Android voice path uses final-pass Whisper base.en and verifies recording commands with a separate phoneme check. [Command and room-noise replay evidence](evidence/slice-04-command-accuracy-2026-11-21.md). See [wake regression evidence](evidence/slice-03-wake-2026-11-21.md). **Owner reported the previous build worked well enough for the demo.** Live voice after the dashboard routing change is pending. [Routing checks and owner test steps](evidence/slice-04-dashboard-voice-routing-2026-11-21.md). |
| Voice commands | Paired browser routes live, silence-ended finals with matching acoustic text. The phone accepts the normal authenticated recording RPC. Static demo rules: "start/begin (the) recording", "record" → start; "stop/end (the) recording" → stop |
| Dashboard voice overlay | ✅ Assistant-style overlay (bottom sheet, animated bars/waveform, glowing light bar, live text, command result chip), fixed a CSS bug that hid it on desktop |
| Slice 5 — original file download over LAN | ⏳ Not started |
| Slice 6 — iOS | ⏳ Not started (needs Mac + iPhone) |

## Owner decisions (binding)
- HTTPS: **self-signed** certificate, browser click-through (ADR 0001). Cellular addresses excluded from cert/URLs.
- Pairing: **QR shown in browser, scanned by phone**, code as fallback.
- Camera starts only on browser **Start video**; stops when video stops.
- Recording: started/stopped from the **web dashboard** (and phone); separate from Start video; browser may start and stop; **always with audio**; saved to **Movies/LocalVision**; quality best effort (no settings UI).
- Voice: fixed **Hey** plus a name **typed in the dashboard** (persisted on phone); **English only**; command ends after **~1 s silence**, 15 s cap, 5 s no-speech; transcripts **session-only, in memory**; listening **starts with the video stream**; must run simultaneously with streaming and recording; no audio leaves the phone.
- Dashboard: wake word shows a bold assistant-style popup (like "Hey Google"/Alexa).
- Commits: author **Ajay Chodankar <achodankar28@gmail.com>** (repo-local git config).

## Architecture (as built)
- **Phone (Flutter + Kotlin)**: `apps/mobile`. Dart: session controller, embedded `dart:io` HTTPS/WSS server (`lib/features/connectivity/`), signaling relay, UI tabs Capture/Voice/Files/Link. Kotlin (`android/app/src/main/kotlin/dev/localvision/local_vision/`): `CameraSession` (Camera2 single owner: preview texture + 640×360 YUV for WebRTC/QR + encoder surface), `VisionStream` (native libwebrtc `io.github.webrtc-sdk:android`, network monitor disabled so the hotspot interface is used, SDP filtered to private IPv4), `MasterRecorder`/`VideoEncoder`/`AacEncoder`, `AudioCaptureBus` (only mic owner, fan-out), `VoiceEngine` (sherpa-onnx, models in assets), `QrScanner` (ZXing), `RecordingStore` (MediaStore).
- **Dashboard**: `apps/dashboard` (TypeScript, no framework, no runtime deps; vendored MIT qrcode-generator. Owns spoken recording intent and command results). `npm run build` writes flat files into `apps/mobile/assets/dashboard/`, served by the phone.
- **Contract**: `packages/protocol/README.md` (locked; later sections supersede earlier ones where stated).
- **ADRs**: `docs/adr/0001` self-signed HTTPS · `0002` single camera owner + native WebRTC (+ hotspot notes) · `0003` recording pipeline (Camera2 spike result) · `0004` voice pipeline.
- **Evidence**: `docs/evidence/slice-0{0,1,2}-2026-11-21.md`.

## Build & run (Linux dev machine)
Toolchain lives in `~/Android` (no root): JDK 21 `~/Android/jdk`, SDK `~/Android/Sdk`, Flutter 3.50.1 `~/Android/flutter`; env vars in `~/.bashrc`.
```bash
cd apps/dashboard && npm install && npm run build && npm test     # dashboard → phone assets
cd apps/mobile && flutter analyze && flutter test
flutter build apk --debug --target-platform android-arm64
adb install -r build/app/outputs/flutter-apk/app-debug.apk        # never uninstall / clear data (owner data)
adb logcat -s LVC                                                  # app log tag
```
Kotlin unit tests: `cd apps/mobile/android && ./gradlew :app:testDebugUnitTest`.

## Network setup used for testing
Phone joins a public Wi-Fi (client isolation — PC↔phone impossible there) **and** runs its **hotspot**; the PC is on the hotspot (phone `10.181.249.119`, PC `10.181.249.x`). Dashboard: `https://10.181.249.119:8443`. adb over wireless debugging (mDNS serial `adb-59110DLCQ0037P-…`).

## Known issues / open items
- Voice false wake rate, latency and repeated wake under load — owner re-test pending (see table).
- Not owner-tested yet: revoke flow in browser, WAN-disabled offline run, PC Wi-Fi drop/reconnect, camera-permission-denied path, A/V sync (clap test), low-storage auto-stop.
- Certificate regenerates when the phone's Wi-Fi/hotspot IP set changes (browser warning again).
- `minSdk` 30 (Android 11+). APK ~319 MB debug (models + WebRTC).
- Dev-only: `apps/dashboard/dev/server.mjs` is a mock phone for UI work; not shipped.

## Next
1. Owner re-tests voice after dashboard routing and verifies reconnect does not replay commands.
2. Slice 5: download finalized recordings over LAN with SHA-256 verification.
3. Dashboard-side MCP command service `voice_bridge.py` (hackathon repo root): built and tested on the simulated arm; free phrasing is interpreted by Claude when a key is supplied on the PC. The dashboard’s **Claude API key settings** link opens the bridge’s local page, where the operator can set or remove an in-memory key while the bridge is running. See [handoff](ARCHITECTURE.md#dashboard-command-routing-and-mcp-handoff) and [evidence](evidence/slice-05-arm-voice-bridge-2026-11-21.md).
