# Laptop dashboard and two recorders — 22 November 2026

- TypeScript rebuilt and all 27 dashboard tests passed, including explicit phone/robot speech routing and the acoustic mismatch rule.
- The laptop integration suite exercised pinned TLS pairing, phone recording acknowledgement through MCP, robot feed lease enforcement, a recording refused in a different tab, and valid video-only FFmpeg output after websocket closure. It also checked the loose-phrasing gate.
- Chromium 158.0.8127.36 used the real dashboard with a generated JPEG feed. It showed a live preview, started a robot recording, finalized the clip on a visibility change, and downloaded the resulting MKV. Reload did not resume recording.
- The existing simulated arm MCP and voice bridge suites both passed. On the physical arm, a read-only request from the new dashboard gateway went through the updated bridge and MCP and reported the arm at `top`. No move or arm stop was issued during this check.
- Flutter analysis and all 74 Flutter tests passed. An arm64 debug build installed successfully on the Pixel 10 Pro XL. After the operator granted raw USB access, restarting Station and the arm capture service recovered the real robot camera. The gateway returned three live JPEGs of 266677, 267029 and 266372 bytes, measured at 960×720. A read-only command through the same stack reported the arm at `home`.
- **NOT TESTED:** owner phone speech, real pairing and WebRTC from the new dashboard, both recorders together, a saved clip from the real robot feed, or live Anthropic with the new routing.

## Owner acceptance on Pixel and laptop

1. Open the laptop page and pair from the phone. Start phone video. Verify the phone feed and fresh transcripts, then disconnect and reconnect. Verify that old transcripts are shown without being executed.
2. Verify Station's USB feed is live. Observe both camera panels, start both recordings, and verify the phone's master has audio and the robot clip opens from the download link without audio.
3. Start a robot clip, then switch tabs, refresh, close the dashboard, scroll the robot feed away, and interrupt the robot stream in separate runs. Each run should stop and save the clip. Reopening must never resume. The phone recording should continue until independently stopped.
4. Speak the explicit start/stop phrases from the [laptop guide](../LAPTOP_DASHBOARD.md), including the unqualified phone default. Try a loose recording request with Claude enabled and an ambiguous or negated request. Verify the robot recorder refuses to start without a visible live feed.
5. Verify local video, both manual controls, and the fixed grammar with the WAN disabled. Verify the arm operator controls as a separate, supervised hardware check.

## Owner robot recording follow-up

The owner recorded a real Station feed on the laptop. FFprobe verified a 39.32 s, 960×720, video-only Matroska clip. It was moved to the Git-ignored `Camera recordings/` directory without restarting the paired dashboard. The download endpoint still returned the complete file.

The Pixel continued processing microphone input with zero reported audio drops during that clip and showed Listening. The native logs contained no wake detection during the clip. The cause of the missed wake phrase has not been established. A same-position spoken comparison with the robot recorder on and off is pending.

## Spoken robot stop follow-up

The phone delivered the complete transcript “stop robot recording” during an active 97.2 s robot clip. The voice bridge returned `command: none` after the acoustic agreement gate rejected it. The laptop now routes exact, affirmative robot recording stops directly to the requesting tab's recorder. The start and phone-stop acoustic checks still apply.

All 27 dashboard tests and six gateway tests passed. Chromium 158 exercised the real dashboard with a final transcript lacking an acoustic hint and verified a direct robot stop request. The Python integration suite started a fixture clip through the MCP bridge, stopped it with that exact text, and verified a saved result. A live spoken retry on the owner’s Pixel is pending.
