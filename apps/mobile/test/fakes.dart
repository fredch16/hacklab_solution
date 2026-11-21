import 'dart:async';

import 'package:local_vision/platform/camera_platform.dart';

/// Mimics the Kotlin rules: the camera runs while a "stream" or "scan" hold exists.
class FakeCameraPlatform implements CameraPlatform {
  final controller = StreamController<Map<String, Object?>>.broadcast(
    sync: true,
  );
  final calls = <String>[];
  final users = <String>{};
  int? textureId = 7;
  WebrtcFailure? offerFailure;
  String answer = 'v=0 answer';
  final offers = <String>[];
  bool _peer = false;

  bool get cameraRunning => users.isNotEmpty;

  @override
  Stream<Map<String, Object?>> get events => controller.stream;

  void emit(Map<String, Object?> e) => controller.add(e);

  @override
  Future<int?> start(String user) async {
    calls.add('start:$user');
    if (users.isEmpty) {
      emit({'event': 'state', 'state': 'initializing', 'error': null});
      emit({'event': 'state', 'state': 'previewing', 'error': null});
      emit({
        'event': 'preview',
        'textureId': textureId,
        'width': 1280,
        'height': 720,
        'rotationDegrees': 90,
      });
    }
    users.add(user);
    return textureId;
  }

  void _release(String user) {
    users.remove(user);
    if (users.isEmpty)
      emit({'event': 'state', 'state': 'not_started', 'error': null});
  }

  @override
  Future<void> stop(String user) async {
    calls.add('stop:$user');
    _release(user);
  }

  @override
  Future<void> focus(double x, double y) async => calls.add('focus:$x,$y');

  @override
  Future<String> offer(String sdp) async {
    calls.add('offer');
    offers.add(sdp);
    emit({'event': 'stream', 'state': 'connecting', 'error': null});
    if (offerFailure != null) {
      emit({
        'event': 'stream',
        'state': 'failed',
        'error': 'CAMERA_UNAVAILABLE',
      });
      throw offerFailure!;
    }
    await start('stream');
    _peer = true;
    return answer;
  }

  @override
  Future<void> stopStream(String reason) async {
    calls.add('stopStream:$reason');
    if (!_peer) return;
    _peer = false;
    emit({'event': 'webrtc_stopped', 'reason': reason});
    emit({'event': 'stream', 'state': 'idle', 'error': null});
    _release('stream');
  }
}
