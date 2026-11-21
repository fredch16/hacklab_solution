# Architecture: phone-first, one camera, one microphone

## Context diagram

```text
                         SAME LOCAL NETWORK (NO WAN)
┌──────────────────────────────── PHONE ────────────────────────────────┐
│ Flutter operator UI: preview, Record, Voice, Library, Connection      │
│                                                                       │
│ Native camera owner (Android: CameraX/Camera2, iOS: AVFoundation)     │
│   sensor -> controlled capture pipeline -> phone preview             │
│                  ├─ master video encoder + muxed audio -> storage    │
│                  └─ low-res low-bitrate WebRTC video encoder ────────┼──┐
│                                                                       │  │
│ Native audio capture owner: PCM tap ---------------------------------│  │
│                  ├─ recording audio muxer                             │  │
│                  └─ local KWS -> VAD -> ASR -> transcript event       │  │
│                                                                       │  │
│ Embedded authenticated HTTPS/WSS server: pairing, signaling, status ─┼──┤
│ Recording library + checked download endpoint -----------------------┼──┤
└───────────────────────────────────────────────────────────────────────┘  │
                                                                           ▼
                                                           PC browser dashboard
                                                      WebRTC peer, event socket,
                                                     controls, verified downloads
```

## Clear separation of concerns

- **Flutter layer (shared):** pages/widgets, session state, settings, event reducers, transfer UI and standardized platform interfaces. Render real camera preview surfaces provided by native adapter.
- **Native media adapters:** the single camera/mic ownership; native encoders/muxers; device-specific capability probing; frame fan-out to preview, compressed WebRTC and master recorder; handle orientation, timestamps, A/V synchronization, foreground lifecycle and thermal constraints.
- **WebRTC adapter:** send one low-resolution video track to a paired browser. Use direct LAN ICE candidates, DTLS-SRTP, H.264 where negotiated/supported. No camera recording/master through WebRTC. Do not assume `flutter_webrtc`'s simple `getUserMedia` track can be mirrored into a **second simultaneously high-quality native recording**; first test its native track/capturer integration and use custom capture source bridging if needed.
- **Local speech engine:** single PCM acquisition path into on-device KWS/VAD/ASR model library. Voice events flow to Flutter state and a local authenticated event socket. This is **not** speech streamed back to the PC for inference.
- **Embedded device server:** static dashboard assets, HTTPS/WSS authenticated API, pairing flow, WebRTC SDP/ICE signaling, real-time event stream, file manifest/download. Hosted by foreground app. No Node/Python process needed on PC.
- **Web dashboard:** static HTML/CSS/TypeScript or React client, built into app assets and served by phone, renders browser-side WebRTC track, telemetry, voice transcripts, file library, and interprets final transcripts. The browser performs no speech inference.

## Dashboard command routing and MCP handoff

1. **Phone capture and speech.** The phone shares one mic input between the master recording and offline KWS/VAD/ASR. Its paired WSS sends a live `voice.transcript.final` with the Whisper text and optional `acoustic_text` from the constrained phoneme decoder. Audio stays on the phone. No native or Dart transcript handler starts/stops recording.
2. **Dashboard intent.** `apps/dashboard/src/main.ts` receives the live event for the active paired session and calls `planVoiceCommand` in `apps/dashboard/src/voice.ts`. A complete recording request that agrees with the second decode becomes `recording.start` or `recording.stop`. Pending requests and existing results are correlated by utterance. Snapshots restore history for display only. Lost connections never queue or replay voice requests.
3. **Phone recorder.** The browser sends the ordinary paired command with request ID `voice:<utterance_id>`. The phone handles it through `SignalingRelay`, checks recorder state and prerequisites, and returns `command.result`. The browser displays the result beside the transcript. Manual phone and dashboard controls remain available.
4. **Arm command service.** For a live final the recording grammar does not take, `main.ts` posts only `{session_id, utterance_id, text}` to the dashboard-side command service `voice_bridge.py` (hackathon repo root) on the PC that runs the browser, default `http://127.0.0.1:8210/api/v1/voice-command` (`?arm=<url>` overrides, `?arm=off` disables; remembered per browser). The service is a Streamable HTTP client of `mcp_server.py` at `http://127.0.0.1:8200/mcp` and owns interpretation, the MCP connection and tool policy; the phone and the static bundle hold no MCP code or credentials. Its local grammar comes first and needs no network: `stop/halt/wait/…` → `stop()`, checked first; `go to/move to/show … <view>` → `go_to_view()`, only for views checked on the real arm; `where are you`/`status` → `get_status()`; `list views` → `list_views()`. Owner request 21 Nov: text the grammar cannot place ("yo, show me the thing from da top") goes to Claude (Haiku 5.5, Anthropic API), which picks one of the same four actions or none; the service then applies the same checked-views rule. This step runs on the PC only, when `ANTHROPIC_API_KEY` is set (`--llm off` disables it), and sends the transcript text plus view names and descriptions: no audio, and nothing from the phone beyond the text the dashboard already forwards. Stop words and anything mentioning "record" never reach it; if the call fails or takes over 6 s, the grammar's answer stands. Owner decision 21 Nov: a spoken move runs at once without a confirmation click. The reply (`{ok, command: "arm.*"|"none", error, message}`) is shown beside the transcript and in the voice popup. Each utterance runs at most once (browser and service both dedupe); nothing is retried or replayed. The service accepts browser calls only from https origins on private/loopback addresses (CORS + Private Network Access preflight); the phone's CSP allows `connect-src http://127.0.0.1:* http://localhost:*` for this.

Recording-related text that fails the acoustic check stays rejected: the command service ignores anything mentioning "record". Only live finals with a successful endpoint may be considered for execution. Define authorization and confirmation for future tools before wiring them up. Keep robot motion behind the hackathon control server's existing tool and motion checks.

## Critical technical gates before choosing adapters

1. **Native capture combinatorics:** enumerate Camera2 mandatory stream combinations and available encoder modes on Pixel; investigate CameraX `VideoCapture` + preview + analysis interoperability. Check actual native WebRTC source accepts external frames. On iOS use AVFoundation `AVCaptureSession` with supported outputs, verified with `canAddOutput`. APIs permit multiple outputs in general; **supported simultaneous quality is device specific**.
2. **Single microphone owner:** recording-with-audio and continuous wake-word + ASR may conflict if separate plugins acquire mic. Choose a native single PCM tap, with separate recording/recognition buffers. If preserving processed vs raw audio quality requires different taps, document routing and verify it. Keep drift-synchronized timestamps.
3. **Hardware-encoding reality:** verify H.264 WebRTC low-res encoding and high-quality HEVC recording really happen concurrently without fallback to software encode, unexpected camera recapture, excessive frames or audio dropout.
4. **Local browser HTTPS:** mobile host must expose a browser-trusted HTTPS origin for full dashboard features, with WSS for events/signaling. One-time LAN pairing/trust bootstrap is required; never rely on a publicly issued certificate or silently suggest disabling browser security.
5. **Direct peer signaling:** phone acts as offer/answer negotiation endpoint; WebRTC media goes directly to paired browser. `iceServers: []` for local-only test; keep only host candidates, and display clear LAN peer failure if AP client isolation prevents connectivity.
6. **Foreground-only:** app keeps screen awake during active session. If OS pauses, camera permission revoked, phone call interrupts mic or UI loses foreground, make state visible and recover, but do not pretend background capture is supported.

## WebRTC connection sequence

```text
PHONE UI             embedded HTTPS/WSS server           PC BROWSER
  │                         │                                │
  │ start session           │                                │
  │ activate LAN URL/QR ───>│                                │
  │                         │<──── HTTPS open + pairing ───────│
  │ approve/confirm pair ──>│──── scoped session token ──────>│
  │                         │<──── subscribe status WSS ───────│
  │<──── request viewer ────│<──── SDP offer/answer/ICE ───────│
  │──── publish video track ┼──────── WebRTC DTLS-SRTP ───────>│
  │──── status/voice events ┼──────── WSS events ─────────────>│
  │                         │<──── permitted control RPC ──────│
  │ local recording continues if browser disconnects           │
```

Host candidates and media lifetime must be debugged using `RTCPeerConnection.getStats()` and the matching mobile peer statistics. The phone must know when a viewer disconnects and surface it honestly.

## Core state machines

- App session: `idle | requesting_permissions | ready | active | interrupted | error`.
- Camera: `unavailable | initializing | previewing | transmitting | degraded | error`.
- Recording: `idle | starting | recording | stopping | finalizing | available | failed`.
- WebRTC: `disconnected | pairing | connecting | connected | reconnecting | failed`.
- Wake/voice: `unavailable | loading_model | listening | wake_detected | recognizing | cooling_down | error | muted`.
- Transfer: `not_requested | preparing | transferring | verifying | completed | paused | failed | cancelled`.

Report state transitions with reason codes and a monotonically increasing `sequence` number. Viewer reconnects request a fresh **snapshot**, not a replay of stale commands.

## Timing and concurrency

Use monotonic timestamps (device nanoseconds or equivalent) at capture/inference; include UTC wall time for display. Recording and voice engines must not block UI isolate; backpressure for vision pipeline should drop **oldest nonessential frames**, not keep a growing queue. Local video recorder must continue despite network congestion. Wake inference must keep a rolling buffer without recording raw mic to disk by default outside the master video file.

## Media priorities under load

1. Preserve ongoing master recording data and audio integrity.
2. Maintain responsive mobile stop/record controls.
3. Keep accurate speech engine state; display if ASR is temporarily throttled.
4. Lower WebRTC resolution, bitrate or FPS when required.
5. Prefer visible warnings and a user-approved profile change over silently degrading the master.

## iOS portability

Flutter/Dart owns models and state where possible. Swift provides an AVFoundation capture/muxer adapter, AVAudioSession management and local network/network permissions. First iOS acceptance is **not** established until physical iPhone tests and build under macOS/Xcode. iOS may impose foreground/background/interruption differences and capture-output constraints; surface incompatibilities rather than hiding them.
