import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:crypto/crypto.dart';

typedef Clock = DateTime Function();

const pairingCodeTtl = Duration(seconds: 120);
const sessionTokenTtl = Duration(hours: 12);
const maxPairingAttempts = 5;

enum ViewerEndReason {
  replaced(4001, 'replaced'),
  expired(4002, 'expired'),
  revoked(4003, 'revoked'),
  sessionEnded(4004, 'session_ended');

  const ViewerEndReason(this.closeCode, this.closeReason);
  final int closeCode;
  final String closeReason;
}

enum PairStatus { ok, invalid, locked }

class PairResult {
  const PairResult(this.status, [this.token]);
  final PairStatus status;
  final String? token;
}

enum RequestState { pending, approved, denied, expired }

const pairRequestTtl = Duration(seconds: 120);
const maxPendingRequests = 3;
const maxRequestsPerMinutePerIp = 10;

class PairRequest {
  PairRequest(
    this.id,
    this.secret,
    this.remoteIp,
    this.createdAt,
    this.challenge,
  ) : expiresAt = createdAt.add(pairRequestTtl);
  final String id;
  final String secret;
  final String remoteIp;
  final DateTime createdAt;
  final DateTime expiresAt;
  final String? challenge;
  String? proof;
  RequestState state = RequestState.pending;
}

class PairRequestStatus {
  const PairRequestStatus(this.state, [this.token, this.proof]);
  final RequestState state;

  /// Present exactly once, on the poll that consumes an approved request.
  final String? token;
  final String? proof;
}

enum CodeStatus { none, active, expired, locked }

class PairingCodeState {
  const PairingCodeState(
    this.status,
    this.code,
    this.expiresAt,
    this.remainingAttempts,
  );
  final CodeStatus status;
  final String? code;
  final DateTime? expiresAt;
  final int remainingAttempts;
}

class ViewerInfo {
  const ViewerInfo({
    required this.paired,
    required this.connected,
    this.pairedSince,
    this.expiresAt,
  });
  final bool paired;
  final bool connected;
  final DateTime? pairedSince;
  final DateTime? expiresAt;
}

class PairingEvent {
  const PairingEvent({this.endedToken, this.endReason});
  final String? endedToken;
  final ViewerEndReason? endReason;
}

bool constantTimeEquals(String a, String b) {
  var diff = a.length ^ b.length;
  final n = max(a.length, b.length);
  for (var i = 0; i < n; i++) {
    final x = i < a.length ? a.codeUnitAt(i) : 0;
    final y = i < b.length ? b.codeUnitAt(i) : 0;
    diff |= x ^ y;
  }
  return diff == 0;
}

class PairingService {
  PairingService({Clock? clock, Random? random})
    : _now = clock ?? DateTime.now,
      _random = random ?? Random.secure();

  final Clock _now;
  final Random _random;
  final _events = StreamController<PairingEvent>.broadcast(sync: true);

  String? _code;
  DateTime? _codeExpires;
  int _failed = 0;
  final _requests = <PairRequest>[]; // oldest first
  final _creations = <String, List<DateTime>>{};
  String? _token;
  DateTime? _pairedSince;
  DateTime? _tokenExpires;

  /// Fires on every change of code or viewer state.
  Stream<PairingEvent> get events => _events.stream;

  PairingCodeState get codeState {
    if (_code == null)
      return const PairingCodeState(CodeStatus.none, null, null, 0);
    final left = maxPairingAttempts - _failed;
    final status = _failed >= maxPairingAttempts
        ? CodeStatus.locked
        : _now().isBefore(_codeExpires!)
        ? CodeStatus.active
        : CodeStatus.expired;
    return PairingCodeState(status, _code, _codeExpires, left < 0 ? 0 : left);
  }

  /// Replaces any previous code (which becomes invalid).
  PairingCodeState createOneTimeCode() {
    _code = _random.nextInt(1000000).toString().padLeft(6, '0');
    _codeExpires = _now().add(pairingCodeTtl);
    _failed = 0;
    _events.add(const PairingEvent());
    return codeState;
  }

  PairResult authorizeViewer(String code) {
    final s = codeState;
    if (s.status == CodeStatus.locked)
      return const PairResult(PairStatus.locked);
    if (s.status != CodeStatus.active)
      return const PairResult(PairStatus.invalid);
    if (!constantTimeEquals(code, _code!)) {
      _failed++;
      _events.add(const PairingEvent());
      return const PairResult(PairStatus.invalid);
    }
    _code = null;
    _codeExpires = null;
    return PairResult(PairStatus.ok, _issueToken());
  }

  String _issueToken() {
    _endViewer(ViewerEndReason.replaced);
    _token = _randomB64(32);
    _pairedSince = _now();
    _tokenExpires = _pairedSince!.add(sessionTokenTtl);
    _events.add(const PairingEvent());
    return _token!;
  }

  String _randomB64(int n) => base64Url
      .encode(List<int>.generate(n, (_) => _random.nextInt(256)))
      .replaceAll('=', '');

  // ---- QR pairing requests ----

  void _prune() {
    final now = _now();
    _requests.removeWhere((r) => !now.isBefore(r.expiresAt));
  }

  /// Null when [remoteIp] exceeded 10 creations in the last minute.
  PairRequest? createPairRequest(String remoteIp, {String? challenge}) {
    final now = _now();
    final times = _creations.putIfAbsent(remoteIp, () => []);
    times.removeWhere((t) => now.difference(t) >= const Duration(minutes: 1));
    if (times.length >= maxRequestsPerMinutePerIp) return null;
    times.add(now);
    _creations.removeWhere((_, v) => v.isEmpty);
    _prune();
    while (_requests.length >= maxPendingRequests) {
      _requests.removeAt(0);
    }
    final r = PairRequest(
      _randomB64(16),
      _randomB64(32),
      remoteIp,
      now,
      challenge,
    );
    _requests.add(r);
    _events.add(const PairingEvent());
    return r;
  }

  /// A still-pending, unexpired request, for the phone's confirm sheet.
  PairRequest? lookupRequest(String requestId, {String? proof}) {
    _prune();
    for (final r in _requests) {
      if (r.state == RequestState.pending &&
          constantTimeEquals(r.id, requestId) &&
          (r.challenge == null
              ? proof == null
              : proof != null &&
                    constantTimeEquals(
                      r.challenge!,
                      sha256.convert(utf8.encode(proof)).toString(),
                    ))) {
        return r;
      }
    }
    return null;
  }

  bool approveRequest(String requestId, {String? proof}) =>
      _decide(requestId, RequestState.approved, proof);
  bool denyRequest(String requestId, {String? proof}) =>
      _decide(requestId, RequestState.denied, proof);

  bool _decide(String requestId, RequestState to, String? proof) {
    final r = lookupRequest(requestId, proof: proof);
    if (r == null) return false;
    r.state = to;
    if (to == RequestState.approved) r.proof = proof;
    _events.add(const PairingEvent());
    return true;
  }

  /// Browser poll. Unknown/expired secret -> expired. An approved request
  /// yields the session token once (replacing any viewer) and is consumed.
  PairRequestStatus pairRequestStatus(String? secret) {
    _prune();
    PairRequest? found;
    if (secret != null) {
      for (final r in _requests) {
        if (constantTimeEquals(r.secret, secret)) found = r;
      }
    }
    if (found == null) return const PairRequestStatus(RequestState.expired);
    switch (found.state) {
      case RequestState.pending:
        return const PairRequestStatus(RequestState.pending);
      case RequestState.denied:
        _requests.remove(found);
        _events.add(const PairingEvent());
        return const PairRequestStatus(RequestState.denied);
      case RequestState.approved:
        _requests.remove(found);
        return PairRequestStatus(
          RequestState.approved,
          _issueToken(),
          found.proof,
        );
      case RequestState.expired:
        return const PairRequestStatus(RequestState.expired);
    }
  }

  /// True when [token] is the current, unexpired viewer token.
  bool validate(String? token) {
    checkExpiry();
    return token != null &&
        _token != null &&
        constantTimeEquals(token, _token!);
  }

  void checkExpiry() {
    if (_token != null && !_now().isBefore(_tokenExpires!)) {
      _endViewer(ViewerEndReason.expired);
    }
  }

  void revoke([ViewerEndReason reason = ViewerEndReason.revoked]) {
    if (_token == null) return;
    _endViewer(reason);
  }

  /// Session end: drop viewer and any pending code/requests.
  void reset() {
    _requests.clear();
    _code = null;
    _codeExpires = null;
    _failed = 0;
    revoke(ViewerEndReason.sessionEnded);
    _events.add(const PairingEvent());
  }

  ViewerInfo viewerInfo({required bool connected}) => ViewerInfo(
    paired: _token != null,
    connected: _token != null && connected,
    pairedSince: _pairedSince,
    expiresAt: _tokenExpires,
  );

  void _endViewer(ViewerEndReason reason) {
    final old = _token;
    if (old == null) return;
    _token = null;
    _pairedSince = null;
    _tokenExpires = null;
    _events.add(PairingEvent(endedToken: old, endReason: reason));
  }
}
