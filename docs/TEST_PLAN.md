# Verification strategy, acceptance matrix and evidence

**No tests were executed on the user's phone while preparing this handoff.** These are instructions and expected outcomes for implementing agents. Record real evidence under `docs/evidence/` after running the app.

## Test taxonomy

- **Static/unit:** Dart state reducers, session transitions, codec profile selection, event schema, permission handling, transcript ring buffer, download URI validation, path traversal, token expiry, hash verification.
- **Widget/component:** mobile buttons/tabs/warnings, desktop view states, transcript partial→final update, responsive layout and keyboard controls.
- **Integration:** embedded server, HTTPS, WSS, WebRTC offer/answer/ICE, token scopes, authenticated download, shutdown/reconnect state.
- **Native/device:** camera actual available formats and simultaneous captures, mic/recording audio quality, thermal load, model inference, app lifecycle, display keep-awake, file integrity.
- **Security:** no unauthorized viewer or file access, invalid/expired pair rejected, TLS trusted correctly, API origin enforcement, LAN-only network egress.
- **Offline:** build/install and manually bundle models in advance, disconnect WAN while keeping Wi-Fi LAN, then complete every operational flow.

## Acceptance test cases

| ID | Slice | Scenario | PASS condition |
|---|---:|---|---|
| S0-01 | 0 | Start Flutter app on Pixel | True phone status visible; screen remains on while session active |
| S0-02 | 0 | Pair PC via trusted HTTPS | Browser displays real status and can subscribe to updates |
| S0-03 | 0 | Revoke paired PC | Browser loses authorized access immediately; no file/status disclosure |
| S0-04 | 0 | WAN disconnected / LAN connected | Local UI, HTTPS dashboard and event updates still work |
| S1-01 | 1 | Start camera with no viewer | Local preview runs, no fake remote stream badge |
| S1-02 | 1 | Browser joins WebRTC | Real phone image displays; actual negotiated H.264 codec where supported |
| S1-03 | 1 | Measure stream | Report actual FPS, frame size, bitrate, ICE candidate, RTT where available |
| S1-04 | 1 | Disable PC Wi-Fi / reconnect | Phone session continues, browser reconnects without stale status |
| S1-05 | 1 | Deny camera permission | Explicit error and recoverable permission request |
| S2-01 | 2 | Record while streaming | Playable high-quality original in phone library with audible audio |
| S2-02 | 2 | Stop and finalize | Manifest marks available only after file is valid and finalized |
| S2-03 | 2 | Pull network during recording | On-device recording continues, no corruption due to viewer loss |
| S2-04 | 2 | Low storage / interrupted recording | Clear warning and recoverable file state; no fake success |
| S2-05 | 2 | Sustained workload | No UI lock or audio drop in 5-minute smoke; schedule extended soak |
| S3-01 | 3 | Speak KWS phrase on phone | Wake event and status on both UIs, no cloud call |
| S3-02 | 3 | Audio while video+recording | Master audio intact and wake recognition still operates |
| S3-03 | 3 | Mute microphone | KWS stops, both UIs reflect actual mic state |
| S3-04 | 3 | Negative speech/noise test | Measured false positives recorded, no fabricated accuracy claim |
| S4-01 | 4 | Wake phrase + 3 utterances | Final transcripts and command results visible on phone and browser |
| S4-02 | 4 | VAD endpoint/timeout | Utterance completes or times out with clear state |
| S4-03 | 4 | Missing/invalid model | Clear model error, other capture features remain operable |
| S4-04 | 4 | WAN off, no ASR API | Offline transcription still completes, no Internet egress |
| S5-01 | 5 | Browser download original | Bytes match manifest and SHA-256 exactly equals phone original |
| S5-02 | 5 | Interrupt download | Recover with Range where supported or explain browser limitation |
| S5-03 | 5 | Invalid file id/path/token | Denied, never expose arbitrary files or logs |
| S5-04 | 5 | Transfer while app active | Controls responsive, recording priority maintained |
| S6-01 | 6 | iPhone foreground parity | Real AVFoundation camera/record/WRTC/KWS/ASR paths evidenced |
| S6-02 | 6 | iOS permission/interruption | Clear, correct state; no background capture promise |

## Precise measurements to capture (never assume)

- Device model, OS, app build/commit, Flutter version, native media libs, browser version, transport (Wi-Fi/USB), network topology.
- Record: requested and actual sensor mode, codec, width/height, FPS, bit rate, sample rate, container, file duration/bytes, audio-video skew, finalization time.
- WebRTC: codec/candidate, video size, rendered FPS, sent/received FPS, target/actual bitrate, RTT, frames dropped, packet loss, decoder latency where exposed.
- Voice: selected model and checksum, wake trigger latency distribution, false positives, ASR load time, utterance duration, partial/final latency, peak memory, CPU/thermal trend.
- Battery/heat: start/end % and approximate thermal state, if exposed by platform. Do not promise a specific °C sensor exists on both platforms.
- Transfer: bytes, elapsed time, throughput, interruption point, resumed bytes and checksum result.

**Latency note:** WebRTC stats `roundTripTime` is **network RTT**, not camera-to-display latency. To measure glass-to-glass, use a physical timer/LED or synchronized capture method; label experimental method and its uncertainty.

## Mandatory edge cases

- Missing camera/mic permissions and permission revocation mid-session.
- Phone/PC on different WLANs; Wi-Fi AP client isolation; phone changes IP; LAN disabled; browser URL cert not trusted.
- Device rotates portrait↔landscape; phone screen remains on; UI respects notch/safe area; user tries to background app.
- Mic occupied by OS or phone call interrupts audio; capture resumes gracefully.
- Recorder and streamer compete for hardware encoder; 4K profile unsupported; overheating; storage full.
- Model corrupted/missing/license not cleared; ASR delayed; voice wakes repeatedly or times out.
- PC viewer closes unexpectedly; original file transfer interrupted; app stopped during finalization.
- Browser unsupported Range-resume or file save permission; do not claim automatic transfer works when browser restricts it.

## Privacy/offline verification

Do not treat a disabled mobile-data icon as proof. Configure LAN with WAN uplink disconnected. Use local network capture, DNS logs or firewall policy to verify **no external runtime hosts are required**. All model files must already reside on the phone. Perform end-to-end demo without public Internet access.

## Evidence document template

Create `docs/evidence/slice-XX-YYYY-MM-DD.md`:

```text
Build/commit:
Device/OS and browser:
Network and WAN status:
Test case IDs + commands:
Observed logs and screenshots:
Measurements with units:
Failures and reproduction steps:
PASS / FAIL / BLOCKED / NOT TESTED:
Reviewed by / next actions:
```

Tests may use mocks to verify state transitions, but **mocks cannot replace the real phone camera, microphone, WebRTC or file verification acceptance gates**.
