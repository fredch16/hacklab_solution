# Video, local audio inference and recording specification

## Video paths

### A. Local master (never via WebRTC)

- Store **original quality** recording on smartphone, with camera audio, in a normal playable container (MP4/HEVC where supported; compatible fallback H.264 when needed).
- Proposed profile exploration: `4K30 HEVC` → `1080p60 HEVC/H.264` → `1080p30`, based on **real simultaneous recording + streaming + ASR benchmarks**; user chooses preferred default after capability report.
- Capture camera exposure/focus/zoom/orientation metadata and device timestamps when supported. Rotation metadata must be honored by playback and vision client, no unexpected mirrored back-camera feed.
- Use file-safe recording workflow: create pending/incomplete artifact, write and flush chunks, finalize metadata/container, mark available only after successful finalization. Crash/interruption recovery and low-disk warning are mandatory.
- Store recording `id`, format, resolution, measured FPS, start/end, duration, bytes, file checksum (compute asynchronously after finalize) and optional associated transcript timeline. Do not auto-delete after transfer.

### B. Low-res live vision

- Baseline **H.264 at 640×360 / 30 FPS / target ~0.5–1.5 Mbps** over WebRTC. This is a baseline, not an upper bound or a guaranteed encoder mode.
- Expose optional 720p30 profile and measured adaptation. On-wire codec and negotiated dimensions must appear in diagnostics, not simply requested values.
- Preserve frame order and monotonic capture timestamps; drop stale frames during congestion. Record decode latency/RTT where measurable, never fake end-to-end latency.
- One phone sender and one paired browser receiver; no STUN/TURN cloud infrastructure and no SFU.
- For future CV consumers, define a `visionFrame` acquisition/consumer interface within phone and allow direct local AI inference without requiring a PC stream. Actual CV/tracking is out of scope for MVP.

## Audio ownership (single capture path)

```text
        Microphone hardware
                │
        Native audio capture owner
                │
        time-aligned PCM frames
             ┌──┴───────────────┐
             ▼                  ▼
   Master recording muxer   16 kHz mono resample
                                │
                             Wake word
                                │ (on hit)
                            VAD + ASR
                                │
                        transcript events
```

- Audio input ownership and app audio session category must be tested on both platforms. Do not instantiate `flutter_webrtc` audio capture, independent recorder and separate wake-word plugin concurrently without proving non-contention.
- Test voice inference **while recording + streaming**. Ensure recording has audible audio, wake word still detects and no double-trigger echo from phone speaker.
- Audio input: appropriate device sample rate and channel count; resample/copy for model as needed, never damage master recording audio quality to satisfy model formats. Keep only a short in-memory audio pre-roll for wake word; avoid indefinite speech/audio log on disk.

## Suggested offline inference stack

Use **sherpa-onnx** (mobile Flutter/Dart bindings with native runtime) as the preferred unified engine for **KWS + Silero VAD + streaming/offline ASR**. Verify its actual mobile APIs, model memory, licensing, language support and perf before locking. It supports keyword spotting and offline ASR on Android/iOS. Prefer one engine to competing mic-consuming plugins. Alternative packages are acceptable only with an explicit decision record and tests proving integrated microphone ownership.

### Wake-word pipeline

1. In active foreground session, keep KWS listening using one local model pack bundled or locally imported with manifest/license/checksum.
2. On wake detection, emit `voice.wake_detected` with timestamp and detection model, show active state in both UIs.
3. Apply VAD to collect the following speech; use pre-roll as supported, timeout, max utterance limit and cooldown to avoid repeated activations.
4. On endpoint or timeout, run local ASR (streaming partial text if supported), publish `voice.transcript.partial` / `.final` with optional confidence, text, timing and language.
5. Return to `listening` after cooldown. A manual **Test voice** action should bypass wake word for ASR validation without changing default privacy.

### Recognition model choices

- **Require user confirmation** for spoken wake phrase and recognition language(s) before choosing production model packs.
- For engineering smoke tests, agents may use a clearly labeled *example/test phrase* from a properly licensed available model. This is not a permanent product choice.
- Choose a mobile-appropriate quantized model after measurement: model size, cold load, memory, duration, noise tolerance, CPU/GPU/NPU support and recognition performance. Do not assume GPU/NPU acceleration is enabled simply because the phone has hardware capable of it.
- Models are local artifacts (bundled or manually imported). Public Internet access may be used during initial **build/setup** to fetch dependencies/assets, but there must be **no runtime external model or speech service**. Prefer reproducible pinned checksums and distributable license metadata.
- Examine license terms for the **exact model weights**, not only the engine source. Some popular wake-word pretrained models are non-commercial; document limitations before selecting or bundling.

## What the user sees

Mobile and desktop should clearly distinguish `Mic muted`, `Wake listening`, `Wake detected`, `Listening for command`, `Transcribing`, `Final transcript`, `Model missing`, `Permission denied`, `Audio interrupted` and `Too noisy/timeout`. Do not show fabricated recognized text. Expose tap-to-copy/edit transcript as local UX, but no voice-command execution in Phase 1.

## Audio tests

- Wake phrase detects in normal near/far speech; avoid obvious false positives in noise, with documented limitations.
- After wake word, 3 different utterances recognized **entirely offline** and appear on both screens.
- Works concurrently with streaming and saved master video **with audible sound**.
- Mic mute immediately stops KWS/ASR and updates dashboard; active recording behavior when muted must follow user-approved audio policy (ask before deciding whether mute also mutes recording audio).
- Interruption/reconnect: event listener may disconnect; phone inference continues, browser reconnects via snapshot. Phone call/OS mic interruption surfaces `interrupted` and recovers.
