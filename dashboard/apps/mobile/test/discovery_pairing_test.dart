import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:local_vision/features/connectivity/pairing.dart';
import 'package:local_vision/ui/scan_page.dart';

void main() {
  test('a discovered request can only be approved with the scanned proof', () {
    final pairing = PairingService();
    const proof = 'Abcdefghijklmnopqrstuvwxyz_0123456789ABCDEF';
    final challenge = sha256.convert(utf8.encode(proof)).toString();
    final request = pairing.createPairRequest(
      '192.168.1.20',
      challenge: challenge,
    )!;

    expect(pairing.approveRequest(request.id), isFalse);
    expect(pairing.approveRequest(request.id, proof: 'wrong'), isFalse);
    expect(
      pairing.pairRequestStatus(request.secret).state,
      RequestState.pending,
    );

    final code = parseScanPairQr('lvc-pair:2:${request.id}:$proof')!;
    expect(pairing.approveRequest(code.id, proof: code.proof), isTrue);
    final result = pairing.pairRequestStatus(request.secret);
    expect(result.proof, proof);
    expect(pairing.validate(result.token), isTrue);
    expect(pairing.pairRequestStatus(request.secret).token, isNull);
  });
}
