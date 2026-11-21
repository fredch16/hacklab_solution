import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../platform/camera_platform.dart';
import '../connectivity/signaling.dart';

typedef MessageSender = void Function(
  String type,
  Map<String, Object?> payload, {
  String? requestId,
});

/// Dart view of the native camera/stream state machine. Camera states:
/// not_started|initializing|previewing|transmitting|paused|interrupted|error.
class CaptureController extends ChangeNotifier implements WebrtcPort {
  CaptureController(this.platform) {
    _sub = platform.events.listen(_onEvent, onError: (Object _) {});
  }

  final CameraPlatform platform;
  late final StreamSubscription<Map<String, Object?>> _sub;
  MessageSender? sender;

  bool _started = false;
  String _native = 'not_started'; // initializing|previewing|paused|error
  String? error;
  bool interrupted = false;
  int? textureId;
  int? previewWidth, previewHeight;
  int previewRotation = 0;

  String streamState = 'idle'; // idle|connecting|connected|failed
  String? streamError;

  Map<String, Object?>? captureStats;
  Map<String, Object?>? streamStats;

  bool get permissionDenied => error == 'PERMISSION_CAMERA';

  String get state {
    if (!_started) return 'not_started';
    if (_native == 'error') return 'error';
    if (interrupted) return 'interrupted';
    if (_native == 'previewing' && streamState == 'connected')
      return 'transmitting';
    return _native;
  }

  bool get live => state == 'previewing' || state == 'transmitting';

  Map<String, Object?> cameraJson() => {
    'state': state,
    'error': state == 'error' ? error : null,
    'lens': 'back',
  };
  Map<String, Object?> streamJson() => {
    'state': streamState,
    'error': streamError,
  };

  Future<void> start() async {
    _started = true;
    _native = 'initializing';
    error = null;
    notifyListeners();
    final id = await platform.start();
    if (id != null) {
      textureId = id;
    } else if (error == null) {
      _native = 'error';
      error = 'CAMERA_FAILED';
    }
    notifyListeners();
  }

  void denyPermission() {
    _started = true;
    _native = 'error';
    error = 'PERMISSION_CAMERA';
    notifyListeners();
  }

  Future<void> end() async {
    if (streamState != 'idle') await platform.stopStream('session_ended');
    if (_started) await platform.stop();
    _started = false;
    _native = 'not_started';
    error = null;
    interrupted = false;
    textureId = null;
    previewWidth = previewHeight = null;
    captureStats = streamStats = null;
    streamState = 'idle';
    streamError = null;
    notifyListeners();
  }

  void setInterrupted(bool v) {
    if (interrupted == v) return;
    interrupted = v;
    notifyListeners();
  }

  /// Release the camera for the QR scanner. Resolves once it is unbound.
  Future<void> pauseForScanner() async {
    if (!_started || _native == 'error') return;
    _native = 'paused';
    notifyListeners();
    await platform.pause();
  }

  Future<void> resumeFromScanner() async {
    if (_native != 'paused') return;
    _native = 'initializing';
    notifyListeners();
    await platform.resume();
  }

  @override
  Future<String> offer(String sdp) async {
    if (!live) {
      throw const WebrtcFailure(
        'CAMERA_UNAVAILABLE',
        'The phone camera is not running.',
      );
    }
    return platform.offer(sdp);
  }

  @override
  Future<void> stopStream(String reason) => platform.stopStream(reason);

  void _onEvent(Map<String, Object?> e) {
    switch (e['event']) {
      case 'state':
        _native = e['state'] as String;
        error = e['error'] as String?;
        notifyListeners();
      case 'preview':
        previewWidth = e['width'] as int?;
        previewHeight = e['height'] as int?;
        previewRotation = (e['rotationDegrees'] as int?) ?? 0;
        notifyListeners();
      case 'stream':
        streamState = e['state'] as String;
        streamError = e['error'] as String?;
        notifyListeners();
      case 'webrtc_stopped':
        sender?.call('webrtc.stopped', {'reason': e['reason']});
      case 'stats':
        captureStats = _map(e['capture']);
        streamStats = _map(e['stream']);
        if (live) {
          sender?.call('camera.stats', statsPayload(captureStats, streamStats));
        }
        notifyListeners();
    }
  }

  static Map<String, Object?>? _map(Object? o) =>
      o == null ? null : Map<String, Object?>.from(o as Map);

  @override
  void dispose() {
    _sub.cancel();
    super.dispose();
  }
}

const _captureKeys = ['width', 'height', 'fps_measured'];
const _streamKeys = [
  'codec',
  'encoder',
  'width',
  'height',
  'fps',
  'bitrate_bps',
  'quality_limitation',
  'rtt_ms',
  'local_candidate',
  'remote_candidate',
];

/// Contract `camera.stats` payload: every key present, null when unknown;
/// `stream` is null unless a peer is connected.
Map<String, Object?> statsPayload(
  Map<String, Object?>? capture,
  Map<String, Object?>? stream,
) => {
  'capture': {for (final k in _captureKeys) k: capture?[k]},
  'stream': stream == null ? null : {for (final k in _streamKeys) k: stream[k]},
};
