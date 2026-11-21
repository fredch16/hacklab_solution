import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'certificate.dart';
import 'pairing.dart';
import 'private_address.dart';

typedef AssetLoader = Future<Uint8List?> Function(String name);
typedef SnapshotBuilder = Map<String, Object?> Function();

const defaultPort = 8443;
const _cookieName = 'lvc_session';
const _pendingName = 'lvc_pending';
const _csp =
    "default-src 'self'; connect-src 'self' wss:; img-src 'self' data:; "
    "style-src 'self'; script-src 'self'; frame-ancestors 'none'";
const _mime = {
  'html': 'text/html; charset=utf-8',
  'css': 'text/css; charset=utf-8',
  'js': 'text/javascript; charset=utf-8',
  'mjs': 'text/javascript; charset=utf-8',
  'json': 'application/json; charset=utf-8',
  'map': 'application/json; charset=utf-8',
  'svg': 'image/svg+xml',
  'png': 'image/png',
  'ico': 'image/x-icon',
  'woff2': 'font/woff2',
  'txt': 'text/plain; charset=utf-8',
  'webmanifest': 'application/manifest+json',
};

enum ServerEventType { started, stopped, stateChanged }

class ServerEvent {
  const ServerEvent(this.type);
  final ServerEventType type;
}

class LocalServer {
  LocalServer({
    required this.pairing,
    required this.certs,
    required this.addresses,
    required this.assetLoader,
    required this.snapshotPayload,
    this.port = defaultPort,
    InternetAddress? bindAddress,
    Clock? clock,
    Random? random,
  }) : bindAddress = bindAddress ?? InternetAddress.anyIPv4,
       _now = clock ?? DateTime.now,
       _random = random ?? Random.secure() {
    _pairingSub = pairing.events.listen(_onPairingEvent);
  }

  final PairingService pairing;
  final CertificateService certs;
  final Future<List<String>> Function() addresses;
  final AssetLoader assetLoader;
  final SnapshotBuilder snapshotPayload;
  final InternetAddress bindAddress;
  int port;
  final Clock _now;
  final Random _random;
  late final StreamSubscription<PairingEvent> _pairingSub;

  final _events = StreamController<ServerEvent>.broadcast(sync: true);
  final _sockets = <WebSocket, String>{}; // socket -> viewer token
  HttpServer? _http;
  Timer? _expiryTimer;
  String? _sessionId;
  int _sequence = 0;
  String? _lastBroadcast;
  ServerCert? _cert;

  Stream<ServerEvent> get events => _events.stream;
  bool get running => _http != null;
  String? get certSha256 => _cert?.sha256;
  List<String> get certAddresses => _cert?.addresses ?? const [];
  ViewerInfo get viewerInfo =>
      pairing.viewerInfo(connected: _sockets.isNotEmpty);

  Future<void> start() async {
    if (_http != null) return;
    final cert = await certs.ensure(await addresses());
    _http = await HttpServer.bindSecure(
      bindAddress,
      port,
      cert.context(),
      shared: false,
    );
    _cert = cert;
    port = _http!.port;
    _sessionId = base64Url
        .encode(List<int>.generate(12, (_) => _random.nextInt(256)))
        .replaceAll('=', '');
    _sequence = 0;
    _lastBroadcast = null;
    _http!.listen(_handle, onError: (Object _) {});
    _expiryTimer = Timer.periodic(
      const Duration(seconds: 1),
      (_) => pairing.checkExpiry(),
    );
    _events.add(const ServerEvent(ServerEventType.started));
  }

  Future<void> stop() async {
    final http = _http;
    if (http == null) return;
    _expiryTimer?.cancel();
    pairing.reset(); // closes sockets with 4004
    _http = null;
    await http.close(force: true);
    _events.add(const ServerEvent(ServerEventType.stopped));
  }

  Future<void> dispose() async {
    await stop();
    await _pairingSub.cancel();
    await _events.close();
  }

  /// Call whenever snapshot inputs may have changed; sends only on change.
  void notifyStateChanged() {
    if (_broadcastIfChanged())
      _events.add(const ServerEvent(ServerEventType.stateChanged));
  }

  bool _broadcastIfChanged() {
    if (_http == null) return false;
    final json = jsonEncode(snapshotPayload());
    if (json == _lastBroadcast) return false;
    _lastBroadcast = json;
    for (final s in _sockets.keys) {
      s.add(_envelope(snapshotPayload: json));
    }
    return true;
  }

  void _onPairingEvent(PairingEvent e) {
    final token = e.endedToken;
    if (token != null) {
      final r = e.endReason!;
      for (final s
          in _sockets.entries
              .where((x) => x.value == token)
              .map((x) => x.key)
              .toList()) {
        _sockets.remove(s);
        s.close(r.closeCode, r.closeReason);
      }
    }
    _broadcastIfChanged();
    _events.add(const ServerEvent(ServerEventType.stateChanged));
  }

  String _envelope({String? snapshotPayload}) {
    final payload = snapshotPayload ?? jsonEncode(this.snapshotPayload());
    return '{"version":1,"type":"session.snapshot","session_id":${jsonEncode(_sessionId)},'
        '"sequence":${++_sequence},"timestamp_utc":"${_now().toUtc().toIso8601String()}",'
        '"payload":$payload}';
  }

  // ---- request handling ----

  Future<void> _handle(HttpRequest req) async {
    try {
      if (!isAllowedPeer(req.connectionInfo!.remoteAddress)) {
        (await req.response.detachSocket(writeHeaders: false)).destroy();
        return;
      }
      final res = req.response;
      res.headers
        ..set('Cache-Control', 'no-store')
        ..set('X-Content-Type-Options', 'nosniff')
        ..set('Referrer-Policy', 'no-referrer')
        ..set('Content-Security-Policy', _csp);
      final path = req.uri.path;
      if (path.startsWith('/api/')) {
        await _api(req, path);
      } else if (req.method == 'GET' || req.method == 'HEAD') {
        await _static(req, path);
      } else {
        await _json(res, 405, {'error': 'METHOD_NOT_ALLOWED'});
      }
    } catch (_) {
      try {
        await _json(req.response, 500, {'error': 'INTERNAL'});
      } catch (_) {}
    }
  }

  Future<void> _api(HttpRequest req, String path) async {
    final res = req.response;
    final isPost = req.method == 'POST';
    bool originOk() =>
        originAllowed(req.headers.value('origin'), req.headers.value('host'));
    bool authed() => pairing.validate(_cookie(req));

    switch (path) {
      case '/api/v1/session':
        if (req.method != 'GET')
          return _json(res, 405, {'error': 'METHOD_NOT_ALLOWED'});
        return _json(res, 200, {'paired': authed()});
      case '/api/v1/pair':
        if (!isPost) return _json(res, 405, {'error': 'METHOD_NOT_ALLOWED'});
        if (!originOk()) return _originRejected(res);
        final code = await _readCode(req);
        if (code == null) return _json(res, 400, {'error': 'BAD_REQUEST'});
        final r = pairing.authorizeViewer(code);
        switch (r.status) {
          case PairStatus.ok:
            res.headers.add('Set-Cookie', _sessionCookie(r.token!));
            return _json(res, 200, {'paired': true});
          case PairStatus.locked:
            return _json(res, 429, {
              'error': 'PAIRING_LOCKED',
              'message': 'Too many attempts. Generate a new code on the phone.',
            });
          case PairStatus.invalid:
            return _json(res, 401, {
              'error': 'PAIRING_CODE_INVALID',
              'message': 'Wrong, expired or missing code.',
            });
        }
      case '/api/v1/pair/request':
        if (!isPost) return _json(res, 405, {'error': 'METHOD_NOT_ALLOWED'});
        if (!originOk()) return _originRejected(res);
        final pr = pairing.createPairRequest(
          req.connectionInfo!.remoteAddress.address,
        );
        if (pr == null) {
          return _json(res, 429, {
            'error': 'RATE_LIMITED',
            'message': 'Too many pairing requests. Wait a minute.',
          });
        }
        res.headers.add(
          'Set-Cookie',
          _pendingCookie(pr.secret, pairRequestTtl.inSeconds),
        );
        return _json(res, 200, {
          'request_id': pr.id,
          'qr': 'lvc-pair:1:${pr.id}',
          'expires_utc': pr.expiresAt.toUtc().toIso8601String(),
        });
      case '/api/v1/pair/request/status':
        if (req.method != 'GET')
          return _json(res, 405, {'error': 'METHOD_NOT_ALLOWED'});
        final st = pairing.pairRequestStatus(_cookie(req, _pendingName));
        if (st.token != null) {
          res.headers.add('Set-Cookie', _sessionCookie(st.token!));
          res.headers.add('Set-Cookie', _pendingCookie('', 0));
        }
        return _json(res, 200, {'state': st.state.name});
      case '/api/v1/unpair':
        if (!isPost) return _json(res, 405, {'error': 'METHOD_NOT_ALLOWED'});
        if (!originOk()) return _originRejected(res);
        if (!authed()) return _json(res, 401, {'error': 'PAIRING_REQUIRED'});
        res.headers.add(
          'Set-Cookie',
          '$_cookieName=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Strict',
        );
        pairing.revoke();
        return _json(res, 200, {'paired': false});
      case '/api/v1/snapshot':
        if (req.method != 'GET')
          return _json(res, 405, {'error': 'METHOD_NOT_ALLOWED'});
        if (!authed()) return _json(res, 401, {'error': 'PAIRING_REQUIRED'});
        res.statusCode = 200;
        res.headers.contentType = ContentType(
          'application',
          'json',
          charset: 'utf-8',
        );
        res.write(_envelope());
        return res.close();
      case '/api/v1/events':
        if (req.method != 'GET')
          return _json(res, 405, {'error': 'METHOD_NOT_ALLOWED'});
        if (!authed()) return _json(res, 401, {'error': 'PAIRING_REQUIRED'});
        if (!originOk()) return _originRejected(res);
        if (!WebSocketTransformer.isUpgradeRequest(req)) {
          return _json(res, 400, {'error': 'BAD_REQUEST'});
        }
        final token = _cookie(req)!;
        final ws = await WebSocketTransformer.upgrade(req);
        ws.pingInterval = const Duration(seconds: 15);
        _sockets[ws] = token;
        ws.listen(
          (_) {},
          onDone: () {
            if (_sockets.remove(ws) != null) notifyStateChanged();
          },
          onError: (Object _) {},
        );
        _lastBroadcast = null; // viewer.connected changed: every socket, incl. the new one, gets a snapshot
        notifyStateChanged();
        return;
      default:
        return _json(res, 404, {'error': 'NOT_FOUND'});
    }
  }

  Future<void> _static(HttpRequest req, String path) async {
    final res = req.response;
    final name = path == '/' ? 'index.html' : path.substring(1);
    if (!RegExp(r'^[A-Za-z0-9._-]+$').hasMatch(name) || name.startsWith('.')) {
      return _json(res, 404, {'error': 'NOT_FOUND'});
    }
    final bytes = await assetLoader(name);
    if (bytes == null) return _json(res, 404, {'error': 'NOT_FOUND'});
    final ext = name.contains('.') ? name.split('.').last.toLowerCase() : '';
    res.statusCode = 200;
    res.headers.set('Content-Type', _mime[ext] ?? 'application/octet-stream');
    res.headers.contentLength = bytes.length;
    if (req.method != 'HEAD') res.add(bytes);
    await res.close();
  }

  String? _cookie(HttpRequest req, [String name = _cookieName]) {
    for (final c in req.cookies) {
      if (c.name == name) return c.value;
    }
    return null;
  }

  String _sessionCookie(String token) =>
      '$_cookieName=$token; Max-Age=${sessionTokenTtl.inSeconds}; Path=/; HttpOnly; Secure; SameSite=Strict';

  String _pendingCookie(String value, int maxAge) =>
      '$_pendingName=$value; Max-Age=$maxAge; Path=/api/v1/pair; HttpOnly; Secure; SameSite=Strict';

  Future<String?> _readCode(HttpRequest req) async {
    final chunks = <int>[];
    await for (final c in req) {
      chunks.addAll(c);
      if (chunks.length > 4096) return null;
    }
    try {
      final j = jsonDecode(utf8.decode(chunks));
      final code = j is Map ? j['code'] : null;
      return code is String && RegExp(r'^\d{6}$').hasMatch(code) ? code : null;
    } catch (_) {
      return null;
    }
  }

  Future<void> _originRejected(HttpResponse res) =>
      _json(res, 403, {'error': 'ORIGIN_REJECTED'});

  Future<void> _json(HttpResponse res, int status, Map<String, Object?> body) {
    res.statusCode = status;
    res.headers.contentType = ContentType(
      'application',
      'json',
      charset: 'utf-8',
    );
    res.write(jsonEncode(body));
    return res.close();
  }
}
