# Pairing starts voice — 22 November 2026

- The session controller regression test used a real paired HTTPS/WebSocket connection on loopback and a test camera platform. It failed before the fix because no voice start happened with idle video. It now checks voice starts with the camera closed, starting and stopping video does not interrupt voice, backgrounding pauses voice, returning re-arms it, and disconnecting stops it.
- `flutter analyze` passed, all 76 Flutter tests passed, all 27 dashboard tests passed, and the arm64 debug APK built.
- Owner check on Pixel and paired laptop: start Local Vision, pair by QR without clicking **Start video**, wait for **Listening**, then say “Hey [wake name], where are you?” Verify a transcript and read-only arm status with the phone camera off. Start and stop video and repeat. Close the dashboard tab to verify listening stops.
- APK installed on the Pixel 10 Pro XL (Android 17) with `adb install -r`; app data was retained. **NOT TESTED:** real speech with the new build and phone video off. The owner must re-open Local Vision and re-pair after installation.
