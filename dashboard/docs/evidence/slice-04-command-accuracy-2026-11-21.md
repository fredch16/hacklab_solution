# Offline command accuracy

## Acceptance

- Replay the same spoken commands with raw and enhanced input. Include immediate commands, commands after a short pause, noncommands, “stout recording”, and a different wake name. Count wake hits, correct command directions, and unintended recording actions separately.
- Verify 5 s no-speech, ~1 s endpoint, 15 s cap, repeated wakes, mute and stream restart. Decode once at the endpoint for normal transcriptions. Keep final text distinct from whether a recording command was accepted.
- On the Pixel with video active and recording supported, verify “Hey Alexa, start recording” and “Hey Alexa, stop recording” in quiet and amid background speech. Also try “Hey Alexa, stout recording”, “Hey Alexa, don’t stop recording”, and “Hey Google, stop recording”. None of the last three should change recording. Check both UIs and with WAN disabled. Owner real-microphone acceptance is pending.

## Diagnosis and model selection

A Piper UK voice saying “Hey Alexa start recording” reproduced the reported
“stout recording” result with the previous streaming Zipformer on both raw
and DPDFNet-enhanced audio using the 320 ms pre-roll. A separate unit case
reproduced an unrelated utterance containing “record” being treated as a
recording command.

The host used sherpa-onnx 2.0.2 and a mixture of 24 US and UK Piper test
utterances per condition. This table covers the 20 clips per condition that
passed the then-current wake stage. Rows compare exact normalized command or
dictation text after removing the wake prefix. Fan-like noise is included for
the initial model comparison. The ordinary-room test below is the active
product criterion.

| Input and model | Quiet exact text | Fan mixture +5 dB exact text |
| --- | ---: | ---: |
| Previous Zipformer, enhanced input, 320 ms pre-roll | 12/20 | 6/20 |
| X-ASR 480 ms, raw input, 320 ms pre-roll | 13/20 | 12/20 |
| Whisper tiny.en, raw input, 1.5 s pre-roll | 19/20 | 12/20 |
| Moonshine Base quantized, raw input, 1.5 s pre-roll | 17/20 | 14/20 |
| Whisper base.en, raw input, 1.25 s pre-roll | 18/20 | 16/20 |

The older wake path exposed 2/8 intended Alexa activations in a generated
reverberant room mix with ongoing background conversation, compared with
8/8 on raw input with the revised keyword threshold on the host. The
configured name remained editable. The two different-name quiet controls
were rejected. With the unmodified Silero-only condition, the ongoing
background speakers held the command open in the room fixtures. Referencing
the wake utterance's energy let the command endpoint fire despite quieter
background speech.

Two Whisper windows agreed on the wrong “stop recording” interpretation in
one Pixel room replay of “stout recording”. An independent Zipformer
phoneme pass separated “stout recording” from the recording commands in
the host replay. The Android app requires agreement between Whisper and
this phoneme pass before it runs an action. It suppresses actions on a
hard-duration cap or an ambiguous acoustic result.

## Installed Pixel replay

- Pixel 10 Pro XL, Android 17/API 37, build CP4A.261105.040.A1, arm64
  debug APK `0.1.0+1`, sherpa-onnx 2.0.2. Built with Flutter 3.50.1
  and installed with `adb install -r`. APK SHA-256
  `7454f692841ffde388ad2440d755601e5d06f0819c4c47a77d17ebc8027152c2`.
- The probe fed 48 kHz PCM into the installed app's actual resampler,
  denoiser, wake detector, endpoint logic, Whisper and phoneme checker.
  It compared the resulting transcript and command signal using the
  app's documented command grammar. Flutter tests separately verified
  command dispatch and rejection when the acoustic result is missing.
- For the room condition, the target was mixed 8 dB above two continuing
  background speech clips with 35 ms and 73 ms reflections. This is a
  synthetic reverberant condition with no calibrated speaking distance.

| Condition | Wake on intended commands | Correct command | Wrong action on negative input |
| --- | ---: | ---: | ---: |
| Quiet | 4/4 | 4/4 | 0/6 |
| Room conversation mix | 3/4 | 2/4 | 0/4 |

The room misses were one missed wake and one transcript with background
words. Whisper still produced "stop recording" on one noisy "stout
recording" clip, and the phoneme check rejected the action. The two quiet
"Hey Google" clips never woke the configured Alexa listener. The probe
reported no interim transcripts. Final decoding and the acoustic check took
0.51–0.96 s after endpoint in this fixture run. An earlier phoneme check
using a cropped audio window missed the quiet commands. The final build
checks the full captured clip and separates the one-word "record" alias
from longer command phrases.

`flutter analyze` passed, `flutter test` passed 81 tests,
`./gradlew :app:testDebugUnitTest` passed 24 tests,
`npm run build && npm test` passed 23 tests, and the Android APK built
successfully. **NOT TESTED** with the owner's microphone and speaking
distance, concurrent real recording and streaming load, WAN disabled, or
iPhone. A voice enrollment feature requires a separate owner decision about
speaker behavior and local profile storage.
