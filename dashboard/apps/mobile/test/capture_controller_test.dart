import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/camera/capture_controller.dart';
import 'package:local_vision/platform/camera_platform.dart';

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

  test('camera is off until an offer starts it; stop releases it', () async {
    expect(c.state, 'not_started');
    await c.offer('sdp');
    expect(cam.cameraRunning, isTrue);
    expect(c.state, 'previewing');
    expect(c.textureId, 7);
    expect(
      (c.previewWidth, c.previewHeight, c.previewRotation),
      (1280, 720, 90),
    );
    c.setInterrupted(true);
    expect(c.state, 'interrupted');
    c.setInterrupted(false);
    await c.stopStream('browser_stop');
    expect(c.state, 'not_started');
    expect(c.textureId, isNull);
    expect(cam.cameraRunning, isFalse);
  });

  test('scanner holds the camera and does not kill a running stream', () async {
    await c.offer('sdp');
    expect(await c.startForScanner(), isTrue);
    await c.stopStream('replaced'); // peer gone, scanner still open
    expect(c.state, 'previewing');
    await c.stopForScanner();
    expect(c.state, 'not_started');
  });

  test('scanner alone starts and stops the camera', () async {
    expect(await c.startForScanner(), isTrue);
    expect(c.state, 'previewing');
    await c.stopForScanner();
    expect(c.state, 'not_started');
    expect(cam.calls, ['start:scan', 'stop:scan']);
  });

  test('scan events reach listeners', () async {
    final got = <String>[];
    c.scans.listen(got.add);
    cam.emit({'event': 'scan', 'text': 'lvc-pair:1:x'});
    await pumpEventQueue();
    expect(got, ['lvc-pair:1:x']);
  });

  test('focus is forwarded', () async {
    await c.focus(0.25, 0.75);
    expect(cam.calls.last, 'focus:0.25,0.75');
  });

  test(
    'permission denial: error until granted, then the camera is simply off',
    () async {
      c.denyPermission();
      expect(c.cameraJson(), {
        'state': 'error',
        'error': 'PERMISSION_CAMERA',
        'lens': 'back',
      });
      await expectLater(c.offer('sdp'), throwsA(isA<WebrtcFailure>()));
      expect(cam.offers, isEmpty);
      cam.emit({'event': 'state', 'state': 'initializing', 'error': null});
      expect(c.state, 'error'); // stays until permission is granted
      c.permissionGranted();
      expect(c.state, 'not_started');
    },
  );

  test('native error and stream events map to snapshot json', () async {
    await c.offer('sdp');
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
      expect(sent, isEmpty); // camera off
      await c.startForScanner();
      cam.emit({
        'event': 'stats',
        'capture': {'width': 640, 'height': 360, 'fps_measured': null},
        'stream': null,
      });
      expect(sent.last.$1, 'camera.stats');
      expect(sent.last.$2, {
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

  test('webrtc_stopped is forwarded to the browser', () {
    cam.emit({'event': 'webrtc_stopped', 'reason': 'ice_failed'});
    expect(sent.single.$1, 'webrtc.stopped');
    expect(sent.single.$2, {'reason': 'ice_failed'});
  });

  test('end stops the stream, which releases the camera', () async {
    await c.offer('sdp');
    cam.emit({'event': 'stream', 'state': 'connected', 'error': null});
    await c.end();
    expect(
      cam.calls,
      containsAllInOrder(['stopStream:session_ended', 'stop:stream']),
    );
    expect(c.streamState, 'idle');
    expect(c.state, 'not_started');
  });
}
