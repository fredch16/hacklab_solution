import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/connectivity/signaling.dart';
import 'package:local_vision/features/recording/recording_controller.dart';
import 'package:local_vision/features/voice/voice_commands.dart';
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
  group('voice command matcher', () {
    test('start phrases', () {
      for (final t in [
        'Start recording',
        'please start recording now',
        'Begin recording.',
        'record',
        'ok, record!',
        'start the recording',
        'START   RECORDING',
      ]) {
        expect(matchVoiceCommand(t), 'recording.start', reason: t);
      }
    });
    test('stop phrases', () {
      for (final t in [
        'Stop recording',
        'stop the recording',
        'end recording',
        'please stop recording now',
        'End the recording.',
      ]) {
        expect(matchVoiceCommand(t), 'recording.stop', reason: t);
      }
    });
    test('everything else does nothing', () {
      for (final t in [
        '',
        'hello world',
        'recorder',
        'recording',
        'prerecord',
        'stop',
        'start the video',
        'stout recording',
        'it was a stout record',
        "don't stop recording",
        'do not start recording',
        'I said start recording yesterday',
        'start recording and stop recording',
        'what did you record',
        'they stopped recording',
      ]) {
        expect(matchVoiceCommand(t), isNull, reason: t);
      }
    });
  });

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
      voice.commandRunner = relay.execute;
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

    test(
      'final "start recording" runs recording.start and reports voice.command',
      () async {
        cam.emit(finalEvent('utt-1', 'Please start recording now'));
        await pumpEventQueue();
        expect(cam.recordCalls, ['start']);
        expect(
          out.map((m) => m.$1),
          containsAll(['voice.transcript.final', 'voice.command']),
        );
        expect(out.firstWhere((m) => m.$1 == 'voice.command').$2, {
          'utterance_id': 'utt-1',
          'command': 'recording.start',
          'ok': true,
          'error': null,
        });
      },
    );

    test(
      'stop and failures are reported, unrelated speech does nothing',
      () async {
        cam.emit(finalEvent('utt-2', 'stop the recording'));
        await pumpEventQueue();
        expect(cam.recordCalls, ['stop:user']);
        cam.recordStartError = 'LOW_STORAGE';
        cam.emit(finalEvent('utt-3', 'record'));
        await pumpEventQueue();
        final c3 = out.lastWhere((m) => m.$1 == 'voice.command').$2;
        expect(c3, {
          'utterance_id': 'utt-3',
          'command': 'recording.start',
          'ok': false,
          'error': 'LOW_STORAGE',
        });
        cam.emit(finalEvent('utt-4', 'what time is it'));
        await pumpEventQueue();
        expect(
          out.lastWhere((m) => m.$1 == 'voice.command').$2['command'],
          'none',
        );
        expect(cam.recordCalls, ['stop:user', 'start']);
      },
    );

    test(
      'a failed acoustic check blocks a command and stays on the phone',
      () async {
        final event = finalEvent('utt-conflict', 'stop recording');
        (event['payload'] as Map<String, Object?>)['verified_command'] = null;
        cam.emit(event);
        await pumpEventQueue();
        expect(cam.recordCalls, isEmpty);
        expect(
          out.lastWhere((m) => m.$1 == 'voice.command').$2['command'],
          'none',
        );
        expect(
          out.firstWhere((m) => m.$1 == 'voice.transcript.final').$2,
          isNot(contains('verified_command')),
        );

        final agreed = finalEvent('utt-agree', 'start recording');
        (agreed['payload'] as Map<String, Object?>)['verified_command'] =
            'recording.start';
        cam.emit(agreed);
        await pumpEventQueue();
        expect(cam.recordCalls, ['start']);
      },
    );

    test(
      'a repeated utterance id does not execute twice (idempotent)',
      () async {
        cam.emit(finalEvent('utt-9', 'start recording'));
        cam.emit(finalEvent('utt-9', 'start recording'));
        await pumpEventQueue();
        expect(cam.recordCalls, ['start']);
      },
    );

    test('snapshot transcripts carry the matched command', () async {
      cam.emit({
        'event': 'voice',
        'voice': {
          'state': 'listening',
          'transcripts': [
            {
              'id': 'utt-1',
              'text': 'start recording',
              'started_utc': 'a',
              'ended_utc': 'b',
              'end_reason': 'silence',
            },
            {
              'id': 'utt-0',
              'text': 'hello',
              'started_utc': 'a',
              'ended_utc': 'b',
              'end_reason': 'silence',
            },
          ],
        },
      });
      cam.emit(finalEvent('utt-1', 'start recording'));
      cam.emit(finalEvent('utt-0', 'hello'));
      await pumpEventQueue();
      final t = voice.voice['transcripts'] as List;
      expect((t[0] as Map)['command'], 'recording.start');
      expect((t[1] as Map)['command'], isNull);
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
