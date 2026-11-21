# Product specification (MVP)

## User intent

Build a **self-contained mobile field camera + local voice interface**, controlled both from the **phone itself** and from a polished **browser dashboard** on a nearby computer. A robot/gimbal may physically carry the phone later, but camera-driven movement and robot integration are not Phase 1. The owner prefers a complete, accurate, independently testable slice-by-slice engineering process and wants agents to ask for product decisions rather than guessing.

## Locked requirements

1. **Mobile-first:** Flutter app for Android and iOS, Pixel 10 Pro XL first validation target. The app stays foreground with its **screen active** and shows preview, recording/voice/connection status, controls and transcripts.
2. **Phone is the compute endpoint:** all camera access, recording, wake-word detection, VAD, speech recognition and model assets are local to the smartphone.
3. **One camera:** use the phone camera only. Robot cameras are not required. Do not attempt multi-camera fusion or cross-camera calibration.
4. **Two video outputs:** compressed low-resolution WebRTC for live CV/monitoring and high-quality original recording directly to phone storage. Never transfer the recording-quality path via WebRTC.
5. **One microphone input:** local recording audio and local voice inference should share a coordinated capture path; do not require simultaneous independent mic ownership.
6. **Browser-only PC dashboard:** no mandatory PC service, compute, backend, Python or installation. The phone hosts session/signaling/status and file-transfer endpoints itself while foreground.
7. **Local-only:** no Internet required during runtime; no external signaling/STUN/TURN, CDN, analytics or cloud transcription. Peer-to-peer on one trusted LAN or direct local link.
8. **Post-shoot original file transfer:** secure, integrity-checked transfer to PC after finalization; no automatic deletion of source. Basic transfer must work without SMB/FTP installation.
9. **Professional interface:** polished, purpose-built camera/voice control system, responsive to phone portrait/landscape and desktop, with truthful statuses and errors.
10. **Complete vertical slices:** each feature independently testable and documented; unresolved user-facing choices are asked before implementation.

## Operational lifecycle

`Idle -> Permissions/Pairing -> Ready -> Live (video and passive wake listening) -> Recording (optional) -> Finalizing -> Ready with saved recording -> Transfer (optional)`.

Phone may stream and listen with **recording off**. Starting/stopping recording must not reset remote WebRTC session or offline speech pipeline. Viewer disconnect must not stop local recording or local voice inference. Local camera preview must remain visible even when no computer is connected.

## Target profiles (proposals, NOT measured guarantees)

| Pipeline | Initial target | Adaptation |
|---|---|---|
| Phone local preview | Preview synced with camera; smooth UI | Lower UI draw cost rather than compromise recording |
| Remote WebRTC vision | 640×360, 30 FPS H.264, ~0.5–1.5 Mbps | 720p30 optional; 60 FPS only after hardware verification |
| Original recording | HEVC/H.265, try 4K30 + audio | Expose real device-supported options; fall back 1080p30; offer 4K60 only when simultaneous test passes |
| Wake-word engine | Continuous while in active foreground session | Local inference with short frame windows, CPU-aware |
| Transcription | Start after wake word, VAD endpoint, partial/final text | One configurable offline model; fallback if thermal or mic interrupted |
| PC viewer | One paired browser, single WebRTC connection | More viewers / SFU *not* in MVP |
| File transfer | Original size, HTTPS with HTTP Range and SHA-256 | Resume browser where supported; avoid large RAM buffering |

The application must **display the negotiated codec, actual FPS and current resolution**, rather than showing aspirational 60 FPS/4K badges. Performance budgets are measurement goals (e.g. smooth preview and acceptable round-trip latency) until Pixel tests establish baselines; never promise fixed numbers.

## Must-have mobile screens

- **Capture** (default): large camera view, record/start/stop, elapsed time, local file indicator, stream indicator, mic/wake status, compact latest transcript, connected viewer count, battery/temperature/storage and local network state. Keep screen awake in active session.
- **Voice**: ready/listening/activated/transcribing/finished status; wake detector confidence where meaningful, live partial/final text, transcript list, test/mute controls, selected model/language.
- **Library / Transfer**: recorded files with duration/resolution/size/created time and transfer status, explicit original download/share controls and checksums where computed.
- **Connection / Settings**: LAN URL/QR pairing, paired device sessions/revoke, camera/mic permissions, profile selection, privacy status, diagnostics and offline model management.

## PC browser dashboard

Large live video with real transport metrics, recording status/control (only after user has authorized remote actions), live voice activity/transcripts and local file library/transfer. Disconnected, no permissions, overheated, full storage, no model and pairing errors have dedicated visible states.

## Not in Phase 1

- Robot motion, servo or gimbal control, PID, ROS, VLA/LLM assistants, voice-controlled actions, wake word responding with speech, cloud sync, multi-camera calibration, deep tracking, background recording/listening, continuous livestream archive, public Internet access, multi-user SFU and remotely sharing raw microphone feed.

## Two important distinctions

- **No extra robot camera calibration** does *not* mean precise camera-to-robot geometry becomes unnecessary later. A single phone camera used for spatial positioning still needs calibration to the robot frame (and likely intrinsics for precise 3D tasks).
- **Flutter compatibility** does not imply identical native capture capabilities or encoder profiles. Android and iOS share domain/UI/service contracts; native media adapters and test plans differ.

## Product decisions agents must ask before relevant slice

See `docs/DECISIONS_AND_QUESTIONS.md` for wake phrase, speech language(s), recording control rules, transfer automation and recording profile. An undecided item must not block an unrelated earlier slice.
