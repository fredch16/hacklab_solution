import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/connectivity/signaling.dart';
import 'package:local_vision/platform/camera_platform.dart';

class _Port implements WebrtcPort {
  final offers = <String>[];
  final stops = <String>[];
  WebrtcFailure? failure;
  @override
  Future<String> offer(String sdp) async {
    offers.add(sdp);
    if (failure != null) throw failure!;
    return 'answer:${offers.length}';
  }

  @override
  Future<void> stopStream(String reason) async => stops.add(reason);
}

String offer(String id, Object? sdp) => jsonEncode({
  'version': 1,
  'type': 'webrtc.offer',
  'request_id': id,
  'payload': {'sdp': sdp},
});

void main() {
  late _Port port;
  late SignalingRelay relay;
  late DateTime now;
  final out = <(String, Map<String, Object?>, String?)>[];

  setUp(() {
    port = _Port();
    now = DateTime.utc(2026, 11, 21);
    out.clear();
    relay = SignalingRelay(
      port: port,
      clock: () => now,
      send: (t, p, {requestId}) => out.add((t, p, requestId)),
    );
  });

  test('valid offer relays and answers with the same request_id', () async {
    await relay.handle(offer('r1', 'v=0'));
    expect(port.offers, ['v=0']);
    expect(out.single.$1, 'webrtc.answer');
    expect(out.single.$2, {'request_id': 'r1', 'sdp': 'answer:1'});
    expect(out.single.$3, 'r1');
  });

  test(
    'rate limit: second offer within 1 s is BAD_REQUEST, after 1 s replaces',
    () async {
      await relay.handle(offer('r1', 'a'));
      now = now.add(const Duration(milliseconds: 500));
      await relay.handle(offer('r2', 'b'));
      expect(port.offers, ['a']);
      expect(out.last.$1, 'webrtc.error');
      expect(out.last.$2['code'], 'BAD_REQUEST');
      now = now.add(const Duration(milliseconds: 600));
      await relay.handle(offer('r3', 'c'));
      expect(port.offers, [
        'a',
        'c',
      ]); // replace semantics live in the native peer
      expect(out.last.$1, 'webrtc.answer');
    },
  );

  test('SDP size limit 64 KiB', () async {
    await relay.handle(offer('r1', 'x' * (64 * 1024 + 1)));
    expect(port.offers, isEmpty);
    expect(out.single.$2['code'], 'BAD_REQUEST');
    out.clear();
    await relay.handle(offer('r2', 'x' * (64 * 1024)));
    expect(port.offers, hasLength(1));
  });

  test('malformed input is rejected without reaching the port', () async {
    for (final raw in [
      'not json',
      '[]',
      jsonEncode({
        'version': 2,
        'type': 'webrtc.offer',
        'request_id': 'a',
        'payload': {},
      }),
      jsonEncode({
        'version': 1,
        'type': 'webrtc.offer',
        'payload': {'sdp': 'x'},
      }),
      jsonEncode({
        'version': 1,
        'type': 'webrtc.offer',
        'request_id': 'a',
        'payload': {},
      }),
      jsonEncode({
        'version': 1,
        'type': 'webrtc.offer',
        'request_id': 'a',
        'payload': {'sdp': 5},
      }),
      jsonEncode({
        'version': 1,
        'type': 'webrtc.nope',
        'request_id': 'a',
        'payload': {},
      }),
    ]) {
      await relay.handle(raw);
    }
    expect(port.offers, isEmpty);
    expect(
      out.every((m) => m.$1 == 'webrtc.error' && m.$2['code'] == 'BAD_REQUEST'),
      isTrue,
    );
    expect(out, hasLength(7));
  });

  test('port failure becomes webrtc.error with its code', () async {
    port.failure = const WebrtcFailure('CAMERA_UNAVAILABLE', 'no camera');
    await relay.handle(offer('r1', 'a'));
    expect(out.single.$1, 'webrtc.error');
    expect(out.single.$2['code'], 'CAMERA_UNAVAILABLE');
    expect(out.single.$2['request_id'], 'r1');
  });

  test('webrtc.stop stops the stream', () async {
    await relay.handle(
      jsonEncode({
        'version': 1,
        'type': 'webrtc.stop',
        'request_id': 's',
        'payload': {},
      }),
    );
    expect(port.stops, ['browser_stop']);
    expect(out, isEmpty);
  });
}
