import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/camera/capture_controller.dart';

import 'fakes.dart';

void main() {
  late FakeCameraPlatform cam;
  late CaptureController c;
  final sent = <(String, Map<String, Object?>)>[];

  setUp(() {
    cam = FakeCameraPlatform();
    c = CaptureController(cam);
    sent.clear();
    c.sender = (t, p, {requestId}) => sent.add((t, p));
  });

  test(
    'state transitions: start, preview, interrupted, scanner pause/resume, end',
    () async {
      expect(c.state, 'not_started');
      await c.start();
      expect(c.state, 'previewing');
      expect(c.textureId, 7);
      c.setInterrupted(true);
      expect(c.state, 'interrupted');
      c.setInterrupted(false);
      expect(c.state, 'previewing');
      await c.pauseForScanner();
      expect(c.state, 'paused');
      await c.resumeFromScanner();
      expect(c.state, 'previewing');
      await c.end();
      expect(c.state, 'not_started');
      expect(
        cam.calls,
        containsAllInOrder(['start', 'pause', 'resume', 'stop']),
      );
    },
  );

  test('permission denial is a camera error with PERMISSION_CAMERA', () {
    c.denyPermission();
    expect(c.cameraJson(), {
      'state': 'error',
      'error': 'PERMISSION_CAMERA',
      'lens': 'back',
    });
    expect(c.permissionDenied, isTrue);
  });

  test('native error event and stream events map to snapshot json', () async {
    await c.start();
    cam.emit({'event': 'state', 'state': 'error', 'error': 'DEVICE_IN_USE'});
    expect(c.cameraJson()['error'], 'DEVICE_IN_USE');
    cam.emit({'event': 'state', 'state': 'previewing', 'error': null});
    expect(c.cameraJson()['error'], isNull);
    cam.emit({'event': 'stream', 'state': 'connected', 'error': null});
    expect(c.state, 'transmitting');
    expect(c.streamJson(), {'state': 'connected', 'error': null});
    cam.emit({'event': 'stream', 'state': 'failed', 'error': 'ICE_FAILED'});
    expect(c.state, 'previewing');
    expect(c.streamJson(), {'state': 'failed', 'error': 'ICE_FAILED'});
  });

  test(
    'stats are relayed only while live, with nulls for unknown fields',
    () async {
      cam.emit({'event': 'stats', 'capture': null, 'stream': null});
      expect(sent, isEmpty); // not started
      await c.start();
      cam.emit({
        'event': 'stats',
        'capture': {'width': 640, 'height': 360, 'fps_measured': null},
        'stream': null,
      });
      final (type, payload) = sent.last;
      expect(type, 'camera.stats');
      expect(payload, {
        'capture': {'width': 640, 'height': 360, 'fps_measured': null},
        'stream': null,
      });
      cam.emit({
        'event': 'stats',
        'capture': null,
        'stream': {'codec': 'H264', 'fps': 29.7},
      });
      final stream = sent.last.$2['stream'] as Map;
      expect(stream.keys, [
        'codec',
        'encoder',
        'width',
        'height',
        'fps',
        'bitrate_bps',
        'quality_limitation',
        'rtt_ms',
        'local_candidate',
        'remote_candidate',
      ]);
      expect(stream['codec'], 'H264');
      expect(stream['encoder'], isNull);
      expect(
        (sent.last.$2['capture'] as Map).values.every((v) => v == null),
        isTrue,
      );
    },
  );

  test('webrtc_stopped is forwarded to the browser', () async {
    cam.emit({'event': 'webrtc_stopped', 'reason': 'ice_failed'});
    expect(sent.single.$1, 'webrtc.stopped');
    expect(sent.single.$2, {'reason': 'ice_failed'});
  });

  test('offer requires a live camera', () async {
    await expectLater(c.offer('sdp'), throwsA(isA<Exception>()));
    await c.start();
    expect(await c.offer('sdp'), 'v=0 answer');
  });

  test('end stops an active stream first', () async {
    await c.start();
    cam.emit({'event': 'stream', 'state': 'connected', 'error': null});
    await c.end();
    expect(cam.calls, containsAllInOrder(['stopStream:session_ended', 'stop']));
    expect(c.streamState, 'idle');
  });
}
