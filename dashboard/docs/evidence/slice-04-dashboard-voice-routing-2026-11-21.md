# Slice 4 follow-up — Dashboard voice routing

2026-11-21 · Android debug/arm64 · Chromium 158.0.8127.36 (localhost browser fixture)

The owner reported that on-device wake and transcription in the previous build worked well enough for the demo. This follow-up changes where recording requests are decided. The model files, speech input, decoding and thresholds are unchanged.

## Automated evidence

- Dashboard build and tests (`npm run build && npm test`) passed 24 tests. Cases include acoustic agreement, negation, similar-sounding phrases, nonfinal events, snapshots and the duration cap.
- Flutter analysis was clean. `flutter test` passed 74 tests, including the regression test where a native final sends text without touching the recorder. Repeating an authenticated recording request ID starts the recorder once.
- `./gradlew :app:testDebugUnitTest` passed the Android unit tests. `flutter build apk --debug --target-platform android-arm64` built the APK. SHA-256 `865734868fc90c3d7543ac3786ff3b555a0eb09183e3b658bc2130b973435258`.
- A Chromium WebSocket test drove the actual dashboard assets with a locally mocked phone. A confirmed start final sent exactly `recording.start` with `voice:utt-1` and showed the successful reply. A duplicated final and a stop final without acoustic agreement sent nothing. A confirmed stop sent exactly `recording.stop` with `voice:utt-3` and displayed the returned `NOT_RECORDING` failure.

The APK installed successfully with `adb install -r` on the Pixel 10 Pro XL (Android 17/API 37). Installation preserved app data.

## Owner test on Pixel and paired PC

1. Pair the dashboard over LAN, start video and say “Hey [saved name], start recording”. Check the browser’s confirmation and the phone’s recording timer.
2. Say “Hey [saved name], stout recording” and “Hey [saved name], don’t stop recording”. The active recording should continue. Say “Hey [saved name], stop recording” and verify the final file and browser confirmation.
3. While recording, close the dashboard. Verify the recording continues and that reopening the dashboard only restores transcript history. Start video again before giving a new voice command.
4. Repeat with WAN off and LAN connected. Verify audio in the recording and verify phone and browser Record/Stop buttons.

**NOT TESTED:** live microphone acceptance with the new routing, recording playback, and WAN-off verification need the owner on Pixel/PC. iOS needs an iPhone and macOS build host. The MCP connection is a future dashboard-side integration.
