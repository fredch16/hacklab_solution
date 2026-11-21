import 'dart:async';
import 'dart:io';

import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/app/session_controller.dart';
import 'package:local_vision/features/connectivity/certificate.dart';
import 'package:local_vision/features/connectivity/local_server.dart';
import 'package:local_vision/features/connectivity/pairing.dart';
import 'package:local_vision/main.dart';
import 'package:local_vision/platform/device_status.dart';
import 'package:permission_handler/permission_handler.dart';

class _NoDevice implements DeviceStatusSource {
  final events = StreamController<BatteryStatus>.broadcast();
  @override
  Stream<BatteryStatus> get batteryEvents => events.stream;
  @override
  Future<DeviceStatus> read() async => const DeviceStatus.unknown();
}

class _Perms implements PermissionPort {
  @override
  Future<String> status(Permission p) async => 'not_requested';
  @override
  Future<String> request(Permission p) async => 'granted';
  @override
  Future<void> openSettings() async {}
}

void main() {
  late Directory dir;
  late SessionController c;
  late _NoDevice device;
  final wake = <bool>[];

  setUp(() async {
    dir = await Directory.systemTemp.createTemp('lvc_ctl');
    wake.clear();
    final pairing = PairingService();
    c = SessionController(
      pairing: pairing,
      deviceSource: device = _NoDevice(),
      permissionPort: _Perms(),
      addresses: () async => ['127.0.0.1'],
      wakelock: (b) async => wake.add(b),
      appVersion: () async => '0.1.0+1',
      serverFactory: (payload) => LocalServer(
        pairing: pairing,
        certs: CertificateService(dir),
        addresses: () async => ['127.0.0.1'],
        bindAddress: InternetAddress.loopbackIPv4,
        port: 0,
        assetLoader: (n) async => null,
        snapshotPayload: payload,
      ),
    );
  });

  tearDown(() async {
    c.dispose();
    await dir.delete(recursive: true);
  });

  testWidgets('session lifecycle state machine', (tester) async {
    await tester.runAsync(() async {
      await c.init();
      expect(c.phase, AppPhase.ready);
      await c.startSession();
      expect(c.phase, AppPhase.active);
      expect(c.server.running, isTrue);
      expect(wake, [true]);
      expect(c.snapshotPayload()['app'], 'active');
      expect(c.snapshotPayload()['keep_screen_on'], true);

      c.didChangeAppLifecycleState(AppLifecycleState.paused);
      expect(c.phase, AppPhase.interrupted);
      expect(c.snapshotPayload()['app'], 'interrupted');
      c.didChangeAppLifecycleState(AppLifecycleState.resumed);
      await Future<void>.delayed(const Duration(milliseconds: 50));
      expect(c.phase, AppPhase.active);

      c.pairing.createOneTimeCode();
      c.pairing.authorizeViewer(c.pairing.codeState.code!);
      expect(c.server.viewerInfo.paired, isTrue);
      await c.endSession();
      expect(c.phase, AppPhase.ready);
      expect(c.server.running, isFalse);
      expect(c.server.viewerInfo.paired, isFalse);
      expect(wake, [true, false]);

      // paused while not in a session does nothing
      c.didChangeAppLifecycleState(AppLifecycleState.paused);
      expect(c.phase, AppPhase.ready);
    });
  });

  testWidgets('battery push updates the snapshot immediately', (tester) async {
    await tester.runAsync(() async {
      await c.init();
      expect(((c.snapshotPayload()['battery']) as Map)['plugged'], isNull);
      device.events.add(const BatteryStatus(80, true, true));
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(c.snapshotPayload()['battery'], {
        'level_percent': 80,
        'charging': true,
        'plugged': true,
      });
      device.events.add(const BatteryStatus(80, false, true));
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(batteryLabel(c.device), 'Plugged in, not charging');
      device.events.add(const BatteryStatus(79, false, false));
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(batteryLabel(c.device), 'On battery');
    });
  });

  testWidgets('capture tab is honest: not started, unavailable values', (
    tester,
  ) async {
    await tester.runAsync(() => c.init());
    await tester.pumpWidget(LocalVisionApp(controller: c));
    expect(find.text('Camera · Not started'), findsOneWidget);
    expect(find.text('WebRTC · Not started'), findsOneWidget);
    expect(find.text('Local master · Not started'), findsOneWidget);
    expect(find.text('Unavailable'), findsWidgets);
    expect(find.text('Start session'), findsOneWidget);
  });
}
