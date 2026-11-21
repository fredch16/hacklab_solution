import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../platform/camera_platform.dart';
import '../connectivity/signaling.dart';
import '../recording/recording_controller.dart' show MessageSenderFn;
import 'voice_commands.dart';

final _phraseChars = RegExp(r"^[A-Za-z' ]+$");

/// Wake phrase rule shared with the phone engine: 2-40 chars, letters, spaces, apostrophes.
/// Returns the normalised phrase, or null when invalid.
String? validateWakePhrase(String? raw) {
  final p = raw?.trim().replaceAll(RegExp(r'\s+'), ' ');
  if (p == null || p.length < 2 || p.length > 40 || !_phraseChars.hasMatch(p))
    return null;
  return p;
}

const _voiceKeys = [
  'state',
  'error',
  'wake_phrase',
  'language',
  'model',
  'transcripts',
];

/// Contract `voice` snapshot object: every key present.
Map<String, Object?> normalizeVoice(Map<String, Object?>? m) => {
  for (final k in _voiceKeys) k: m?[k],
  'state': m?['state'] ?? 'unavailable',
  'language': m?['language'] ?? 'en',
  'transcripts': m?['transcripts'] ?? const <Object?>[],
};

/// Mirrors the native voice engine and relays its events to the browser.
class VoiceController extends ChangeNotifier implements VoicePort {
  VoiceController(this.platform) {
    _sub = platform.events.listen(_onEvent, onError: (Object _) {});
  }

  final CameraPlatform platform;
  late final StreamSubscription<Map<String, Object?>> _sub;
  MessageSenderFn? sender;

  var _voice = normalizeVoice(null);

  /// Runs a command via the shared idempotent path (set by the session controller).
  Future<Map<String, Object?>> Function(String requestId, String type)?
  commandRunner;
  final _commands = <String, String?>{}; // utterance id -> matched command

  /// Snapshot object with each transcript's matched `command` merged in.
  Map<String, Object?> get voice => {
    ..._voice,
    'transcripts': [
      for (final t in (_voice['transcripts'] as List))
        {...Map<String, Object?>.from(t as Map), 'command': _commands[t['id']]},
    ],
  };

  /// Live partial transcript of the utterance in progress (null when none).
  String? partial;

  String get state => _voice['state'] as String;
  String? get phrase => _voice['wake_phrase'] as String?;
  List<Map<String, Object?>> get transcripts => [
    for (final t in (voice['transcripts'] as List))
      Map<String, Object?>.from(t as Map),
  ];

  bool listening = false;

  /// Listening follows the video stream: start with the peer.
  Future<void> start() async {
    if (listening) return;
    listening = true;
    await platform.voiceStart();
  }

  Future<void> stop() async {
    if (!listening) return;
    listening = false;
    partial = null;
    await platform.voiceStop();
  }

  /// Session end: stop and forget this session's transcripts.
  Future<void> end() async {
    listening = false;
    partial = null;
    _commands.clear();
    await platform.voiceStop(clear: true);
  }

  @override
  Future<String?> setPhrase(String phrase) async {
    final p = validateWakePhrase(phrase);
    if (p == null) return 'INVALID_WAKE_PHRASE';
    return platform.voiceSetPhrase(p);
  }

  @override
  Future<String?> setMuted(bool muted) async {
    await platform.voiceMute(muted);
    return null;
  }

  void _onEvent(Map<String, Object?> e) {
    switch (e['event']) {
      case 'voice':
        _voice = normalizeVoice(
          e['voice'] == null
              ? null
              : Map<String, Object?>.from(e['voice'] as Map),
        );
        if (state != 'recognizing' && state != 'wake_detected') partial = null;
        notifyListeners();
      case 'voice_event':
        final type = e['type'] as String;
        final payload = Map<String, Object?>.from(e['payload'] as Map);
        if (type == 'voice.transcript.partial')
          partial = payload['text'] as String?;
        if (type == 'voice.transcript.final') partial = null;
        if (type == 'voice.wake_detected') partial = '';
        sender?.call(type, payload);
        // Off the event dispatch: the command emits further platform events.
        if (type == 'voice.transcript.final')
          scheduleMicrotask(() => _onFinal(payload));
        notifyListeners();
    }
  }

  Future<void> _onFinal(Map<String, Object?> p) async {
    final id = p['utterance_id'] as String;
    final cmd = matchVoiceCommand((p['text'] as String?) ?? '');
    _commands[id] = cmd;
    var ok = true;
    String? err;
    if (cmd != null) {
      final r =
          await (commandRunner?.call(id, cmd) ??
              Future.value(<String, Object?>{
                'ok': false,
                'error': 'BAD_REQUEST',
              }));
      ok = r['ok'] == true;
      err = r['error'] as String?;
    }
    sender?.call('voice.command', {
      'utterance_id': id,
      'command': cmd ?? 'none',
      'ok': ok,
      'error': err,
    });
    notifyListeners();
  }

  @override
  void dispose() {
    _sub.cancel();
    super.dispose();
  }
}
