import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/connectivity/signaling.dart';
import 'package:local_vision/features/recording/recording_controller.dart';
import 'package:local_vision/features/voice/voice_controller.dart';

import 'fakes.dart';

class _Port implements WebrtcPort {
  @override
  Future<String> offer(String sdp) async => 'a';
  @override
  Future<void> stopStream(String reason) async {}
}

String cmd(String type, String id, [Map<String, Object?> payload = const {}]) =>
    jsonEncode({
      'version': 1,
      'type': type,
      'request_id': id,
      'payload': payload,
    });

void main() {
  test('wake phrase validation', () {
    expect(validateWakePhrase('  hey   robot '), 'hey robot');
    expect(validateWakePhrase("don't stop"), isNull);
    expect(validateWakePhrase(' Hey   ELi '), 'hey ELi');
    expect(validateWakePhrase('hey Elrobot'), 'hey Elrobot');
    expect(validateWakePhrase('hey'), isNull);
    expect(validateWakePhrase('a'), isNull);
    expect(validateWakePhrase('hey robot 2'), isNull);
    expect(validateWakePhrase('x' * 41), isNull);
    expect(validateWakePhrase(null), isNull);
  });

  group('voice controller + relay', () {
    late FakeCameraPlatform cam;
    late RecordingController rec;
    late VoiceController voice;
    late SignalingRelay relay;
    final out = <(String, Map<String, Object?>)>[];

    setUp(() {
      cam = FakeCameraPlatform();
      rec = RecordingController(cam);
      voice = VoiceController(cam);
      out.clear();
      relay = SignalingRelay(
        port: _Port(),
        recording: rec,
        voice: voice,
        send: (t, p, {requestId}) => out.add((t, p)),
      );
      voice.sender = (t, p, {requestId}) => out.add((t, p));
    });

    Map<String, Object?> finalEvent(String id, String text) => {
      'event': 'voice_event',
      'type': 'voice.transcript.final',
      'payload': <String, Object?>{
        'utterance_id': id,
        'text': text,
        'started_utc': 'a',
        'ended_utc': 'b',
        'end_reason': 'silence',
        'asr_latency_ms': 12,
      },
    };

    test(
      'voice.settings.set validates then reaches native; mute/unmute reply ok',
      () async {
        await relay.handle(
          cmd('voice.settings.set', 'v1', {'wake_phrase': 'hey robot'}),
        );
        expect(cam.voiceCalls, ['phrase:hey robot']);
        expect(out.last.$2, {'request_id': 'v1', 'ok': true, 'error': null});
        await relay.handle(
          cmd('voice.settings.set', 'v2', {'wake_phrase': '12'}),
        );
        expect(out.last.$2['error'], 'INVALID_WAKE_PHRASE');
        expect(cam.voiceCalls, ['phrase:hey robot']);
        await relay.handle(cmd('voice.mute', 'v3'));
        await relay.handle(cmd('voice.unmute', 'v4'));
        expect(cam.voiceCalls.skip(1), ['mute:true', 'mute:false']);
      },
    );

    test('phone forwards the transcription without acting on it', () async {
      final event = finalEvent('utt-1', 'Please start recording now');
      (event['payload'] as Map<String, Object?>)['acoustic_text'] =
          'start recording';
      cam.emit(event);
      await pumpEventQueue();

      expect(cam.recordCalls, isEmpty);
      expect(out.single.$1, 'voice.transcript.final');
      expect(out.single.$2, event['payload']);

      await relay.handle(cmd('recording.start', 'voice:utt-1'));
      await relay.handle(cmd('recording.start', 'voice:utt-1'));
      expect(cam.recordCalls, ['start']);
      expect(out.last.$1, 'command.result');
    });

    test('listening follows start/stop and end clears', () async {
      await voice.start();
      await voice.start(); // idempotent
      await voice.stop();
      await voice.end();
      expect(cam.voiceCalls, ['start', 'stop', 'stop+clear']);
    });

    test('state defaults to unavailable with all keys', () {
      expect(normalizeVoice(null).keys, [
        'state',
        'error',
        'wake_phrase',
        'language',
        'model',
        'transcripts',
      ]);
      expect(voice.state, 'unavailable');
    });
  });
}
