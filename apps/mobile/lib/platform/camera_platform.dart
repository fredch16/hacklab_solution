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

  /// Takes a hold on the camera for [user] ('stream' or 'scan'); resolves once
  /// frames arrive. Texture id, or null when it could not start.
  Future<int?> start(String user);

  /// Drops that hold; the camera stops when nobody needs it.
  Future<void> stop(String user);

  /// Tap-to-focus at a point normalised to the preview buffer (0..1).
  Future<void> focus(double x, double y);
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
  Future<int?> start(String user) async {
    final r = await _call<Map<Object?, Object?>>('start', {'user': user});
    return r?['textureId'] as int?;
  }

  @override
  Future<void> stop(String user) => _call<void>('stop', {'user': user});

  @override
  Future<void> focus(double x, double y) =>
      _call<void>('focus', {'x': x, 'y': y});

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
