import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/connectivity/signaling.dart';
import 'package:local_vision/features/recording/recording_controller.dart';
import 'package:local_vision/platform/camera_platform.dart';

import 'fakes.dart';

class _Port implements WebrtcPort {
  @override
  Future<String> offer(String sdp) async => 'a';
  @override
  Future<void> stopStream(String reason) async {}
}

String cmd(String type, String id) =>
    jsonEncode({'version': 1, 'type': type, 'request_id': id, 'payload': {}});

void main() {
  late FakeCameraPlatform cam;
  late RecordingController rec;
  late SignalingRelay relay;
  final out = <(String, Map<String, Object?>)>[];

  setUp(() {
    cam = FakeCameraPlatform();
    rec = RecordingController(cam);
    out.clear();
    relay = SignalingRelay(
      port: _Port(),
      recording: rec,
      send: (t, p, {requestId}) => out.add((t, p)),
    );
  });

  test('recording.start/stop reply with command.result', () async {
    await relay.handle(cmd('recording.start', 'a1'));
    expect(cam.recordCalls, ['start']);
    expect(out.last.$1, 'command.result');
    expect(out.last.$2, {'request_id': 'a1', 'ok': true, 'error': null});
    expect(rec.isRecording, isTrue);
    await relay.handle(cmd('recording.stop', 'a2'));
    expect(cam.recordCalls, ['start', 'stop:user']);
    expect(out.last.$2['ok'], true);
    expect(rec.state, 'available');
  });

  test('commands are idempotent by request_id', () async {
    await relay.handle(cmd('recording.start', 'same'));
    await relay.handle(cmd('recording.start', 'same'));
    expect(cam.recordCalls, ['start']); // not executed twice
    expect(out.where((m) => m.$1 == 'command.result'), hasLength(2));
    expect(out[0].$2, out[1].$2);
  });

  test('errors from native are returned as command.result error', () async {
    cam.recordStartError = 'PERMISSION_MIC';
    await relay.handle(cmd('recording.start', 'b1'));
    expect(out.last.$2, {
      'request_id': 'b1',
      'ok': false,
      'error': 'PERMISSION_MIC',
    });
  });

  test('recording object: all contract keys present, idle by default', () {
    final n = normalizeRecording(null);
    expect(n.keys, [
      'state',
      'id',
      'started_utc',
      'elapsed_ms',
      'bytes',
      'profile',
      'audio',
      'error',
      'warning',
    ]);
    expect(n['state'], 'idle');
    expect(
      normalizeRecording({'state': 'recording', 'bytes': 5})['elapsed_ms'],
      isNull,
    );
  });

  test(
    'native recording and recordings events update the controller',
    () async {
      cam.emit({
        'event': 'recording',
        'recording': {'state': 'finalizing'},
      });
      expect(rec.busy, isTrue);
      cam.emit({
        'event': 'recordings',
        'items': [
          {'id': 'rec-1', 'filename': 'x.mp4'},
        ],
      });
      expect(rec.recordings.single['id'], 'rec-1');
    },
  );

  test('without a recording port the command is refused', () async {
    final r = SignalingRelay(
      port: _Port(),
      send: (t, p, {requestId}) => out.add((t, p)),
    );
    await r.handle(cmd('recording.start', 'z'));
    expect(out.last.$2['ok'], false);
    expect(out.last.$2['error'], 'BAD_REQUEST');
  });

  test('platform failure code surfaces', () {
    expect(const WebrtcFailure('X').code, 'X');
  });
}
