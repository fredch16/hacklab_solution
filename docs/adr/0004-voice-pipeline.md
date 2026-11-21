# ADR 0004 — Offline wake word + command transcription

**Date:** 2026-11-21 · **Status:** proposed (lead; model choice to be verified on device) · **Slices:** 3–4

## Owner decisions (2026-11-21)
- Wake phrase is **user-defined, typed in the web dashboard settings** (no fixed phrase).
- **English only.**
- Command ends on **~1 s of silence** (VAD), hard cap **15 s**.
- Transcripts kept for the **current session only**, in memory; nothing written to disk.
- Voice runs **simultaneously** with recording and streaming from the single `AudioCaptureBus` (ADR 0003).

## Proposed engine
`sherpa-onnx` (Apache-2.0) on Android, all models bundled in the APK (no download at runtime):
- **KWS:** open-vocabulary streaming zipformer keyword spotter (English, BPE) — the typed phrase is tokenized on the phone at runtime, so any phrase works without retraining. Sensitivity via keyword threshold/boost; cooldown after a hit.
- **VAD:** Silero VAD (MIT) for 1 s end-of-speech detection.
- **ASR:** streaming English zipformer transducer (partial + final results), started on wake, stopped at VAD endpoint/15 s.
Licenses and exact model files verified and recorded before shipping; on-device latency/CPU measured while recording 1080p60 + streaming.
