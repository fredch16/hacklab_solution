import 'dart:async';

import 'package:flutter/services.dart';

class WebrtcFailure implements Exception {
  const WebrtcFailure(this.code, [this.message]);

  /// CAMERA_UNAVAILABLE | NEGOTIATION_FAILED | BAD_REQUEST
  final String code;
  final String? message;
}

/// Native camera owner + WebRTC sender (Kotlin). Events are maps with an
/// `event` key: state, preview, stats, stream, webrtc_stopped.
abstract class CameraPlatform {
  Stream<Map<String, Object?>> get events;

  /// Texture id, or null when the camera could not start.
  Future<int?> start();
  Future<void> stop();
  Future<void> pause();
  Future<void> resume();
  Future<String> offer(String sdp);
  Future<void> stopStream(String reason);
}

class ChannelCameraPlatform implements CameraPlatform {
  static const _method = MethodChannel('dev.localvision/camera');
  static const _events = EventChannel('dev.localvision/camera_events');

  @override
  Stream<Map<String, Object?>> get events => _events
      .receiveBroadcastStream()
      .map((e) => Map<String, Object?>.from(e as Map))
      .handleError((Object _) {});

  Future<T?> _call<T>(String m, [Object? args]) async {
    try {
      return await _method.invokeMethod<T>(m, args);
    } on MissingPluginException {
      return null; // iOS side arrives in a later slice
    } on PlatformException {
      return null;
    }
  }

  @override
  Future<int?> start() async {
    final r = await _call<Map<Object?, Object?>>('start');
    return r?['textureId'] as int?;
  }

  @override
  Future<void> stop() => _call<void>('stop');
  @override
  Future<void> pause() => _call<void>('pause');
  @override
  Future<void> resume() => _call<void>('resume');

  @override
  Future<String> offer(String sdp) async {
    try {
      final r = await _method.invokeMethod<Map<Object?, Object?>>(
        'webrtcOffer',
        {'sdp': sdp},
      );
      final answer = r?['sdp'] as String?;
      if (answer == null) throw const WebrtcFailure('NEGOTIATION_FAILED');
      return answer;
    } on PlatformException catch (e) {
      throw WebrtcFailure(e.code, e.message);
    } on MissingPluginException {
      throw const WebrtcFailure(
        'CAMERA_UNAVAILABLE',
        'Camera is not available on this platform.',
      );
    }
  }

  @override
  Future<void> stopStream(String reason) =>
      _call<void>('webrtcStop', {'reason': reason});
}
