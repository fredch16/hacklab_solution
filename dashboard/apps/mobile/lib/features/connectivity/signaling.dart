import 'dart:convert';

import '../../platform/camera_platform.dart';
import 'pairing.dart';

abstract class WebrtcPort {
  Future<String> offer(String sdp);
  Future<void> stopStream(String reason);
}

abstract class RecordingPort {
  /// Null on success, else an error code (PERMISSION_MIC, LOW_STORAGE, ...).
  Future<String?> startRecording();
  Future<String?> stopRecording();
}

abstract class VoicePort {
  /// Null on success, else INVALID_WAKE_PHRASE.
  Future<String?> setPhrase(String phrase);
  Future<String?> setMuted(bool muted);
}

const maxSdpBytes = 64 * 1024;
const _maxMessageChars = maxSdpBytes + 4096;

/// Validates `webrtc.*` messages from the authenticated socket and relays
/// them to the native sender. The socket is already authenticated.
class SignalingRelay {
  SignalingRelay({
    required this.port,
    this.recording,
    this.voice,
    required this.send,
    Clock? clock,
  }) : _now = clock ?? DateTime.now;

  final WebrtcPort port;
  final RecordingPort? recording;
  final VoicePort? voice;
  final void Function(
    String type,
    Map<String, Object?> payload, {
    String? requestId,
  })
  send;
  final Clock _now;
  DateTime? _lastOffer;

  // Commands are idempotent by request_id: a repeat gets the stored result.
  final _results = <String, Future<Map<String, Object?>>>{};

  void _commandResult(String id, Map<String, Object?> r) =>
      send('command.result', {'request_id': id, ...r}, requestId: id);

  Future<Map<String, Object?>> _runCommand(String type, Map payload) async {
    Map<String, Object?> res(String? err, [String? msg]) => {
      'ok': err == null,
      'error': err,
      'message': ?msg,
    };
    if (type.startsWith('voice.')) {
      final v = voice;
      if (v == null) return res('BAD_REQUEST', 'Voice is not available.');
      if (type == 'voice.settings.set') {
        final phrase = payload['wake_phrase'];
        if (phrase is! String)
          return res('INVALID_WAKE_PHRASE', 'wake_phrase is required.');
        return res(await v.setPhrase(phrase));
      }
      return res(await v.setMuted(type == 'voice.mute'));
    }
    final rec = recording;
    if (rec == null) return res('BAD_REQUEST', 'Recording is not available.');
    if (type == 'recording.start') {
      // Best effort: any requested profile is ignored; the actual one is reported in recording.profile.
      return res(await rec.startRecording());
    }
    return res(await rec.stopRecording());
  }

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
      case 'recording.start' ||
          'recording.stop' ||
          'voice.settings.set' ||
          'voice.mute' ||
          'voice.unmute':
        final type = j['type'] as String;
        final r = await (_results[id] ??= _runCommand(type, payload).catchError(
          (Object _) => <String, Object?>{
            'ok': false,
            'error': 'RECORDING_FAILED',
          },
        ));
        if (_results.length > 100) _results.remove(_results.keys.first);
        _commandResult(id, r);
      case 'webrtc.stop':
        await port.stopStream('browser_stop');
      default:
        _error(id, 'BAD_REQUEST', 'Unknown message type.');
    }
  }
}
