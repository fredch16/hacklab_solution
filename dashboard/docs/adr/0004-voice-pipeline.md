# ADR 0004 — Offline wake word + command transcription

**Date:** 2026-11-21 · **Status:** implemented, awaiting owner microphone and browser acceptance · **Slices:** 3–4

## Owner decisions (2026-11-21)
- Wake phrase is **Hey + a user-defined name** in the web dashboard settings. The phone persists the name. Existing saved phrases retain their text after the fixed prefix, and a saved name without the prefix gains it.
- **English only.** The latest demo prioritizes wake and recording command accuracy in ordinary rooms. Text is shown as a final transcript after the command.
- Command ends on **~1 s of silence** (VAD), hard cap **15 s**.
- Transcripts kept for the **current session only**, in memory; nothing written to disk.
- Voice runs **simultaneously** with recording and streaming from the single `AudioCaptureBus` (ADR 0003).

## Android engine
`sherpa-onnx` (Apache-2.0) on Android, all models bundled in the APK (no download at runtime):
- **Noise suppression:** DPDFNet Baseline feeds endpoint detection during a command. Wake detection and final transcription use the resampled mic audio. This choice followed a replay where competing speech and room reflections degraded wake detection with fully enhanced input. The master recorder receives capture PCM directly from the shared bus. Endpoint detection falls back to its original input if the enhancer fails. [Streaming model](https://k2-fsa.github.io/sherpa/onnx/speech-enhancement/dpdfnet.html)
- **KWS:** sherpa-onnx open-vocabulary Zipformer `zh-en-3M-2025-12-20`, using English CMU phonemes and chunk-8 models. The configured name is compiled on the phone using the bundled dictionary and Flite letter-to-sound rules, including names outside the dictionary. The existing GigaSpeech BPE model can encode a name if no pronunciation is available. The spotter starts fresh after a command, mute, stream restart or settings change.
- **Wake handoff:** the command recognizer receives up to 1.25 s of raw PCM before the wake event, followed by the command. The longer context helps preserve the first word. PCM exists only in memory.
- **VAD:** Silero VAD (MIT) for 1 s end-of-speech detection. A level gate based on the wake and command helps quieter background speech avoid holding the command open.
- **ASR:** bundled Whisper base.en int8 after endpoint detection. The phone publishes the final text and an independent constrained phoneme transcription for recording phrases. The paired dashboard matches the whole request, allowing optional polite words, and requires agreement between the two decodes. The 15 s cap can produce a transcript. The dashboard only acts on silence-ended speech.
Model sources and licenses are recorded with the Android assets. Physical microphone, CPU, latency and thermal measurements during the combined workload are pending. [Model documentation](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)

## Alternatives considered

- Porcupine has an Android PCM API and dedicated per-phrase models. Its AccessKey and platform-specific model setup would add provisioning when the operator changes names. [Porcupine Android docs](https://picovoice.ai/quick-start/wake-word-android/)
- openWakeWord needs a trained model for each new phrase. Its example pretrained model licenses include noncommercial terms. [openWakeWord project](https://github.com/dscripka/openWakeWord)
- Continuous ASR with text matching missed the test phrases when the recognizer decoded different words. The dedicated model constrains decoding to the current name and runs independently of command ASR.


## Noise suppression research

- Google Home research combined microphone-array enhancement with keyword spotting feedback and measured gains with TV or music interference. Our Android app receives the audio that Android routes to its shared capture session. [Google Research](https://research.google/pubs/supervised-noise-reduction-for-multichannel-keyword-spotting/)
- Home Assistant details openWakeWord training with background noise augmentation. Its Linux Voice Assistant supports WebRTC gain and noise suppression ahead of wake processing. [Home Assistant wake-word approach](https://www.home-assistant.io/voice_control/about_wake_word) [Linux Voice Assistant](https://github.com/OHF-Voice/linux-voice-assistant/blob/main/docs/configuration.md)
- We compared the existing path with the GTCRN and DPDFNet streaming models. DPDFNet improved the most on the fan-like mixtures we tried. Gains depend on the noise, especially when nearby people are speaking. The voice path has no playback reference for echo cancellation. [GTCRN](https://github.com/Xiaobin-Rong/gtcrn) [Replay evidence](../evidence/slice-03-noise-2026-11-21.md)


## Android and hands-free dictation comparison

- Handy buffers speech and transcribes the clip locally, with Whisper and Parakeet among its model options. FUTO Voice Input documents Whisper and Parakeet alongside its streaming models. Sayso separates its hands-free wake detector from dictation. [Handy](https://github.com/cjpais/Handy) [FUTO Voice Input](https://docs.keyboard.futo.tech/improvements/voice-input-nightly-september-2026) [Sayso](https://github.com/stevengonsalvez/sayso)
- We replayed the same commands with the previous Zipformer, X-ASR, Whisper base.en and tiny.en, and Moonshine Base. Whisper base.en with raw pre-roll gave the strongest transcription on the tested command recordings. [Replay evidence](../evidence/slice-04-command-accuracy-2026-11-21.md)
- Google documents Voice Match as identifying the speaker for personalized results. Target-speaker extraction is a distinct option for overlapping talkers. A future enrollment feature needs a decision on profile storage and deletion. [Google Voice Match](https://support.google.com/googlehome/answer/7320960) [VoxLock](https://k2-fsa.github.io/sherpa/onnx/c-api/html/speech_enhancement.html)
