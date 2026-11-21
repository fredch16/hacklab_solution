# Verified technology references (retrieved 2026-11-21)

These are upstream starting points; recheck specific versions, docs, model weights and licenses before implementation. No implementation has been validated on the user's Pixel.

- Flutter WebRTC package, current mobile support and API: https://pub.dev/packages/flutter_webrtc
- sherpa-onnx Flutter package (offline ASR, VAD, keyword spotting support): https://pub.dev/packages/sherpa_onnx
- sherpa-onnx upstream examples and models (check exact model weights license): https://github.com/k2-fsa/sherpa-onnx
- Android Camera2 simultaneous stream combinations: https://developer.android.com/media/camera/camera2/multiple-camera-streams-simultaneously
- Android CameraCharacteristics mandatory stream combinations: https://developer.android.com/reference/android/hardware/camera2/CameraCharacteristics
- Android shared audio input rules and privacy-sensitive recording: https://developer.android.com/media/platform/sharing-audio-input
- Android 17 local network runtime permission requirements: https://developer.android.com/privacy-and-security/local-network-permission
- Apple AVFoundation capture session: https://developer.apple.com/documentation/avfoundation/avcapturesession
- Apple AVFoundation capture output combination limitations and iOS 16 behavior: https://developer.apple.com/documentation/avfoundation/avcapturesession/canaddoutput%28_%3A%29
- Apple audio session and interruptions: https://developer.apple.com/documentation/avfaudio/avaudiosession
- MDN WebRTC peer connection: https://developer.mozilla.org/en-US/docs/Web/API/RTCPeerConnection
- MDN browser secure contexts and permission policy: https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia

## Risk notes from the references

- The existence of Camera2/AVFoundation multi-output support **does not guarantee** a particular 4K60 + simultaneous low-latency stream combination. Query/benchmark actual hardware.
- `flutter_webrtc` lists partial/conditional `MediaRecorder` support for native platforms; don't rely on it to implement production master recording while simultaneously publishing a low-res track without testing native hooks.
- sherpa-onnx supports local mobile KWS, VAD and ASR, but **weights, language models, licensing, memory and speed must be selected and benchmarked**; don't blindly claim a wake phrase works.
- Android 17 / target API 37 introduces `ACCESS_LOCAL_NETWORK`; permission handling depends on target SDK and must not be copy/pasted unconditionally.
- A PC browser opening a LAN dashboard needs a trustworthy HTTPS/WSS origin for robust modern web features. Pairing and TLS cannot be an afterthought even on an offline LAN.
