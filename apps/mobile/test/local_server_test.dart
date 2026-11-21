import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/connectivity/certificate.dart';
import 'package:local_vision/features/connectivity/local_server.dart';
import 'package:local_vision/features/connectivity/pairing.dart';

class Resp {
  Resp(this.status, this.headers, this.body);
  final int status;
  final HttpHeaders headers;
  final String body;
  Map<String, dynamic> get json => jsonDecode(body) as Map<String, dynamic>;
}

class Client {
  Client(this.port) : http = HttpClient() {
    http.badCertificateCallback = (cert, host, port) => true;
  }
  final int port;
  final HttpClient http;
  String? cookie;
  String get origin => 'https://127.0.0.1:$port';

  Future<Resp> send(
    String method,
    String path, {
    Object? body,
    String? originHeader,
    bool withOrigin = true,
  }) async {
    final r = await http.openUrl(method, Uri.parse('$origin$path'));
    if (withOrigin) r.headers.set('origin', originHeader ?? origin);
    if (cookie != null) r.headers.set('cookie', 'lvc_session=$cookie');
    if (body != null) {
      r.headers.contentType = ContentType.json;
      r.write(body is String ? body : jsonEncode(body));
    }
    final res = await r.close();
    final text = await utf8.decodeStream(res);
    for (final c in res.cookies) {
      if (c.name == 'lvc_session') cookie = c.value.isEmpty ? null : c.value;
    }
    return Resp(res.statusCode, res.headers, text);
  }

  Future<Resp> pair(String code, {String? originHeader}) => send(
    'POST',
    '/api/v1/pair',
    body: {'code': code},
    originHeader: originHeader,
  );

  Future<WebSocket> ws({String? originHeader}) => WebSocket.connect(
    'wss://127.0.0.1:$port/api/v1/events',
    headers: {
      'origin': originHeader ?? origin,
      if (cookie != null) 'cookie': 'lvc_session=$cookie',
    },
    customClient: http,
  );
}

/// Collects decoded envelopes and close info from a WebSocket.
class WsLog {
  WsLog(this.ws) {
    ws.listen(
      (m) => messages.add(jsonDecode(m as String) as Map<String, dynamic>),
      onDone: () => closed.complete(),
    );
  }
  final WebSocket ws;
  final messages = <Map<String, dynamic>>[];
  final closed = Completer<void>();

  Future<void> waitFor(int n) async {
    for (var i = 0; i < 200 && messages.length < n; i++) {
      await Future<void>.delayed(const Duration(milliseconds: 25));
    }
    expect(messages.length, greaterThanOrEqualTo(n));
  }
}

void main() {
  late Directory dir;
  late PairingService pairing;
  late LocalServer server;
  late Client a;
  var counter = 0;

  setUp(() async {
    dir = await Directory.systemTemp.createTemp('lvc_test');
    counter = 0;
    pairing = PairingService();
    server = LocalServer(
      pairing: pairing,
      certs: CertificateService(dir),
      addresses: () async => ['127.0.0.1'],
      bindAddress: InternetAddress.loopbackIPv4,
      port: 0,
      assetLoader: (name) async => name == 'index.html'
          ? Uint8List.fromList(utf8.encode('<h1>hi</h1>'))
          : null,
      snapshotPayload: () => {
        'app': 'active',
        'counter': counter,
        'viewer': {'paired': pairing.viewerInfo(connected: false).paired},
      },
    );
    await server.start();
    a = Client(server.port);
  });

  tearDown(() async {
    await server.dispose();
    await dir.delete(recursive: true);
  });

  Future<void> pairA(Client c) async {
    final code = pairing.createOneTimeCode().code!;
    expect((await c.pair(code)).status, 200);
  }

  test('static index with security headers; unknown path 404', () async {
    final r = await a.send('GET', '/', withOrigin: false);
    expect(r.status, 200);
    expect(r.body, '<h1>hi</h1>');
    expect(r.headers.contentType!.mimeType, 'text/html');
    expect(
      r.headers.value('content-security-policy'),
      "default-src 'self'; connect-src 'self' wss:; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
    );
    expect(r.headers.value('cache-control'), 'no-store');
    expect(r.headers.value('x-content-type-options'), 'nosniff');
    expect(r.headers.value('referrer-policy'), 'no-referrer');
    expect((await a.send('GET', '/missing.js')).status, 404);
    expect((await a.send('GET', '/../etc/passwd')).status, 404);
    expect((await a.send('GET', '/api/v1/nope')).json['error'], 'NOT_FOUND');
  });

  test('certificate carries IP SAN and fingerprint format', () {
    expect(
      server.certSha256,
      matches(RegExp(r'^([0-9A-F]{2}:){31}[0-9A-F]{2}$')),
    );
  });

  test('unauthenticated: session false, snapshot 401, ws 401', () async {
    expect((await a.send('GET', '/api/v1/session')).json, {'paired': false});
    final s = await a.send('GET', '/api/v1/snapshot');
    expect(s.status, 401);
    expect(s.json['error'], 'PAIRING_REQUIRED');
    await expectLater(a.ws(), throwsA(isA<WebSocketException>()));
  });

  test('wrong code 401, bad body 400, then lockout 429', () async {
    final code = pairing.createOneTimeCode().code!;
    final wrong = code == '000000' ? '000001' : '000000';
    expect(
      (await a.send('POST', '/api/v1/pair', body: 'nope')).json['error'],
      'BAD_REQUEST',
    );
    for (var i = 0; i < 5; i++) {
      final r = await a.pair(wrong);
      expect(r.status, 401);
      expect(r.json['error'], 'PAIRING_CODE_INVALID');
    }
    final locked = await a.pair(code);
    expect(locked.status, 429);
    expect(locked.json['error'], 'PAIRING_LOCKED');
  });

  test('bad Origin 403 on pair and ws', () async {
    final code = pairing.createOneTimeCode().code!;
    final r = await a.pair(code, originHeader: 'https://evil.example');
    expect(r.status, 403);
    expect(r.json['error'], 'ORIGIN_REJECTED');
    expect(
      (await a.send(
        'POST',
        '/api/v1/pair',
        body: {'code': code},
        withOrigin: false,
      )).status,
      403,
    );
    expect((await a.pair(code)).status, 200); // code was not burned
    await expectLater(
      a.ws(originHeader: 'https://evil.example'),
      throwsA(isA<WebSocketException>()),
    );
  });

  test(
    'pair sets hardened cookie, snapshot works, envelope sequence increases',
    () async {
      final code = pairing.createOneTimeCode().code!;
      final r = await a.send('POST', '/api/v1/pair', body: {'code': code});
      expect(r.status, 200);
      final setCookie = r.headers['set-cookie']!.single;
      for (final part in [
        'lvc_session=',
        'Max-Age=43200',
        'Path=/',
        'HttpOnly',
        'Secure',
        'SameSite=Strict',
      ]) {
        expect(setCookie, contains(part));
      }
      expect((await a.send('GET', '/api/v1/session')).json, {'paired': true});
      final s1 = (await a.send('GET', '/api/v1/snapshot')).json;
      final s2 = (await a.send('GET', '/api/v1/snapshot')).json;
      expect(s1['version'], 1);
      expect(s1['type'], 'session.snapshot');
      expect(s2['session_id'], s1['session_id']);
      expect(s2['sequence'], (s1['sequence'] as int) + 1);
      expect(DateTime.parse(s1['timestamp_utc'] as String).isUtc, isTrue);
      expect((s1['payload'] as Map)['app'], 'active');
    },
  );

  test(
    'ws: snapshot immediately, then on change only; unpair via http',
    () async {
      await pairA(a);
      final log = WsLog(await a.ws());
      await log.waitFor(1);
      expect(log.messages.first['type'], 'session.snapshot');
      server.notifyStateChanged(); // unchanged -> nothing new
      await Future<void>.delayed(const Duration(milliseconds: 150));
      expect(log.messages.length, 1);
      counter++;
      server.notifyStateChanged();
      await log.waitFor(2);
      expect((log.messages[1]['payload'] as Map)['counter'], 1);
      expect(
        log.messages[1]['sequence'],
        greaterThan(log.messages[0]['sequence'] as int),
      );
      expect(log.messages[1]['session_id'], log.messages[0]['session_id']);
      await log.ws.close();
    },
  );

  test('revoke closes ws with 4003', () async {
    await pairA(a);
    final log = WsLog(await a.ws());
    await log.waitFor(1);
    pairing.revoke();
    await log.closed.future.timeout(const Duration(seconds: 5));
    expect(log.ws.closeCode, 4003);
    expect(log.ws.closeReason, 'revoked');
    expect((await a.send('GET', '/api/v1/snapshot')).status, 401);
  });

  test('re-pair from second client closes first ws with 4001', () async {
    await pairA(a);
    final log = WsLog(await a.ws());
    await log.waitFor(1);
    final b = Client(server.port);
    await pairA(b);
    await log.closed.future.timeout(const Duration(seconds: 5));
    expect(log.ws.closeCode, 4001);
    expect(log.ws.closeReason, 'replaced');
    expect((await a.send('GET', '/api/v1/snapshot')).status, 401);
    expect((await b.send('GET', '/api/v1/snapshot')).status, 200);
  });

  test('unpair clears cookie; server stop closes ws with 4004', () async {
    await pairA(a);
    final log = WsLog(await a.ws());
    await log.waitFor(1);
    await server.stop();
    await log.closed.future.timeout(const Duration(seconds: 5));
    expect(log.ws.closeCode, 4004);
    expect(log.ws.closeReason, 'session_ended');
    await server.start();
    final c = Client(server.port);
    await pairA(c);
    final u = await c.send('POST', '/api/v1/unpair');
    expect(u.json, {'paired': false});
    expect(c.cookie, isNull);
  });

  test(
    'cert is reused when covered, regenerated when a new IP appears',
    () async {
      final svc = CertificateService(dir);
      final c1 = await svc.ensure(['127.0.0.1']);
      final c2 = await svc.ensure(['127.0.0.1']);
      expect(c2.sha256, c1.sha256);
      final c3 = await svc.ensure(['127.0.0.1', '10.1.2.3']);
      expect(c3.sha256, isNot(c1.sha256));
      expect((await svc.ensure(['10.1.2.3'])).sha256, c3.sha256);
    },
  );

  test('QR flow: request, approve on phone, poll issues session once, replaces viewer', () async {
    await pairA(a);
    final first = WsLog(await a.ws());
    await first.waitFor(1);

    final b = Client(server.port);
    final cr = await b.send('POST', '/api/v1/pair/request');
    expect(cr.status, 200);
    final id = cr.json['request_id'] as String;
    expect(cr.json['qr'], 'lvc-pair:1:$id');
    expect(DateTime.parse(cr.json['expires_utc'] as String).isUtc, isTrue);
    final pending = cr.headers['set-cookie']!.single;
    for (final part in [
      'lvc_pending=',
      'Max-Age=120',
      'Path=/api/v1/pair',
      'HttpOnly',
      'Secure',
      'SameSite=Strict',
    ]) {
      expect(pending, contains(part));
    }
    expect(pairing.lookupRequest(id)!.remoteIp, '127.0.0.1');

    // Poll with the pending secret (kept apart from the session cookie).
    final secret = RegExp(r'lvc_pending=([^;]+)')
        .firstMatch(pending)!
        .group(1)!;
    Future<(Resp, List<String>)> poll(String? cookie) async {
      final r = await b.http.getUrl(
        Uri.parse('${b.origin}/api/v1/pair/request/status'),
      );
      if (cookie != null) r.headers.set('cookie', 'lvc_pending=$cookie');
      final res = await r.close();
      final text = await utf8.decodeStream(res);
      return (
        Resp(res.statusCode, res.headers, text),
        res.headers['set-cookie'] ?? <String>[],
      );
    }

    expect((await poll(secret)).$1.json, {'state': 'pending'});
    expect((await poll(null)).$1.json, {'state': 'expired'});
    expect((await poll(id)).$1.json, {
      'state': 'expired',
    }); // request_id is not the secret

    expect(pairing.approveRequest(id), isTrue);
    final (ok, cookies) = await poll(secret);
    expect(ok.json, {'state': 'approved'});
    final session = cookies.firstWhere((c) => c.startsWith('lvc_session='));
    for (final part in [
      'Max-Age=43200',
      'Path=/',
      'HttpOnly',
      'Secure',
      'SameSite=Strict',
    ]) {
      expect(session, contains(part));
    }
    expect(
      cookies.any(
        (c) => c.startsWith('lvc_pending=;') && c.contains('Max-Age=0'),
      ),
      isTrue,
    );
    expect((await poll(secret)).$1.json, {'state': 'expired'}); // single use

    b.cookie = RegExp(r'lvc_session=([^;]+)').firstMatch(session)!.group(1);
    expect((await b.send('GET', '/api/v1/snapshot')).status, 200);
    await first.closed.future.timeout(const Duration(seconds: 5));
    expect(first.ws.closeCode, 4001);
  });

  test('QR flow: bad Origin 403, rate limit 429, deny', () async {
    expect(
      (await a.send(
        'POST',
        '/api/v1/pair/request',
        originHeader: 'https://evil.example',
      )).status,
      403,
    );
    final cr = await a.send('POST', '/api/v1/pair/request');
    final secret = RegExp(r'lvc_pending=([^;]+)')
        .firstMatch(cr.headers['set-cookie']!.single)!
        .group(1)!;
    pairing.denyRequest(cr.json['request_id'] as String);
    final r = await a.http.getUrl(
      Uri.parse('${a.origin}/api/v1/pair/request/status'),
    );
    r.headers.set('cookie', 'lvc_pending=$secret');
    expect(jsonDecode(await utf8.decodeStream(await r.close())), {
      'state': 'denied',
    });
    for (var i = 0; i < 9; i++) {
      expect((await a.send('POST', '/api/v1/pair/request')).status, 200);
    }
    final limited = await a.send('POST', '/api/v1/pair/request');
    expect(limited.status, 429);
    expect(limited.json['error'], 'RATE_LIMITED');
  });
}
