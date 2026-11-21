# ADR 0004 — Offline wake word + command transcription

**Date:** 2026-11-21 · **Status:** implemented, awaiting owner microphone and browser acceptance · **Slices:** 3–4

## Owner decisions (2026-11-21)
- Wake phrase is **Hey + a user-defined name** in the web dashboard settings. The phone persists the name. Existing saved phrases retain their text after the fixed prefix, and a saved name without the prefix gains it.
- **English only.**
- Command ends on **~1 s of silence** (VAD), hard cap **15 s**.
- Transcripts kept for the **current session only**, in memory; nothing written to disk.
- Voice runs **simultaneously** with recording and streaming from the single `AudioCaptureBus` (ADR 0003).

## Android engine
`sherpa-onnx` (Apache-2.0) on Android, all models bundled in the APK (no download at runtime):
- **KWS:** sherpa-onnx open-vocabulary Zipformer `zh-en-3M-2025-12-20`, using English CMU phonemes and chunk-8 models. The configured name is compiled on the phone using the bundled dictionary and Flite letter-to-sound rules, including names outside the dictionary. The existing GigaSpeech BPE model can encode a name if no pronunciation is available. The spotter starts fresh after a command, mute, stream restart or settings change.
- **Wake handoff:** the command recognizer receives a short rolling PCM buffer so it can recover speech spoken during spotter confirmation. The buffer exists only in memory.
- **VAD:** Silero VAD (MIT) for 1 s end-of-speech detection.
- **ASR:** streaming English zipformer transducer (partial + final results), started on wake, stopped at VAD endpoint/15 s.
Model sources and licenses are recorded with the Android assets. Physical microphone, CPU, latency and thermal measurements during the combined workload are pending. [Model documentation](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)

## Alternatives considered

- Porcupine has an Android PCM API and dedicated per-phrase models. Its AccessKey and platform-specific model setup would add provisioning when the operator changes names. [Porcupine Android docs](https://picovoice.ai/quick-start/wake-word-android/)
- openWakeWord needs a trained model for each new phrase. Its example pretrained model licenses include noncommercial terms. [openWakeWord project](https://github.com/dscripka/openWakeWord)
- Continuous ASR with text matching missed the test phrases when the recognizer decoded different words. The dedicated model constrains decoding to the current name and runs independently of command ASR.
