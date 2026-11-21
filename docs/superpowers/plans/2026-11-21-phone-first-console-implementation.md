# Phone-First Console MVP Implementation Plan

> Progress and current state: see `docs/STATUS.md`. One agent executes this plan itself (no subagents).


**Goal:** Deliver a professional foreground Flutter phone camera/voice app, compressed one-to-one WebRTC PC browser dashboard, high-quality local recording, local wake-word+ASR and verified LAN file download, one tested vertical slice at a time.

**Architecture:** Flutter UI and app state; Kotlin/Swift native single-owner video/audio capture bridges; direct local WebRTC video; phone embedded HTTPS/WSS server for secure browser pairing, status/control and original download; all inference on mobile. Device remains foreground and presents full operator UI.

**Tech Stack:** Flutter/Dart; Android CameraX/Camera2/MediaCodec/AudioRecord as appropriate; iOS AVFoundation/AVAudioSession; `flutter_webrtc`; `sherpa_onnx` or justified unified on-device inference binding; TypeScript/React-or-vanilla desktop dashboard bundled into mobile assets; local HTTPS/WSS; MP4/HEVC or compatible hardware profile.

**Spec:** `docs/superpowers/specs/2026-11-21-phone-first-console-design.md`

## Global Constraints

- Single phone camera + microphone owner; compressed H.264 WebRTC for the PC only, master original stored on device.
- No PC server/AI, cloud model or external STUN/TURN; local browser dashboard served by phone with authenticated HTTPS/WSS.
- Flutter code architecture supports Android/iOS; Pixel 10 Pro XL is the initial hardware acceptance target; iOS tests require actual iPhone and Mac.
- Mobile foreground app always shows useful real information, including preview, record/voice/status/transcript/files; keeps screen awake while session is active.
- Runtime operates without WAN after dependencies/models are installed.
- One completed, evidenced, committed vertical slice before beginning next.
- Ask owner before any unresolved user-facing behavior choice; routine technical choices should follow spec and be documented.

## Review Focus

1. **Multiple media capture owners:** recording audio and wake recognition during WebRTC must not mute each other. Test in tasks 2/3.
2. **Advertised vs actual camera quality:** profile requested but hardware cannot sustain it. Test actual outputs in task 2.
3. **Local HTTPS trust / pairing:** server available but browser refuses trust or permits unauthorized access. Test in task 1.
4. **WAN disconnected / Wi-Fi isolation:** video/signaling/voice must operate without Internet, with clear failure for client isolation. Test in tasks 1–4.
5. **Partial/corrupt final files and transfers:** an unfinished recording or half download must never be shown as verified. Test in tasks 2/5.

---

## Planned file responsibility map

`apps/mobile/lib/app/`: shared app lifecycle, navigation and state; `features/connectivity/`: LAN HTTPS server/WS pairing/signaling; `features/capture/`: preview and profile selection; `features/recording/`: master/local library; `features/voice/`: KWS/VAD/ASR state; `features/transfer/`: file serving; `platform/`: Dart native bridge interfaces; `apps/mobile/android/...`: Kotlin media/audio implementation; `apps/mobile/ios/...`: Swift AVFoundation counterpart; `apps/dashboard/src/`: responsive browser UI, WebRTC and events; `packages/protocol/`: versioned JSON API contracts. Follow existing repo patterns when present. Each file should have one responsibility; exact Dart class/function signatures should be locked in an interface ADR before parallel implementations.

### Task 1 — Slice 0: authenticated foreground mobile + browser status

**Files to create/adjust:** `apps/mobile/lib/app/`, `apps/mobile/lib/features/connectivity/`, `apps/mobile/lib/platform/`, `apps/dashboard/src/`, `packages/protocol/`, native permission manifests, `docs/evidence/slice-00-*.md`.

**Interfaces:** `SessionState` snapshot and `SessionEvent` envelopes defined in `docs/INTERFACES.md`; `LocalServer.start()`, `.stop()` and `.events`; `PairingService.createOneTimeCode()`, `.authorizeViewer()`, `.revoke()`. Lock names/signatures in `packages/protocol/README.md` before writing both sides.

- [ ] **Step 1:** Inspect Flutter/Android/iOS toolchain, native targets and repo conventions; create minimal mobile + dashboard workspaces using approved package versions.
- [ ] **Step 2:** Write failing state/permission/token/expiry tests and browser pairing/status component tests. Test origin/auth expiry and revoked access.
- [ ] **Step 3:** Run tests, record expected fail baseline.
- [ ] **Step 4:** Implement Flutter shell + real permission/phone status, foreground keep-screen-on, local HTTPS/WSS server + certificate trust onboarding, QR pairing, signed scoped viewer session, bundled web assets.
- [ ] **Step 5:** Implement browser dashboard shell + actual session snapshots/WSS status and offline error recovery.
- [ ] **Step 6:** Run format/lints/unit/widget/integration; run Pixel+PC LAN hardware tests S0-01..04, including WAN off, unsupported cert, revoked viewer. Collect screenshot/log evidence.
- [ ] **Step 7:** Document results, secure local pairing tradeoff/ADR, and commit `feat: add local paired phone and browser session`.

**Gate:** Browser without desktop server sees and updates real mobile session states via authenticated HTTPS/WSS, no cloud, clear failures, while phone stays awake.

### Task 2 — Slice 1: compressed WebRTC video and phone preview

**Files:** `apps/mobile/lib/features/capture/`, `apps/mobile/lib/platform/`, native Android camera adapter, `apps/dashboard/src/features/video/`, protocol signaling/stats tests, evidence.

**Interfaces:** `CameraSession.listProfiles()`, `.startPreview()`, `.stopPreview()`, `VisionStream.start(profile)`, `.stop()`; SDP/ICE events from `docs/INTERFACES.md`. `CameraProfile` includes requested/actual resolution/fps/codec, not invented values.

- [ ] **Step 1:** Probe real Pixel Camera2 formats and encoder modes, inspect `flutter_webrtc` camera/video source hooks and shareability.
- [ ] **Step 2:** Write failing profile/state/transceiver/signaling tests; browser reconnect and permission failure cases.
- [ ] **Step 3:** Run tests to see expected failures.
- [ ] **Step 4:** Implement one camera owner, native/Flutter preview and low-bitrate video source, direct peer 1:1 H.264 WebRTC with no public servers. Implement browser receiver and useful status overlays.
- [ ] **Step 5:** Measure `getStats()` and native capture stats and report actual, not requested, codec/size/FPS/RTT.
- [ ] **Step 6:** Test S1-01..05 on Pixel and PC, LAN WAN-disabled, browser closes/rejoins, rotation, client isolation failure. Run prior slice tests.
- [ ] **Step 7:** Document evidence, ADR if plugin cannot share native video source, commit `feat: stream low-res phone video to local dashboard`.

**Gate:** Real phone preview and compressed live WebRTC video on browser with independent lifecycle, truthfully reported metrics.

### Task 3 — Slice 2: native master recording concurrent with stream

**Files:** `apps/mobile/lib/features/recording/`, native recorder/audio tap, `apps/mobile/lib/features/capture/` recording adapter, browser session/record indicator, recording manifest and state tests, evidence.

**Interfaces:** `Recorder.start(profile)`, `.stop()`, `.listFinalized()`, `RecordingManifest` from `docs/INTERFACES.md`, `AudioCaptureBus.subscribePcm()` for future voice; interface ownership documented.

- [ ] **Step 1:** Ask if recording auto-starts and whether PC may start/stop; ask audio mute semantics only if needed.
- [ ] **Step 2:** Write state-machine, incomplete/finalized-file, ID collision, low disk and remote-control auth tests. Add explicit multi-owner contention test/hardware procedure.
- [ ] **Step 3:** Run tests and verify fail baseline.
- [ ] **Step 4:** Implement native high-quality video+audio recording from the **same camera/mic owners** as preview/vision; profile capability probe; user-visible record controls; on-phone library and browser status.
- [ ] **Step 5:** Run Pixel + PC S2-01..05 with simultaneous stream, storage warning, Wi-Fi disconnect, audible audio, file metadata and recorded resolution verification. Run prior slice regression suite.
- [ ] **Step 6:** Record real quality/thermal/encoder limits, document playable original evidence, commit `feat: record high-quality original locally during stream`.

**Gate:** Playable high-quality native original on phone with audio while browser receives low-bitrate stream; network loss does not stop master.

### Task 4 — Slice 3: local wake-word detection

**Files:** `apps/mobile/lib/features/voice/` + model manifest/assets, native mic tap if needed, voice cards on mobile/browser, events schema tests, evidence.

**Interfaces:** `VoiceEngine.loadModel()`, `.startListening()`, `.mute()`, `.stop()`, events `voice.state` / `voice.wake_detected`; PCM only from Task 3's coordinated `AudioCaptureBus`.

- [ ] **Step 1:** Ask owner which wake phrase to use; allow clearly labeled demo phrase for initial testing only. Check KWS model weights licenses and compatible mobile APIs.
- [ ] **Step 2:** Write failing KWS state tests (no model, muted, cooldown, hit, timeout, mic interruption) and event sync tests.
- [ ] **Step 3:** Run tests for failing baseline.
- [ ] **Step 4:** Implement local KWS with single audio tap, model load/cooldown, phone and browser listening/wake UI, no audio egress.
- [ ] **Step 5:** Test S3-01..04 while master recording and WebRTC stream active and WAN disabled. Compare recorded audio; document false-positive observations.
- [ ] **Step 6:** Run regressions and commit `feat: detect wake word locally on phone` with evidence.

**Gate:** Real wake-word hits on-device alongside stream + recording, no cloud, phone and browser state synchronized.

### Task 5 — Slice 4: local VAD and speech-to-text

**Files:** `apps/mobile/lib/features/voice/` models/transcripts, dashboard transcript timeline, state tests, evidence.

**Interfaces:** `TranscriptionEngine.recognize(utterance)`, streaming partial/final events from `docs/INTERFACES.md`; utterance id and device timestamps; local transcript store with user-approved retention.

- [ ] **Step 1:** Ask user ASR language(s) and transcript retention preference. Select/check local quantized model and license; benchmark first.
- [ ] **Step 2:** Write failing tests for wake→VAD→ASR, partial→final, missing model, timeout, mute, duplicate utterances, browser reconnection.
- [ ] **Step 3:** Run tests for expected failure.
- [ ] **Step 4:** Implement on-device VAD+ASR, bounded PCM buffers, non-blocking Flutter UI, phone and browser transcript history. No audio streaming to PC.
- [ ] **Step 5:** Verify S4-01..04 with 3 real utterances under simultaneous video streaming + recording, WAN disconnected and actual recognition timing measurements.
- [ ] **Step 6:** Regress earlier slices, document evidence and commit `feat: transcribe wake-triggered speech offline on phone`.

**Gate:** Real recognized local speech on phone and browser in a concurrent live session; correct missing-model and audio failure states.

### Task 6 — Slice 5: secure verified original download

**Files:** `apps/mobile/lib/features/transfer/`, download API, `apps/dashboard/src/features/library/`, integrity/Range tests, evidence.

**Interfaces:** `GET /api/v1/recordings` and `GET /api/v1/recordings/{id}/download`; manifest per `docs/INTERFACES.md`, `FileTransferState`, SHA-256 digest, ranged responses.

- [ ] **Step 1:** Ask user if post-shoot download is manual or automatically triggered and destination permissions are acceptable.
- [ ] **Step 2:** Write failing tests for authenticated listing, streamed body, 206 Range, ETag/If-Range, 416, path traversal, expired token, cancel/partial, checksum mismatch.
- [ ] **Step 3:** Run tests for expected failures.
- [ ] **Step 4:** Implement memory-bounded HTTPS original transfer, browser UI, real progress, checksum verification, browser-specific resume caveats. Do not add SMB/FTP as a prerequisite.
- [ ] **Step 5:** Verify S5-01..04 on a substantial real recording, WAN off, interrupted download, equal SHA-256 on phone and PC (external command if browser cannot verify).
- [ ] **Step 6:** Regress prior slices, record evidence and commit `feat: transfer verified original recordings over LAN`.

**Gate:** Original bytes leave device only over explicitly authorized LAN transfer, file matches source hash, and interruption/recovery behavior is truthful.

### Task 7 — Slice 6: iOS adapter and actual parity verification

**Files:** `apps/mobile/ios/`, native Swift media/voice bridge, iOS permissions, platform tests, evidence.

**Interfaces:** same `CameraSession`, `Recorder`, `AudioCaptureBus`, `VoiceEngine`, server and control contracts defined in preceding slices.

- [ ] **Step 1:** Audit iOS minimum version, audio session and AVFoundation supported output combinations. Build using macOS/Xcode; compare against shared Dart state contract.
- [ ] **Step 2:** Add native Swift tests where possible plus widget and protocol regressions. Implement iOS adapters with capability-based fallbacks.
- [ ] **Step 3:** Real iPhone camera+recording+WebRTC+voice+HTTPS pairing test with WAN disabled; document any iOS-specific restrictions.
- [ ] **Step 4:** Record evidence and commit `feat: validate iOS native capture and voice support` only if tested. Without hardware mark **BLOCKED / NOT TESTED** rather than claiming full iOS parity.

**Gate:** Verified physical iPhone behavior or explicitly unverified port with clear remaining work.

## Self-review before delivery

- [ ] Every locked requirement in spec points to a task and real acceptance test.
- [ ] No PC AI backend, cloud runtime, extra robot camera, or background guarantee slipped in.
- [ ] Dual video path is distinct; recording independent of WebRTC; audio single-owner and tested.
- [ ] Mobile and browser UX show actual status/permissions/error states and no fake metrics.
- [ ] Pairing/trust security and original file transfer verification covered end-to-end.
- [ ] Each slice was completed, reviewed, tested and committed separately; any inaccessible hardware labeled clearly.
