# Decisions, proposed defaults and questions for the owner

**Principle:** Follow decisions already supplied. Ask **when a question becomes relevant**, ideally one concise question with a recommended option and its trade-offs. Do not block Slice 0 over a question needed only in Slice 4. Questions below are **not** permission to speculate or silently choose a permanent behavior.

## Locked by owner

| Area | Decision |
|---|---|
| Product | Mobile/phone-first camera and voice system for future robotics |
| UI | Both Flutter phone app and PC web dashboard show meaningful live information |
| Foreground | Phone app stays **open** and screen on during active use; background not required |
| Mobile platforms | Flutter Android and iOS design, Pixel 10 Pro XL primary first target |
| Sources | **Only phone camera** for MVP; no robot-camera feed or multi-camera calibration |
| Video transport | **Compressed low-quality WebRTC** adequate for CV/monitoring |
| Recording | **Full-quality master remains on phone**, not streamed via WebRTC |
| Voice | Wake word + VAD + transcription on phone, offline; use phone microphone |
| PC | Browser-only dashboard; no required desktop AI/backend installation |
| Transfer | Original sent over LAN **after** recording/shoot, LocalSend-like |
| Networking | Completely local at runtime; no cloud/model API |
| Process | Complete testable vertical slices; ask about behavioral ambiguities and suggestions |

## Recommended engineering decisions (agent may implement after checking platform)

- Use Flutter for UI and domain/application code; native Kotlin and Swift capture/audio bridges where necessary.
- Use direct 1:1 WebRTC H.264 360p30 baseline (subject to real measured codec negotiation), with no external ICE servers.
- Embedded phone HTTPS/WSS server for browser UI, signaling, status and ranged original download.
- Prefer sherpa-onnx for unified local KWS/VAD/ASR; validate model licenses and real device perf.
- Single camera and mic owners with fan-out; prioritize master recording over viewer stream.
- Distinct human-facing mobile tabs: Capture, Voice, Files, Link. PC: large camera plus status/voice rail.
- Start with manual file download and integrity; optional automatic download based on browser permission and owner's choice.

## Ask before implementing these product decisions

| Needed by | Question | Recommended proposal / why |
|---|---|---|
| Slice 2 | Should recording begin automatically when the session starts, or only after a **Record** tap? | **Manual** avoids unintentional recordings; phone remains preview/voice-ready. |
| Slice 2 | Can the paired PC start/stop local recording, or should only the phone control it? | Permit only after explicit phone pairing/authorization; avoid accidental remote stop. |
| Slice 2/3 | When user taps **Mute**, should it mute voice assistant listening only, or also the audio saved with the video? | Separate `Mute Assistant` from `Mute Recording Audio` for clear semantics. |
| Slice 3 | What exact wake phrase should trigger the assistant? | Ask for exact phrase; pick a properly licensed supported keyword model, or train a local one if needed. |
| Slice 4 | Which ASR language(s) are required? | Start with user's main spoken language; multilingual mode costs resources. |
| Slice 5 | Should originals transfer **automatically** after stop or manually on demand? Where should PC save them? | Manual reliable default; opt-in auto with explicit browser destination permission. |
| Slice 0 | How much pairing/setup friction is acceptable for locally trusted HTTPS? | Recommend secure one-time trust/pairing; explain implications before final implementation. |
| Slice 2 | Should best quality default be 4K30 HEVC, or do you prefer 1080p60 for motion? | Choose after actual Pixel dual-output benchmark; quality vs thermal/fps tradeoff. |
| Voice follow-up | Should transcripts persist after app restarts and link to original recording timestamps? | Proposed local transcript history with explicit privacy/retention choice. |

## What not to ask again

Don't re-ask whether to use Flutter, whether phone stays foreground, whether to use robot cameras, where inference runs, whether a laptop backend is allowed, or whether the full original should be sent over WebRTC. These are already decided.

## Deferred questions — only ask if new feature approved

Voice command execution authority, robotic control, gimbal trajectory safety, extra camera geometry, multi-user remote viewers, local AI reasoning/LLMs, public Internet access, background operation and cloud backups. None are required for the first five slices.
