# ADR 0003 — Master recording: one camera, one mic, own encoders + muxer

**Date:** 2026-11-21 · **Status:** accepted (lead) · **Slice:** 2 (prepares 3/4)

## Owner decisions (2026-11-21)
- Recording is started/stopped from the **web dashboard** (and the phone's own Record button); independent of Start video.
- Paired browser may start **and** stop recording (browser confirms stop).
- Quality and camera modes are chosen in the dashboard from **device-reported capabilities** (lens incl. ultra-wide/tele, resolution×fps, codec, stabilization, HDR, orientation lock, zoom, exposure, torch) — no Pixel-specific hardcoding.
- Recordings **always include audio**; wake word + transcription (Slices 3–4) must run **at the same time** from the same mic.
- Files saved to shared **Movies/LocalVision** (MediaStore).

## Decision
- **`AudioCaptureBus` (Kotlin)** is the only mic owner: one `AudioRecord` (source `CAMCORDER`, 48 kHz, stereo if supported else mono, PCM16), read on a dedicated thread, fanned out to subscribers with monotonic `System.nanoTime()` timestamps. Subscriber 1 = AAC encoder for recording. Slice 3 adds a 16 kHz mono resampled tap for KWS/VAD/ASR. CameraX `Recorder` is **not** used because it owns the mic internally.
- **Video:** the existing CameraX binding gains a third use case `VideoCapture<VideoOutput>` with a **custom `VideoOutput`** that provides a `MediaCodec` HEVC/AVC encoder input surface (spike first: if `VideoOutput` cannot be implemented reliably, fall back to a Camera2 capture session with the same three outputs — record the result here).
- **`MasterRecorder`:** video MediaCodec + AAC MediaCodec → `MediaMuxer` (MP4) writing into a MediaStore `Movies/LocalVision` item with `IS_PENDING=1`; `IS_PENDING=0` only after muxer stop succeeds → state `available`. Timestamps for both tracks on the same monotonic clock (camera frames are `SENSOR_TIMESTAMP` realtime → convert); video orientation via `setOrientationHint` (orientation-lock aware).
- Recording priority > stream > voice. Stream/peer failures never touch the recorder. Camera holder `record` keeps the camera running independently of `stream`/`scan`.
- No silent downgrade: requested profile not achievable → refuse start with a reason; thermal/storage warnings are surfaced, never auto-lowered.

## Risks
Custom VideoOutput relies on CameraX surface contract; A/V sync must be measured (clap test); 4K HEVC + 640×360 analysis + preview simultaneously must be benchmarked per device — capabilities mark combinations `untested` until a test recording passes.
