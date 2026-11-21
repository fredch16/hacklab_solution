# Developer start guide and recommended repo boundaries

**This is a proposed structure, not existing code.** Inspect the repo and reuse working conventions before adopting it. Do not spawn Python/Node on the PC just to use the dashboard.

## Suggested tree after implementation starts

```text
apps/
  mobile/                         # Flutter app, Android/iOS runners
    lib/
      app/                         # routing, dependency injection, app state
      features/
        capture/                   # local camera preview + profiles
        recording/                 # local recording and library
        voice/                     # KWS/VAD/ASR transcript and controls
        connectivity/              # LAN session/pairing/signaling
        transfer/                  # on-phone file manifest/download server
      platform/                    # abstract media/audio adapters and bridges
    android/                       # native CameraX/Camera2/AudioRecord etc.
    ios/                           # AVFoundation/AVAudioSession adapters
  dashboard/                       # web frontend sources built to mobile assets
packages/
  protocol/                        # JSON versioned message schemas/tests
  design_tokens/                   # shared tokens or generated docs
models/                            # manifest only; weights bundled/imported as approved
scripts/                           # repeatable build/test/asset packaging
ui/reference-dashboard.html        # static designer reference (NOT app)
docs/ ...
```

## Preferred tools

- Flutter stable matching `flutter_webrtc` and chosen native camera plugin version. Check package Flutter minimum, build tool versions, iOS deployment floor and Android minSdk before locking.
- Flutter `flutter_webrtc` for cross-platform peer connection + media receiving/sending, **not** assumed to solve concurrent native video muxing automatically.
- Native Android capture via Camera2/CameraX/MediaCodec as needed; native iOS via AVFoundation. Abstract into `CameraSession`, `VideoRecordService`, `AudioCaptureBus` interfaces.
- `sherpa_onnx` for local KWS/VAD/ASR once model language/license and device performance are proven.
- Browser app: plain TypeScript or React, bundled entirely in APK/IPA static assets. It connects directly to the phone; no runtime cloud dependencies.
- Dart embedded server (`dart:io` / well-maintained router) for local HTTPS/WSS, pairing and file transfer. Use a trusted, tested TLS stack.
- Standard mobile persistent storage APIs with safe app-private recording paths or platform media library export as user prefers; avoid unrestricted filesystem permissions unless necessary.

## Toolchain commands (once repo is implemented)

```bash
flutter doctor -v
flutter devices
flutter pub get
flutter analyze
flutter test
# On connected Android phone:
flutter run -d <device-id>
# In dashboard source, use its declared package scripts; do not assume npm until chosen.
```

For iOS physical testing, macOS + Xcode, provisioning, an iPhone, camera/mic permission strings and local network entitlement/usage strings are required. Container/Linux cannot validate an iOS device build.

## Run without Internet — distinction

Initial dependency installation and model download by the **developer** can use Internet. Running the installed app during a shoot **must not**. Pin/ship local dashboard assets and on-device models and prove offline mode by disconnecting router WAN while maintaining local Wi-Fi.

## Dependency acceptance checklist

- Active upstream, compatible Android and iOS APIs, license permits intended distribution/use.
- Actual device operation for camera/record/audio multistream, not just example screenshots.
- No hidden analytics, Firebase, cloud service, public ICE/STUN/TURN or model download at runtime.
- No independent second mic/camera owner.
- UI still responsive under full recording + KWS/ASR + streaming.

## Coding agent setup

One agent does all the work; no subagents. See `CLAUDE.md` and `docs/STATUS.md`.

## Agent cooperation

Different agents may independently research **UI**, **native camera**, **voice models** and **security** after a lead defines the shared interfaces. However, **only one integration owner** merges changes and runs each end-to-end slice. Do not dispatch several agents to build disconnected vertical layers that cannot be integrated. Always test earlier slices after merging.
