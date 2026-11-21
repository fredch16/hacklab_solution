import 'dart:async';

import 'package:material_ui/material_ui.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:permission_handler/permission_handler.dart';

import '../app/session_controller.dart';
import 'theme.dart';

/// Returns the request id from `lvc-pair:1:<id>`, else null.
String? parsePairQr(String raw) {
  final m = RegExp(r'^lvc-pair:1:([A-Za-z0-9_-]{22})$').firstMatch(raw.trim());
  return m?.group(1);
}

/// Full-screen QR scanner. The camera exists only while this page is mounted.
class ScanPage extends StatefulWidget {
  const ScanPage({super.key, required this.controller});
  final SessionController controller;

  @override
  State<ScanPage> createState() => _ScanPageState();
}

class _ScanPageState extends State<ScanPage> {
  MobileScannerController? _scanner;
  String? _message;
  Timer? _clear;
  var _busy = false;

  SessionController get c => widget.controller;
  bool get _granted => c.permissions['camera'] == 'granted';

  @override
  void initState() {
    super.initState();
    c.addListener(_onController);
    _syncScanner();
  }

  void _onController() {
    _syncScanner();
    if (mounted) setState(() {});
  }

  void _syncScanner() {
    if (_granted && _scanner == null) {
      _scanner = MobileScannerController(
        formats: const [BarcodeFormat.qrCode],
        detectionSpeed: DetectionSpeed.noDuplicates,
      );
    }
  }

  @override
  void dispose() {
    c.removeListener(_onController);
    _clear?.cancel();
    _scanner?.dispose(); // releases the camera
    super.dispose();
  }

  void _say(String m) {
    setState(() => _message = m);
    _clear?.cancel();
    _clear = Timer(const Duration(seconds: 4), () {
      if (mounted) setState(() => _message = null);
    });
  }

  Future<void> _onDetect(BarcodeCapture cap) async {
    if (_busy) return;
    for (final b in cap.barcodes) {
      final raw = b.rawValue;
      if (raw == null) continue;
      final id = parsePairQr(raw);
      if (id == null) {
        _say('Not a Local Vision pairing code.');
        return;
      }
      final r = c.pairing.lookupRequest(id);
      if (r == null) {
        _say('This pairing code expired. Refresh the QR code in the browser.');
        return;
      }
      _busy = true;
      await _scanner?.stop();
      if (!mounted) return;
      final approve = await showModalBottomSheet<bool>(
        context: context,
        backgroundColor: Tok.ink800,
        builder: (ctx) => SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text(
                  'Pair this browser?',
                  style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 8),
                Text(
                  'Request from ${r.remoteIp}. Replaces the current viewer.',
                  style: const TextStyle(
                    color: Tok.textSecondary,
                    fontSize: 16,
                  ),
                ),
                const SizedBox(height: 24),
                FilledButton(
                  onPressed: () => Navigator.pop(ctx, true),
                  child: const Text('Approve'),
                ),
                const SizedBox(height: 8),
                OutlinedButton(
                  onPressed: () => Navigator.pop(ctx, false),
                  child: const Text('Deny'),
                ),
              ],
            ),
          ),
        ),
      );
      if (!mounted) return;
      if (approve == true) {
        if (!c.pairing.approveRequest(id)) {
          _say('The request expired before it was approved.');
        } else {
          Navigator.pop(context, true);
          return;
        }
      } else if (approve == false) {
        c.pairing.denyRequest(id);
        Navigator.pop(context, false);
        return;
      }
      _busy = false; // sheet dismissed without a choice: keep scanning
      await _scanner?.start();
      return;
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('Scan to pair'),
      backgroundColor: Tok.ink900,
      foregroundColor: Tok.textPrimary,
    ),
    body: _granted && _scanner != null ? _scanView() : _permissionView(),
  );

  Widget _scanView() => Stack(
    fit: StackFit.expand,
    children: [
      MobileScanner(controller: _scanner!, onDetect: _onDetect),
      Align(
        alignment: Alignment.bottomCenter,
        child: SafeArea(
          child: Container(
            margin: const EdgeInsets.all(16),
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: Tok.ink900,
              borderRadius: BorderRadius.circular(14),
            ),
            child: Text(
              _message ??
                  'Point the camera at the QR code shown in the browser.',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 15,
                color: _message == null ? Tok.textPrimary : Tok.warning,
              ),
            ),
          ),
        ),
      ),
    ],
  );

  Widget _permissionView() {
    final s = c.permissions['camera'] ?? 'not_requested';
    final blocked = s == 'permanently_denied' || s == 'restricted';
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Icon(Icons.qr_code_scanner, size: 56, color: Tok.teal),
            const SizedBox(height: 16),
            Text(
              blocked
                  ? 'Camera access is blocked. Open settings to allow it, or go back and pair with a code instead.'
                  : s == 'denied'
                  ? 'Camera access was denied. The camera is only used to read the pairing QR code on this screen. You can allow it, or go back and pair with a code instead.'
                  : 'The camera is only used to read the pairing QR code on this screen. Nothing is recorded or stored.',
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 16, color: Tok.textSecondary),
            ),
            const SizedBox(height: 24),
            if (blocked)
              FilledButton(
                onPressed: c.openSettings,
                child: const Text('Open settings'),
              )
            else
              FilledButton(
                onPressed: () => c.requestPermission(Permission.camera),
                child: const Text('Allow camera'),
              ),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Use a code instead'),
            ),
          ],
        ),
      ),
    );
  }
}
