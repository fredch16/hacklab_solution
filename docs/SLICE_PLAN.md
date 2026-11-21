# Vertical-slice delivery plan (one complete slice at a time)

**Order matters.** Every slice has a visible standalone result, automated tests and a physical-device gate. A slice isn't complete solely because it runs in a simulator or passes unit tests; hardware verification may be `NOT TESTED / awaiting owner`.

## Slice 0 — Foreground mobile shell + paired browser dashboard

**User-visible outcome:** launch Flutter on Pixel → camera/voice/storage/network cards with *real permission/device states* → show phone local URL and pair PC securely → open polished responsive dashboard in PC browser → mobile and browser reflect current (non-camera) app session state. Mobile stays awake while session is active.

- Build shared UI shell, local server, HTTPS trust setup, pairing, WSS snapshot/events, session lifecycle.
- Empty camera and voice cards must say **Not started**, not invent video or transcripts.
- Check browser page truly loads without Internet or a desktop server. Include disconnection/re-pair/revoke flows.
- **Gate:** trusted HTTPS + valid paired browser + live status change phone↔PC + offline WAN-disabled proof on Pixel and browser. No fake status.

## Slice 1 — One phone camera → compressed WebRTC → PC dashboard

**User-visible outcome:** local phone camera preview AND low-bitrate video received by PC browser simultaneously; app/PC show real dimensions, codec, FPS, bitrate and connection state.

- Capability probe; single native owner; responsive preview; H.264 WebRTC direct LAN; SDP/ICE; no external servers.
- Test single viewer, reconnect, phone network change, portrait/landscape and camera permission denied.
- Stream target 640×360 at 30 FPS, not an assumed guarantee. USB tether or Wi-Fi both can be benchmarked (local only).
- **Gate:** hardware capture and WebRTC confirmed on Pixel+browser with screenshots, getStats logs and no external connections.

## Slice 2 — Local high-quality master recording alongside live WebRTC

**User-visible outcome:** press record on **phone**, get proper timer and audio indicator; remote viewer stays connected to compressed feed; stop → original playable video+audio in mobile library. Browser loss does not stop recording.

- Implement native multi-output recording and synchronized single mic owner, pending/finalized file semantics, capture profiles, disk warning, error/interrupt recovery.
- Prove simultaneously recording **and** streaming, inspect codec/resolution/AV sync and thermal behavior; no duplicate camera owner.
- Remote recording control is only enabled after explicit user approval of authority/confirmation behavior.
- **Gate:** 5-min Pixel test plus longer soak if feasible, playable original with audible audio, actual recording metadata, no WebRTC quality fed into master, graceful network disconnect.

## Slice 3 — On-device wake-word detection while video runs

**User-visible outcome:** when app active, status = listening; speak an agreed test wake phrase; phone and paired dashboard show wake event. Muting prevents recognition. No audio streamed to PC.

- Integrate locally licensed model and PCM tap on the existing mic owner; model load/missing states; cooldown/threshold tuning; voice status events.
- Choose user-confirmed phrase for product; smoke-test phrase may be temporary. No cloud requests.
- **Gate:** real audio recording + video streaming + wake detection in same foreground session; evaluate sample hits and false positives; airplane mode **with local Wi-Fi left on** or WAN disabled.

## Slice 4 — Local VAD + offline speech transcription

**User-visible outcome:** wake phrase → speech utterance → final transcript and any verified command result on phone and PC. Transcription still functions when public Internet inaccessible. Recording remains unaffected.

- sherpa-onnx model packs (or justified alternate), VAD endpointing, ASR, events, transcript list, timeout/cancel/missing model and non-blocking UI.
- Ask user for language(s) before selecting the final ASR model.
- **Gate:** at least three spoken phrases verified on Pixel, recorded while WebRTC video + recording active; latency and model load measured; browser reconnect snapshot works.

## Slice 5 — Original transfer over local HTTPS

**User-visible outcome:** finalized original appears on phone and PC library. User can download to browser over LAN with real progress, interruption handling and SHA-256 verification. Optional post-shoot auto-download is enabled only if chosen by user and browser permissions support it.

- File manifest, safe ranged downloads, auth, integrity checks, storage handling and browser compatibility reporting.
- **Gate:** transmit large file offline with WAN disabled; verify identical hash source and destination; interrupt and resume or transparently report unsupported browser resume; phone recording still independent.

## Slice 6 — Cross-platform hardening and iOS adaptation

**User-visible outcome:** parity or clear surfaced feature profile on a real iPhone. Maintain same Flutter UX/service contracts; Swift/AVFoundation + AVAudioSession bridge as needed.

- Build with macOS/Xcode, physical iPhone, local network permission prompts, camera+mic fan-out and iOS recording/stream ASR load profile.
- **Gate:** real iPhone performance and behavior logged. If no iPhone/Mac, mark `NOT TESTED` and avoid claiming full iOS support. Android slices remain valuable.

## Explicitly deferred

Robot/gimbal control, multi-camera vision, CV tracking, autonomous agents, local LLM reasoning, TTS, continuous microphone broadcast to PC, multi-viewer SFU, external Internet access, and background phone operation. Request user approval before adding.

## Standard per-slice finish report

```
Slice: N — name
Status: PASS / FAIL / BLOCKED / NOT TESTED
Device/build/browser: ...
User flow demonstrated: ...
Tests and results: ...
Actual measured FPS/quality/latency/memory/thermal: ...
Logs/screenshots evidence: ...
Known limitations: ...
Decisions needed: ...
Commit: ...
```
