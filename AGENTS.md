# AGENTS.md — binding instructions for every implementation agent

Applies to Claude Code, Codex, Cursor, Copilot, reviewers, and humans executing changes in this repository. **Read this file first**, then `docs/PRODUCT_SPEC.md`, `docs/ARCHITECTURE.md`, `docs/SLICE_PLAN.md` and `docs/TEST_PLAN.md`.

## 1. Mission and boundaries

Build a **phone-first, fully local, foreground** Flutter application that owns camera/mic capture, low-bitrate WebRTC transmission, high-quality **on-device** recording, offline wake-word detection and speech-to-text. The same Flutter app is a **useful on-phone control screen**. It also hosts the signaling/control/download endpoints and a **browser-only PC dashboard**. The PC does **not** run AI inference, camera processing, Python, an SFU or a mandatory server.

The Pixel 10 Pro XL is the first target. Maintain shared Dart UI/domain code and clean Kotlin/Swift native bridges for iOS compatibility. Do not claim iOS behavior works without testing it on real iPhone hardware.

One camera (the phone) in this MVP. Do not add robot USB cameras, calibration suites, robotics control, ROS, autonomous actions, agents/LLMs, remote cloud services, or full-resolution video streaming.

## 2. Vertical slices are mandatory

**Complete one demonstrable feature end-to-end before starting the next.** A slice includes UI, device side, browser side where applicable, error states, diagnostics, tests, a demo and updated docs. Never stop at a compiled component and call it complete.

For each slice, in order:
1. Read the spec and relevant interfaces, inspect repository, and write/confirm a tiny acceptance checklist.
2. Record any unresolved **behavioral or UX** question. **Ask the user** instead of guessing. Present a short recommendation and trade-offs, and ask *how they want it to work*.
3. Write meaningful tests or a reproducible hardware test procedure **before** implementing; keep feedback loops short.
4. Implement the entire slice, including app UI, browser behavior, communications, lifecycle and failure handling.
5. Run automated tests and real-device tests where available. Collect evidence (commands, logs, screenshots/video, measured values).
6. Perform a self-review: verify offline-only, permissions, error messages, accessibility, no fake metrics, and regressions in earlier slices.
7. Document results in `docs/TEST_PLAN.md` or a new `docs/evidence/slice-N.md` (timestamp, platform/OS, build, expected/actual, screenshots, logs).
8. Commit the slice atomically with a clear message. Move to the next **only** after the required gate passes or user explicitly accepts a documented limitation.

**Do not hide partial/failed hardware tests.** If hardware is unavailable, mark it `NOT TESTED: needs Pixel/iPhone/PC`; do not claim the slice is proven complete. If blocked, state the exact missing information or prerequisite and ask one actionable question.

Do not create a feature that is three layers of placeholder code. Mock data can only appear in isolated, labeled demos/tests—not as working camera, recording, wake-word or transcription output.

## 2A. Working mode

One agent does all implementation itself; no subagents or parallel delegated workers. The owner performs real-world hardware tests and reports results — agents build, unit-test, install and provide short test steps instead of driving the device UI or relying on screenshots. Record progress in `docs/STATUS.md`.

## 3. Asking questions vs making routine decisions

**Always ask** when:
- a user-visible interaction, default, privacy choice or irreversible action is undefined;
- the wake phrase, recognition language(s), recording control authority, auto-transfer semantics or other consequential requirement is undecided;
- an improvement would change the requested flow, add an external service, extra hardware, cloud dependency or scope;
- multiple architectures have materially different UX, costs or guarantees;
- platform limitations prevent the promised behavior;
- a test fails and repair requires changing the agreed specification.

**Do not interrupt** for routine implementation detail that follows documented constraints. Choose the smallest maintainable solution, record a short ADR if a non-obvious tradeoff is involved, and continue the **whole slice**. Make suggestions proactively, but distinguish **proposal** from **approved requirement**. Don't repeatedly ask questions already answered by the repo/spec.

Suggested question format: "I recommend A because X; B trades that for Y. This affects [behavior]. Which should the application do?"

## 4. Technical invariants

- **Single physical camera owner** per phone session. The app's capture pipeline fans out to **local preview + high-quality recording + compressed low-resolution WebRTC**. Never open the camera twice as independent owners to achieve this.
- **Single microphone input owner** routes/taps audio for recording and **on-phone** wake word/VAD/ASR. Avoid concurrent independent recording plugins that contend for the microphone.
- **Master file** stays local until a **separate**, authenticated LAN transfer after stop/finalization. Never send master 4K recording through WebRTC or to a cloud.
- Remote WebRTC is **vision-only** by default; raw microphone audio is *not* sent to PC. Only transcript/status events go to the dashboard unless user explicitly approves a future change.
- All inference **on phone**, using bundled or locally imported model assets and verified license. No speech API, hosted STT, cloud STUN/TURN, Firebase, telemetry SaaS, external CDN, analytics or remote signaling.
- Device stays foreground and keeps screen on while active. Handle OS interruptions truthfully; never promise uninterrupted capture on phone calls, thermal shutdown or backgrounding.
- Public Internet must not be necessary after installing app/models; test by disabling WAN while keeping local Wi-Fi connected.
- Host **secure** LAN dashboard/control endpoints. Pair before remote controls/downloads; no unauthenticated LAN admin endpoints. Document certificate/trust onboarding accurately and avoid telling users to disable browser security.
- WebRTC transport: direct 1:1 peer connection and LAN host ICE candidates, no SFU/media relay or public servers for MVP. Confirm on actual device and target browser.
- Recording has priority over low-res stream and AI. Recover gracefully from thermal/performance pressure; never silently downgrade master footage.
- Do not assert that 4K60 recording + live stream is supported until **simultaneously** benchmarked on the Pixel. Always enumerate hardware capabilities and fallback transparently.
- Designed cross-platform; Android-first validation, then iOS-native parity. Isolate platform-specific code behind interfaces.

## 5. Engineering standards

- Follow existing repo conventions; avoid opportunistic refactors and large speculative frameworks.
- Strictly typed Dart/TypeScript; lint, format, unit, integration and physical-device smoke tests.
- Separate capture, encoding, voice inference, session control, WebRTC transport, storage, transfer and UI into well-defined modules.
- Use versioned messages and explicit finite-state machines; report useful errors with recovery actions.
- Bind services only on local interfaces; pair sessions, expire credentials, sanitize file names, and never expose file paths to clients.
- Treat connection disconnection as **viewer lost**, not recording ended; phone continues local work.
- Keep event timestamps monotonic internally and wall-clock timestamps only for display/logging; log model/camera profiles used.
- Keep screen responsive: offload ASR/encoding, avoid synchronous heavy work on main UI isolate.
- Do not silently choose a branded wake phrase, assistant personality, cloud resource, auto-delete policy or paid/commercially restricted model.
- Validate dependency APIs, mobile permissions and licenses against current upstream docs before pinning versions.

## 6. Required slice report

At the end of each slice, communicate:

```
SLICE: name / commit
WORKS: observed end-to-end behavior
TESTED ON: exact devices, OS versions and browser
RESULTS: commands, evidence paths, metrics and failures
NOT TESTED: anything not demonstrated
RISKS: any known latency, device, quality, security or heat caveat
QUESTIONS / SUGGESTIONS: only choices that actually need user input
NEXT: next agreed vertical slice
```

**Do not mark a slice COMPLETE if its critical real-device acceptance tests have not been run.**
