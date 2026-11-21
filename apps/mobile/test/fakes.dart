import 'dart:async';

import 'package:local_vision/platform/camera_platform.dart';

class FakeCameraPlatform implements CameraPlatform {
  final controller = StreamController<Map<String, Object?>>.broadcast(
    sync: true,
  );
  final calls = <String>[];
  int? textureId = 7;
  WebrtcFailure? offerFailure;
  String answer = 'v=0 answer';
  final offers = <String>[];

  @override
  Stream<Map<String, Object?>> get events => controller.stream;

  void emit(Map<String, Object?> e) => controller.add(e);

  @override
  Future<int?> start() async {
    calls.add('start');
    emit({'event': 'state', 'state': 'previewing', 'error': null});
    return textureId;
  }

  @override
  Future<void> stop() async => calls.add('stop');
  @override
  Future<void> pause() async {
    calls.add('pause');
    emit({'event': 'state', 'state': 'paused', 'error': null});
  }

  @override
  Future<void> resume() async {
    calls.add('resume');
    emit({'event': 'state', 'state': 'previewing', 'error': null});
  }

  @override
  Future<String> offer(String sdp) async {
    calls.add('offer');
    offers.add(sdp);
    if (offerFailure != null) throw offerFailure!;
    return answer;
  }

  @override
  Future<void> stopStream(String reason) async {
    calls.add('stopStream:$reason');
    emit({'event': 'webrtc_stopped', 'reason': reason});
    emit({'event': 'stream', 'state': 'idle', 'error': null});
  }
}
