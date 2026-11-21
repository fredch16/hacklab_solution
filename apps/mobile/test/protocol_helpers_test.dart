import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/connectivity/pairing.dart';
import 'package:local_vision/features/connectivity/private_address.dart';
import 'package:local_vision/features/connectivity/snapshot.dart';
import 'package:local_vision/platform/device_status.dart';

void main() {
  lanAddressTests();
  test('private address filter', () {
    bool ok(String a) => isAllowedPeer(InternetAddress(a));
    for (final a in [
      '127.0.0.1',
      '10.0.0.1',
      '10.255.255.255',
      '172.16.0.1',
      '172.31.255.255',
      '192.168.1.5',
      '169.254.1.1',
      '100.64.0.1',
      '100.127.255.255',
      '::1',
      'fe80::1',
    ]) {
      expect(ok(a), isTrue, reason: a);
    }
    for (final a in [
      '8.8.8.8',
      '172.15.0.1',
      '172.32.0.1',
      '192.169.0.1',
      '100.63.255.255',
      '100.128.0.1',
      '11.0.0.1',
      '2001:db8::1',
    ]) {
      expect(ok(a), isFalse, reason: a);
    }
  });

  test('origin check', () {
    expect(originAllowed('https://10.0.0.5:8443', '10.0.0.5:8443'), isTrue);
    expect(originAllowed('http://10.0.0.5:8443', '10.0.0.5:8443'), isFalse);
    expect(originAllowed('https://evil.example', '10.0.0.5:8443'), isFalse);
    expect(originAllowed('https://10.0.0.5:8443', '10.0.0.6:8443'), isFalse);
    expect(originAllowed(null, '10.0.0.5:8443'), isFalse);
    expect(originAllowed('https://10.0.0.5:8443', null), isFalse);
  });

  test('snapshot payload has every key and nulls for unknown', () {
    final p = buildSnapshotPayload(
      app: 'ready',
      sessionStartedUtc: null,
      keepScreenOn: false,
      device: const DeviceStatus.unknown(),
      appVersion: null,
      permissions: const {},
      addresses: const [],
      port: 8443,
      certSha256: null,
      viewer: const ViewerInfo(paired: false, connected: false),
      camera: const {'state': 'not_started', 'error': null, 'lens': 'back'},
      stream: const {'state': 'idle', 'error': null},
    );
    final j = jsonDecode(jsonEncode(p)) as Map<String, dynamic>;
    expect(j.keys.toSet(), {
      'app',
      'session_started_utc',
      'keep_screen_on',
      'device',
      'permissions',
      'camera',
      'stream',
      'recording',
      'voice',
      'battery',
      'thermal',
      'storage',
      'network',
      'viewer',
    });
    expect((j['device'] as Map).keys.toSet(), {
      'model',
      'manufacturer',
      'os',
      'sdk_int',
      'app_version',
    });
    expect((j['device'] as Map).values.every((v) => v == null), isTrue);
    expect(j['battery'], {
      'level_percent': null,
      'charging': null,
      'plugged': null,
    });
    expect(j['thermal'], {'status': null});
    expect(j['storage'], {'free_bytes': null, 'total_bytes': null});
    expect(j['network'], {
      'addresses': [],
      'port': 8443,
      'tls': 'self_signed',
      'cert_sha256': null,
    });
    expect(j['viewer'], {
      'paired': false,
      'connected': false,
      'paired_since_utc': null,
      'expires_utc': null,
    });
    expect(j['camera'], {
      'state': 'not_started',
      'error': null,
      'lens': 'back',
    });
    expect(j['stream'], {'state': 'idle', 'error': null});
    expect(j['permissions'], {
      'camera': 'not_requested',
      'microphone': 'not_requested',
    });
  });
}

void lanAddressTests() {
  InternetAddress ip(String a) => InternetAddress(a);
  test('cellular interfaces are excluded from advertised addresses', () {
    final got = selectLanAddresses({
      'wlan0': [ip('10.19.20.246')],
      'ap_br_wlan2': [ip('10.181.249.119')],
      'rmnet16': [ip('100.94.73.169')],
      'v4-rmnet_data2': [ip('192.0.0.4')],
      'ccmni1': [ip('10.5.5.5')],
      'lo': [ip('127.0.0.1')],
      'swlan0': [ip('192.168.43.1')],
      'dummy0': [ip('8.8.8.8')],
    });
    expect(got, ['10.181.249.119', '10.19.20.246', '192.168.43.1']);
  });

  test('non-cellular CGNAT (e.g. a CGNAT-numbered Wi-Fi) is allowed', () {
    expect(
      selectLanAddresses({
        'wlan0': [ip('100.100.1.2')],
      }),
      ['100.100.1.2'],
    );
    expect(isCellularInterface('rmnet16'), isTrue);
    expect(isCellularInterface('wlan0'), isFalse);
  });
}
