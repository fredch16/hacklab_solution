# Phone-First Local Vision Console — Design Specification

**Date:** 2026-11-21. **Status:** proposed design handoff reflecting decisions already supplied by the owner; implementation awaits repository execution and open product decisions in the relevant slices.

## Intent and why

The owner wants a local, reliable camera-and-voice foundation for future robotic/gimbal use, rather than a large server-bound robotics stack. The Pixel's high-quality camera/microphone and on-device compute can handle capture, recording, wake-word recognition and speech-to-text while remaining foreground. The PC should be a thin, browser-only remote monitor/control dashboard. Working whole features in measurable vertical slices matters more than scattering partial implementation.

## Goals

- A useful professional Flutter UI on the always-open phone, with status, preview, recording, speech activity, transcript and file controls.
- Android first with a cross-platform Flutter interface and native iOS adapters, no arbitrary Android-only product assumptions.
- One phone camera: compressed low-res WebRTC feed for browser/CV and simultaneous high-quality local original with audio.
- Single microphone capture; wake/VAD/ASR on phone using offline model pack.
- Local-only signaling, dashboard delivery, pairing and secured original download after recording.
- Honest measurable diagnostics, tests, errors and a demonstrably functioning slice at each step.

## Anti-goals

- No high-quality source file on WebRTC, no cloud/Internet inference, no PC inference stack, no robot cameras or calibration, no robot movement, no background capture, no unrequested LLM assistant, and no fake metrics.

## Constraints

- Phone remains in foreground, screen awake. Android camera/codec capability must be queried, iOS camera and audio integration separately validated; concurrent encoding and capture are not guaranteed by package capability flags.
- Browser needs local secure HTTPS/WSS trust and pairing, direct LAN WebRTC with host ICE candidates and no external ICE servers.
- Recording must not depend on browser/network uptime. Original copied only via authenticated post-shoot transfer.
- All speech models and dashboard frontend assets local, with exact weight licensing checked before shipping.
- Authenticated viewer and explicit scopes; raw mic audio never shipped to the PC by default.

## Approaches considered and decision

**A. Browser-only capture on Android/iOS:** fastest proof of concept but unreliable for robust native simultaneous high-quality recording, microphone sharing and sustained on-device speech; rejected as production foundation.

**B. PC-based media server and offline AI:** easier backend tooling and more compute, but breaks the owner's goal of independent mobile compute/browser-only desktop; rejected.

**C. Flutter foreground app + native media/audio bridges + embedded phone LAN HTTPS/WSS + thin browser dashboard:** preferred; honors local/phone-first goals and iOS reuse while keeping access to platform media internals. Has greater native integration cost; accepted as technical direction by owner's latest request.

## Application boundaries

See `docs/ARCHITECTURE.md` for capture/audio fan-out, phone server signaling, data flow and state machines. Flutter owns shared UI/domain; native Kotlin/Swift own camera and audio details as required. WebRTC carries only low-bitrate video; WSS carries status and text; local HTTPS carries original files.

## Professional GUI

`docs/DESIGN_SYSTEM.md` defines **Quiet Precision**, phone tabs and desktop video/voice layout, status language, accessibility and responsive breakpoints. `ui/reference-dashboard.html` is a visual reference; all real components must use actual state and be individually testable.

## Unresolved behavior (ask only when relevant)

Recording automatic versus manual, PC authority over recording, separate assistant mic mute/recording mute, wake phrase, ASR languages, post-shoot auto-download semantics, HTTPS onboarding friction and actual default capture profile. Refer to `docs/DECISIONS_AND_QUESTIONS.md`. Do not invent user preference or block earlier unrelated work.

## Delivery and proof

`docs/SLICE_PLAN.md` defines S0 secure app/pairing, S1 WebRTC, S2 master recording, S3 wake word, S4 ASR, S5 transfer, S6 iOS parity. `docs/TEST_PLAN.md` defines evidence and hardware gates. The following page documents task-by-task execution.
