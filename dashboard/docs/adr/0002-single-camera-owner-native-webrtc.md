# ADR 0002 — One native CameraX owner; native libwebrtc sender

**Date:** 2026-11-21 · **Status:** accepted (lead) · **Slice:** 1 (prepares Slice 2)

## Decision
- **One camera owner in Kotlin** (`CameraSession`): CameraX bound to the activity lifecycle, back main camera, with use cases
  - `Preview` → Flutter `TextureRegistry.SurfaceProducer` (phone preview),
  - `ImageAnalysis` 640×360 target, YUV_420_888, `STRATEGY_KEEP_ONLY_LATEST`, target FPS range 30 → frames converted to I420 and fed to WebRTC **only while a peer is active** (otherwise closed immediately).
  - Slice 2 adds `VideoCapture<Recorder>` as the third use case on the **same** binding; Preview+ImageAnalysis+VideoCapture must be verified on the Pixel then (capability-probe; fall back transparently).
- **WebRTC runs natively** with `io.github.webrtc-sdk:android` (the libwebrtc build flutter_webrtc uses) — no `flutter_webrtc` / `getUserMedia`, so WebRTC never opens the camera. Hardware encoders via `DefaultVideoEncoderFactory`, `iceServers = []`, one peer.
- **Signaling** rides the existing authenticated WSS (`/api/v1/events`), non-trickle (complete ICE gathering before sending SDP); Dart relays to Kotlin over a MethodChannel.
- QR scanner (`mobile_scanner`) is a second camera client used only during pairing: the session camera is **unbound while the scanner page is open** and rebound after. Never simultaneous.

## Why
A plugin-owned WebRTC camera cannot also feed a high-quality native recorder (spec warns). CameraX gives one session with multiple outputs; libwebrtc accepts external frames via `CapturerObserver`. The browser's mDNS-obfuscated host candidates are fine: the browser connects to the phone's raw host candidate and the phone learns the browser via peer-reflexive candidates.

## Risks
CPU copy of 640×360 YUV→I420 (~0.35 MB/frame at 30 FPS) — measure. Device-specific use-case combination limits in Slice 2. H.264 availability depends on negotiated hardware encoder; report actual codec/encoder.

## Notes (Slice 1 device findings)
- **Hotspot candidates.** libwebrtc's Android `NetworkMonitor` enumerates only `ConnectivityManager` networks, so the phone's own hotspot/tether interface (`ap_br_wlan*`, `swlan*`) never produced host candidates and a PC on the phone's hotspot could not connect. `PeerConnectionFactory.Options.disableNetworkMonitor = true` makes libwebrtc fall back to `getifaddrs` enumeration (all up interfaces with an address) and sockets are no longer bound to one `Network`, so checks arriving on the hotspot interface are answered by kernel routing.
- **No WAN.** `networkIgnoreMask` excludes cellular adapter types (`rmnet*`: CELLULAR, 2G-5G = 4|64|128|256|512), so no cellular/CGNAT candidates are gathered. VPN and loopback are ignored by default. The answer's local candidates (ip, proto, type) are logged at logcat tag `LVC`.
- Trade-off: without the monitor, network changes are noticed by libwebrtc's periodic poll (about 2 s) instead of platform callbacks. Candidate gathering is one-shot (`GATHER_ONCE`), so an IP change needs a new Start video, which matches the "no auto-restart" product rule. Not verified on the Pixel yet.
