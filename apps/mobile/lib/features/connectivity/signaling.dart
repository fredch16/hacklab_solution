import 'dart:convert';

import '../../platform/camera_platform.dart';
import 'pairing.dart';

abstract class WebrtcPort {
  Future<String> offer(String sdp);
  Future<void> stopStream(String reason);
}

const maxSdpBytes = 64 * 1024;
const _maxMessageChars = maxSdpBytes + 4096;

/// Validates `webrtc.*` messages from the authenticated socket and relays
/// them to the native sender. The socket is already authenticated.
class SignalingRelay {
  SignalingRelay({required this.port, required this.send, Clock? clock})
    : _now = clock ?? DateTime.now;

  final WebrtcPort port;
  final void Function(
    String type,
    Map<String, Object?> payload, {
    String? requestId,
  })
  send;
  final Clock _now;
  DateTime? _lastOffer;

  void _error(String? id, String code, String message) => send('webrtc.error', {
    'request_id': id,
    'code': code,
    'message': message,
  }, requestId: id);

  Future<void> handle(String raw) async {
    if (raw.length > _maxMessageChars)
      return _error(null, 'BAD_REQUEST', 'Message too large.');
    Object? j;
    try {
      j = jsonDecode(raw);
    } catch (_) {
      return _error(null, 'BAD_REQUEST', 'Invalid JSON.');
    }
    if (j is! Map || j['version'] != 1 || j['type'] is! String) {
      return _error(null, 'BAD_REQUEST', 'Unsupported message.');
    }
    final id = j['request_id'];
    final payload = j['payload'];
    if (id is! String || id.isEmpty || id.length > 128 || payload is! Map) {
      return _error(null, 'BAD_REQUEST', 'Missing request_id or payload.');
    }
    switch (j['type']) {
      case 'webrtc.offer':
        final sdp = payload['sdp'];
        if (sdp is! String ||
            sdp.isEmpty ||
            utf8.encode(sdp).length > maxSdpBytes) {
          return _error(
            id,
            'BAD_REQUEST',
            'Offer SDP missing or larger than 64 KiB.',
          );
        }
        final now = _now();
        final last = _lastOffer;
        if (last != null && now.difference(last) < const Duration(seconds: 1)) {
          return _error(
            id,
            'BAD_REQUEST',
            'Only one offer per second is accepted.',
          );
        }
        _lastOffer = now;
        try {
          final answer = await port.offer(sdp);
          send('webrtc.answer', {
            'request_id': id,
            'sdp': answer,
          }, requestId: id);
        } on WebrtcFailure catch (e) {
          _error(id, e.code, e.message ?? 'WebRTC negotiation failed.');
        } catch (_) {
          _error(id, 'NEGOTIATION_FAILED', 'WebRTC negotiation failed.');
        }
      case 'webrtc.stop':
        await port.stopStream('browser_stop');
      default:
        _error(id, 'BAD_REQUEST', 'Unknown message type.');
    }
  }
}
