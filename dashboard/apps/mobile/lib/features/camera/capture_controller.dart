import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../platform/camera_platform.dart';
import '../connectivity/signaling.dart';

typedef MessageSender = void Function(
  String type,
  Map<String, Object?> payload, {
  String? requestId,
});

/// Dart view of the native camera/stream state machine. The camera runs only
/// while the PC streams or the QR scanner is open; native events drive state.
/// Camera states: not_started|initializing|previewing|transmitting|interrupted|error.
class CaptureController extends ChangeNotifier implements WebrtcPort {
  CaptureController(this.platform) {
    _sub = platform.events.listen(_onEvent, onError: (Object _) {});
  }

  final CameraPlatform platform;
  late final StreamSubscription<Map<String, Object?>> _sub;
  final _scans = StreamController<String>.broadcast();
  MessageSender? sender;

  String _native = 'not_started'; // initializing|previewing|error
  String? error;
  bool interrupted = false;
  int? textureId;
  int? previewWidth, previewHeight;
  int previewRotation = 0;

  String streamState = 'idle'; // idle|connecting|connected|failed
  String? streamError;

  Map<String, Object?>? captureStats;
  Map<String, Object?>? streamStats;

  /// Decoded QR strings from the native scanner (only while it holds the camera).
  Stream<String> get scans => _scans.stream;

  bool get permissionDenied => error == 'PERMISSION_CAMERA';

  String get state {
    if (_native == 'error') return 'error';
    if (_native == 'not_started') return 'not_started';
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

  /// Camera permission refused when the session started.
  void denyPermission() {
    _native = 'error';
    error = 'PERMISSION_CAMERA';
    notifyListeners();
  }

  /// Permission now granted; the camera itself starts on the next offer.
  void permissionGranted() {
    if (!permissionDenied) return;
    _native = 'not_started';
    error = null;
    notifyListeners();
  }

  /// QR scanner opened: take a hold on the camera.
  Future<bool> startForScanner() async {
    final id = await platform.start('scan');
    if (id != null) textureId = id;
    notifyListeners();
    return id != null;
  }

  Future<void> stopForScanner() => platform.stop('scan');

  Future<void> focus(double x, double y) => platform.focus(x, y);

  /// Session end: stop the peer (which releases the stream's camera hold).
  Future<void> end() async {
    await platform.stopStream('session_ended');
    await platform.stop('stream');
    interrupted = false;
    error = null;
    _native = 'not_started';
    _clearCamera();
    streamState = 'idle';
    streamError = null;
    notifyListeners();
  }

  void _clearCamera() {
    textureId = null;
    previewWidth = previewHeight = null;
    captureStats = streamStats = null;
  }

  void setInterrupted(bool v) {
    if (interrupted == v) return;
    interrupted = v;
    notifyListeners();
  }

  /// Native side starts the camera and waits for frames before answering.
  @override
  Future<String> offer(String sdp) async {
    if (permissionDenied) {
      throw const WebrtcFailure(
        'CAMERA_UNAVAILABLE',
        'Camera permission not granted on the phone',
      );
    }
    return platform.offer(sdp);
  }

  @override
  Future<void> stopStream(String reason) => platform.stopStream(reason);

  void _onEvent(Map<String, Object?> e) {
    switch (e['event']) {
      case 'state':
        final s = e['state'] as String;
        // A permission error stays until permission is granted.
        if (permissionDenied && s != 'previewing') return;
        _native = s;
        error = e['error'] as String?;
        if (s == 'not_started') _clearCamera();
        notifyListeners();
      case 'preview':
        textureId = (e['textureId'] as int?) ?? textureId;
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
      case 'scan':
        final t = e['text'];
        if (t is String) _scans.add(t);
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
    _scans.close();
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
