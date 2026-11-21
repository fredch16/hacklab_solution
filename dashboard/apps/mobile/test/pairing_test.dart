import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/connectivity/pairing.dart';

class FakeClock {
  DateTime t = DateTime.utc(2026, 11, 21, 14);
  DateTime call() => t;
  void advance(Duration d) => t = t.add(d);
}

void main() {
  late FakeClock clock;
  late PairingService p;
  setUp(() {
    clock = FakeClock();
    p = PairingService(clock: clock.call, random: Random(1));
  });

  test('code is 6 digits, zero padded', () {
    for (var i = 0; i < 50; i++) {
      expect(p.createOneTimeCode().code, matches(RegExp(r'^\d{6}$')));
    }
  });

  test('code expires at 120 s', () {
    final code = p.createOneTimeCode().code!;
    clock.advance(const Duration(seconds: 119));
    expect(p.codeState.status, CodeStatus.active);
    clock.advance(const Duration(seconds: 1));
    expect(p.authorizeViewer(code).status, PairStatus.invalid);
    expect(p.codeState.status, CodeStatus.expired);
  });

  test('code is single use', () {
    final code = p.createOneTimeCode().code!;
    expect(p.authorizeViewer(code).status, PairStatus.ok);
    expect(p.authorizeViewer(code).status, PairStatus.invalid);
  });

  test('no active code is invalid, not locked', () {
    expect(p.authorizeViewer('123456').status, PairStatus.invalid);
  });

  test('5 wrong attempts lock the code, even the right one', () {
    final code = p.createOneTimeCode().code!;
    final wrong = code == '000000' ? '000001' : '000000';
    for (var i = 0; i < 5; i++) {
      expect(p.authorizeViewer(wrong).status, PairStatus.invalid);
    }
    expect(p.codeState.status, CodeStatus.locked);
    expect(p.codeState.remainingAttempts, 0);
    expect(p.authorizeViewer(code).status, PairStatus.locked);
    expect(p.authorizeViewer(wrong).status, PairStatus.locked);
  });

  test('new code invalidates the old one and resets attempts', () {
    final old = p.createOneTimeCode().code!;
    p.authorizeViewer(old == '999999' ? '000000' : '999999');
    var fresh = p.createOneTimeCode().code!;
    while (fresh == old) {
      fresh = p.createOneTimeCode().code!;
    }
    expect(p.codeState.remainingAttempts, 5);
    expect(p.authorizeViewer(old).status, PairStatus.invalid);
    expect(p.authorizeViewer(fresh).status, PairStatus.ok);
  });

  test('constantTimeEquals', () {
    expect(constantTimeEquals('123456', '123456'), isTrue);
    expect(constantTimeEquals('123456', '123457'), isFalse);
    expect(constantTimeEquals('123456', '12345'), isFalse);
    expect(constantTimeEquals('', ''), isTrue);
  });

  test('token: 43-char base64url, valid for 12 h then expired', () {
    final events = <PairingEvent>[];
    p.events.listen(events.add);
    p.createOneTimeCode();
    final token = p.authorizeViewer(p.codeState.code!).token!;
    expect(token, matches(RegExp(r'^[A-Za-z0-9_-]{43}$')));
    expect(p.validate(token), isTrue);
    expect(p.validate('nope'), isFalse);
    clock.advance(const Duration(hours: 12) - const Duration(seconds: 1));
    expect(p.validate(token), isTrue);
    clock.advance(const Duration(seconds: 1));
    expect(p.validate(token), isFalse);
    expect(events.last.endReason, ViewerEndReason.expired);
    expect(p.viewerInfo(connected: true).paired, isFalse);
  });

  test('second pairing replaces the viewer (4001)', () {
    final events = <PairingEvent>[];
    p.events.listen(events.add);
    p.createOneTimeCode();
    final t1 = p.authorizeViewer(p.codeState.code!).token!;
    p.createOneTimeCode();
    final t2 = p.authorizeViewer(p.codeState.code!).token!;
    expect(p.validate(t1), isFalse);
    expect(p.validate(t2), isTrue);
    final ended = events.where((e) => e.endedToken != null).single;
    expect(ended.endedToken, t1);
    expect(ended.endReason!.closeCode, 4001);
  });

  test('revoke invalidates (4003) and is a no-op without a viewer', () {
    final events = <PairingEvent>[];
    p.events.listen(events.add);
    p.revoke();
    expect(events, isEmpty);
    p.createOneTimeCode();
    final t = p.authorizeViewer(p.codeState.code!).token!;
    events.clear();
    p.revoke();
    expect(p.validate(t), isFalse);
    expect(events.single.endReason!.closeCode, 4003);
  });

  test('reset ends viewer with 4004 and drops the code', () {
    final events = <PairingEvent>[];
    p.createOneTimeCode();
    p.authorizeViewer(p.codeState.code!);
    p.createOneTimeCode();
    p.events.listen(events.add);
    p.reset();
    expect(events.any((e) => e.endReason?.closeCode == 4004), isTrue);
    expect(p.codeState.status, CodeStatus.none);
  });

  group('QR pair requests', () {
    test('create: ids and secrets are distinct, request expires at 120 s', () {
      final r = p.createPairRequest('10.0.0.2')!;
      expect(r.id, matches(RegExp(r'^[A-Za-z0-9_-]{22}$')));
      expect(r.secret, matches(RegExp(r'^[A-Za-z0-9_-]{43}$')));
      expect(r.remoteIp, '10.0.0.2');
      clock.advance(const Duration(seconds: 119));
      expect(p.lookupRequest(r.id), isNotNull);
      clock.advance(const Duration(seconds: 1));
      expect(p.lookupRequest(r.id), isNull);
      expect(p.pairRequestStatus(r.secret).state, RequestState.expired);
    });

    test('at most 3 pending, oldest evicted', () {
      final rs = [for (var i = 0; i < 4; i++) p.createPairRequest('10.0.0.2')!];
      expect(p.lookupRequest(rs[0].id), isNull);
      expect(p.pairRequestStatus(rs[0].secret).state, RequestState.expired);
      for (final r in rs.skip(1)) {
        expect(p.lookupRequest(r.id), isNotNull);
      }
    });

    test(
      'rate limit: 10 per minute per IP, other IPs unaffected, window slides',
      () {
        for (var i = 0; i < 10; i++) {
          expect(p.createPairRequest('10.0.0.2'), isNotNull);
        }
        expect(p.createPairRequest('10.0.0.2'), isNull);
        expect(p.createPairRequest('10.0.0.3'), isNotNull);
        clock.advance(const Duration(seconds: 60));
        expect(p.createPairRequest('10.0.0.2'), isNotNull);
      },
    );

    test('pending until approved; approve issues the token exactly once', () {
      final r = p.createPairRequest('10.0.0.2')!;
      expect(p.pairRequestStatus(r.secret).state, RequestState.pending);
      expect(p.pairRequestStatus(r.secret).token, isNull);
      expect(p.approveRequest(r.id), isTrue);
      final st = p.pairRequestStatus(r.secret);
      expect(st.state, RequestState.approved);
      expect(p.validate(st.token), isTrue);
      final again = p.pairRequestStatus(r.secret);
      expect(again.state, RequestState.expired);
      expect(again.token, isNull);
    });

    test('request_id alone cannot obtain a token', () {
      final r = p.createPairRequest('10.0.0.2')!;
      p.approveRequest(r.id);
      expect(p.pairRequestStatus(r.id).state, RequestState.expired);
      expect(p.pairRequestStatus(r.id).token, isNull);
      expect(p.viewerInfo(connected: false).paired, isFalse);
      expect(p.pairRequestStatus(null).state, RequestState.expired);
    });

    test('deny is reported once; unknown secret is expired', () {
      final r = p.createPairRequest('10.0.0.2')!;
      expect(p.denyRequest(r.id), isTrue);
      expect(p.pairRequestStatus(r.secret).state, RequestState.denied);
      expect(p.pairRequestStatus(r.secret).state, RequestState.expired);
      expect(p.pairRequestStatus('nope').state, RequestState.expired);
      expect(p.approveRequest(r.id), isFalse);
      expect(p.approveRequest('unknown'), isFalse);
    });

    test('approved request replaces the existing viewer (4001)', () {
      final events = <PairingEvent>[];
      p.createPairRequest('10.0.0.2');
      p.createOneTimeCode();
      final t1 = p.authorizeViewer(p.codeState.code!).token!;
      p.events.listen(events.add);
      final r = p.createPairRequest('10.0.0.2')!;
      p.approveRequest(r.id);
      expect(p.validate(t1), isTrue); // not replaced until the browser polls
      final t2 = p.pairRequestStatus(r.secret).token!;
      expect(p.validate(t1), isFalse);
      expect(p.validate(t2), isTrue);
      expect(
        events.where((e) => e.endReason == ViewerEndReason.replaced),
        hasLength(1),
      );
    });

    test('reset drops pending requests', () {
      final r = p.createPairRequest('10.0.0.2')!;
      p.reset();
      expect(p.lookupRequest(r.id), isNull);
    });
  });
}
