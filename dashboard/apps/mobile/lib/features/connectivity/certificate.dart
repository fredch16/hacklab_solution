import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:asn1lib/asn1lib.dart';
import 'package:basic_utils/basic_utils.dart';
import 'package:crypto/crypto.dart';

import 'pairing.dart';

class ServerCert {
  const ServerCert(this.certPem, this.keyPem, this.addresses, this.sha256);
  final Uint8List certPem;
  final Uint8List keyPem;
  final List<String> addresses;

  /// Uppercase hex, colon separated, over the DER certificate.
  final String sha256;

  SecurityContext context() => SecurityContext()
    ..useCertificateChainBytes(certPem)
    ..usePrivateKeyBytes(keyPem);
}

/// Generates and persists a self-signed EC P-256 certificate whose SANs are
/// the phone's IPv4 addresses. Hand-built X.509 because basic_utils encodes
/// IP SANs as dNSName, which browsers ignore.
class CertificateService {
  CertificateService(this.dir, {Clock? clock, Random? random})
    : _now = clock ?? DateTime.now,
      _random = random ?? Random.secure();

  final Directory dir;
  final Clock _now;
  final Random _random;
  static const _validDays = 365;
  static const _renewBefore = Duration(days: 30);

  Future<ServerCert> ensure(List<String> addresses) async {
    final certF = File('${dir.path}/cert.pem');
    final keyF = File('${dir.path}/key.pem');
    final metaF = File('${dir.path}/cert_meta.json');
    if (await certF.exists() && await keyF.exists() && await metaF.exists()) {
      try {
        final meta =
            jsonDecode(await metaF.readAsString()) as Map<String, dynamic>;
        final have = (meta['addresses'] as List).cast<String>().toSet();
        final notAfter = DateTime.parse(meta['not_after'] as String);
        if (have.containsAll(addresses) &&
            _now().add(_renewBefore).isBefore(notAfter)) {
          final pem = await certF.readAsBytes();
          return ServerCert(
            pem,
            await keyF.readAsBytes(),
            have.toList(),
            _fingerprint(_pemToDer(utf8.decode(pem))),
          );
        }
      } catch (_) {
        // Corrupt or unreadable: regenerate below.
      }
    }
    final ips = addresses.isEmpty ? ['127.0.0.1'] : addresses;
    final made = _generate(ips);
    await dir.create(recursive: true);
    await certF.writeAsBytes(made.certPem, flush: true);
    await keyF.writeAsBytes(made.keyPem, flush: true);
    await metaF.writeAsString(
      jsonEncode({
        'addresses': ips,
        'not_after': _now()
            .add(const Duration(days: _validDays))
            .toUtc()
            .toIso8601String(),
      }),
      flush: true,
    );
    return made;
  }

  ServerCert _generate(List<String> ips) {
    final pair = CryptoUtils.generateEcKeyPair();
    final priv = pair.privateKey as ECPrivateKey;
    final pub = pair.publicKey as ECPublicKey;
    final now = _now();

    final name = ASN1Sequence()
      ..add(
        ASN1Set()..add(
          ASN1Sequence()
            ..add(ASN1ObjectIdentifier.fromComponentString('2.5.4.3'))
            ..add(ASN1UTF8String('Local Vision')),
        ),
      );
    final sigAlg = ASN1Sequence()
      ..add(ASN1ObjectIdentifier.fromComponentString('1.2.840.10045.4.3.2'));
    final spki = ASN1Sequence()
      ..add(
        ASN1Sequence()
          ..add(ASN1ObjectIdentifier.fromComponentString('1.2.840.10045.2.1'))
          ..add(
            ASN1ObjectIdentifier.fromComponentString('1.2.840.10045.3.1.7'),
          ),
      )
      ..add(ASN1BitString(pub.Q!.getEncoded(false)));

    final serial = List<int>.generate(16, (_) => _random.nextInt(256));
    serial[0] &= 0x7f; // keep the INTEGER positive
    final tbs = ASN1Sequence()
      ..add(ASN1Object.preEncoded(0xA0, ASN1Integer.fromInt(2).encodedBytes))
      ..add(
        ASN1Integer(
          BigInt.parse(
            serial.map((b) => b.toRadixString(16).padLeft(2, '0')).join(),
            radix: 16,
          ),
        ),
      )
      ..add(sigAlg)
      ..add(name)
      ..add(
        ASN1Sequence()
          ..add(ASN1UtcTime(now.subtract(const Duration(days: 1)).toUtc()))
          ..add(ASN1UtcTime(now.add(const Duration(days: _validDays)).toUtc())),
      )
      ..add(name)
      ..add(spki)
      ..add(ASN1Object.preEncoded(0xA3, _extensions(ips).encodedBytes));

    final sig = CryptoUtils.ecSign(
      priv,
      tbs.encodedBytes,
      algorithmName: 'SHA-256/ECDSA',
    );
    final cert = ASN1Sequence()
      ..add(tbs)
      ..add(sigAlg)
      ..add(
        ASN1BitString(
          (ASN1Sequence()
                ..add(ASN1Integer(sig.r))
                ..add(ASN1Integer(sig.s)))
              .encodedBytes,
        ),
      );
    final der = cert.encodedBytes;
    final b64 = base64.encode(der);
    final lines = RegExp('.{1,64}')
        .allMatches(b64)
        .map((m) => m.group(0))
        .join('\n');
    final pem =
        '-----BEGIN CERTIFICATE-----\n$lines\n-----END CERTIFICATE-----\n';
    return ServerCert(
      Uint8List.fromList(utf8.encode(pem)),
      Uint8List.fromList(
        utf8.encode(CryptoUtils.encodeEcPrivateKeyToPem(priv)),
      ),
      ips,
      _fingerprint(der),
    );
  }

  ASN1Sequence _extensions(List<String> ips) {
    ASN1Sequence ext(String oid, ASN1Object value, {bool critical = false}) {
      final s = ASN1Sequence()
        ..add(ASN1ObjectIdentifier.fromComponentString(oid));
      if (critical) s.add(ASN1Boolean(true));
      return s..add(ASN1OctetString(value.encodedBytes));
    }

    final san = ASN1Sequence();
    for (final ip in ips) {
      san.add(
        ASN1Object.preEncoded(
          0x87,
          Uint8List.fromList(InternetAddress(ip).rawAddress),
        ),
      );
    }
    return ASN1Sequence()
      ..add(ext('2.5.29.17', san))
      ..add(ext('2.5.29.19', ASN1Sequence(), critical: true)) // CA:false
      ..add(
        ext(
          '2.5.29.37',
          ASN1Sequence()..add(
            ASN1ObjectIdentifier.fromComponentString('1.3.6.1.5.5.7.3.1'),
          ),
        ),
      );
  }
}

Uint8List _pemToDer(String pem) =>
    base64.decode(pem.replaceAll(RegExp(r'-----[A-Z ]+-----|\s'), ''));

String _fingerprint(List<int> der) => sha256
    .convert(der)
    .bytes
    .map((b) => b.toRadixString(16).padLeft(2, '0').toUpperCase())
    .join(':');
