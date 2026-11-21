import 'dart:io';

/// Contract: only loopback, RFC1918, link-local and CGNAT peers may connect.
bool isAllowedPeer(InternetAddress a) {
  if (a.isLoopback) return true;
  if (a.type == InternetAddressType.IPv6) return a.isLinkLocal;
  final b = a.rawAddress;
  if (b.length != 4) return false;
  return b[0] == 10 ||
      (b[0] == 172 && b[1] >= 16 && b[1] <= 31) ||
      (b[0] == 192 && b[1] == 168) ||
      (b[0] == 169 && b[1] == 254) ||
      (b[0] == 100 && b[1] >= 64 && b[1] <= 127);
}

/// Contract: Origin must equal `https://<Host header>`.
bool originAllowed(String? origin, String? host) =>
    origin != null && host != null && origin == 'https://$host';
