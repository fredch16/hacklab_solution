import 'dart:async';

import 'package:material_ui/material_ui.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:qr_flutter/qr_flutter.dart';

import '../app/session_controller.dart';
import '../features/connectivity/pairing.dart';
import 'scan_page.dart';
import 'session_button.dart';
import 'theme.dart';
import 'widgets.dart';

class LinkPage extends StatefulWidget {
  const LinkPage({super.key, required this.controller});
  final SessionController controller;

  @override
  State<LinkPage> createState() => _LinkPageState();
}

class _LinkPageState extends State<LinkPage> {
  Timer? _tick;
  int _selected = 0;

  SessionController get c => widget.controller;

  @override
  void initState() {
    super.initState();
    _tick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && c.sessionRunning) setState(() {});
    });
  }

  @override
  void dispose() {
    _tick?.cancel();
    super.dispose();
  }

  Future<void> _scan() async {
    final ok = await Navigator.of(context)
        .push<bool>(MaterialPageRoute(builder: (_) => ScanPage(controller: c)));
    if (ok == true && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Approved. The browser will connect in a moment.'),
        ),
      );
    }
  }

  Future<void> _pair() async {
    if (c.server.viewerInfo.paired) {
      final ok = await showDialog<bool>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: const Text('Replace current viewer?'),
          content: const Text(
            'Pairing a new browser disconnects the browser that is paired now. '
            'It will have to be paired again.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('Continue'),
            ),
          ],
        ),
      );
      if (ok != true) return;
    }
    c.pairing.createOneTimeCode();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: c,
    builder: (context, _) {
      final running = c.sessionRunning;
      final addrs = c.addresses;
      final sel = _selected < addrs.length ? _selected : 0;
      return PageScaffold(
        children: [
          if (!running)
            AppCard(
              title: 'Local dashboard',
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'Start a session to serve the dashboard to a browser on this network.',
                    style: TextStyle(color: Tok.textSecondary, fontSize: 14),
                  ),
                  const SizedBox(height: 12),
                  SessionButton(controller: c),
                ],
              ),
            ),
          if (running) _addressCard(addrs, sel),
          if (running) _certCard(),
          if (running) _pairCard(),
          if (running) _viewerCard(),
          _permissionsCard(),
          if (running) AppCard(child: SessionButton(controller: c)),
        ],
      );
    },
  );

  Widget _addressCard(List<String> addrs, int sel) => AppCard(
    title: 'Open on your computer',
    child: addrs.isEmpty
        ? const Text(
            'No local network address. Connect the phone to Wi-Fi (or enable hotspot) and the address will appear here.',
            style: TextStyle(color: Tok.warning, fontSize: 14),
          )
        : Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              for (var i = 0; i < addrs.length; i++)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 4),
                  child: InkWell(
                    onTap: () => setState(() => _selected = i),
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(minHeight: 48),
                      child: Row(
                        children: [
                          Icon(
                            i == sel
                                ? Icons.radio_button_checked
                                : Icons.radio_button_off,
                            size: 20,
                            color: Tok.teal,
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: SelectableText(
                              'https://${addrs[i]}:${c.server.port}',
                              style: const TextStyle(
                                fontSize: 16,
                                fontFeatures: tabular,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              const SizedBox(height: 8),
              Center(
                child: Semantics(
                  label: 'QR code for https://${addrs[sel]}:${c.server.port}',
                  child: Container(
                    color: Colors.white,
                    padding: const EdgeInsets.all(10),
                    child: QrImageView(
                      data: 'https://${addrs[sel]}:${c.server.port}',
                      size: 168,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 8),
              const Text(
                'The QR code holds only the address, not a password. Pair with the code below.',
                style: TextStyle(color: Tok.textTertiary, fontSize: 12),
              ),
              if (c.addressesNotCovered)
                const Padding(
                  padding: EdgeInsets.only(top: 8),
                  child: Text(
                    'The network address changed. End and start the session so the certificate covers it.',
                    style: TextStyle(color: Tok.warning, fontSize: 14),
                  ),
                ),
            ],
          ),
  );

  Widget _certCard() => AppCard(
    title: 'Certificate fingerprint (SHA-256)',
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SelectableText(
          c.server.certSha256 ?? unavailable,
          style: const TextStyle(
            fontSize: 13,
            fontFeatures: tabular,
            color: Tok.blue,
          ),
        ),
        const SizedBox(height: 8),
        const Text(
          'Your browser will warn that the connection is not private because this '
          'phone signs its own certificate. Open the certificate details in the '
          'warning page and check that its SHA-256 fingerprint matches this one, '
          'then continue.',
          style: TextStyle(color: Tok.textSecondary, fontSize: 14),
        ),
      ],
    ),
  );

  Widget _pairCard() {
    final s = c.pairing.codeState;
    final left = s.expiresAt?.difference(DateTime.now());
    final secs = left == null
        ? 0
        : left.inSeconds.clamp(0, pairingCodeTtl.inSeconds);
    final Widget status = switch (s.status) {
      CodeStatus.active => Column(
        children: [
          Semantics(
            label: 'Pairing code ${s.code!.split('').join(' ')}',
            child: Text(
              s.code!,
              style: const TextStyle(
                fontSize: 44,
                letterSpacing: 6,
                fontWeight: FontWeight.w600,
                fontFeatures: tabular,
              ),
            ),
          ),
          Text(
            'Expires in ${secs ~/ 60}:${two(secs % 60)} · ${s.remainingAttempts} ${s.remainingAttempts == 1 ? 'attempt' : 'attempts'} left',
            style: const TextStyle(
              color: Tok.textSecondary,
              fontSize: 14,
              fontFeatures: tabular,
            ),
          ),
        ],
      ),
      CodeStatus.locked => const Text(
        'Too many attempts — generate a new code.',
        style: TextStyle(color: Tok.danger, fontSize: 16),
      ),
      CodeStatus.expired => const Text(
        'Code expired — generate a new code.',
        style: TextStyle(color: Tok.warning, fontSize: 16),
      ),
      CodeStatus.none => const SizedBox.shrink(),
    };
    return AppCard(
      title: 'Pair a browser',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (s.status != CodeStatus.none) ...[
            Center(child: status),
            const SizedBox(height: 12),
          ],
          FilledButton.icon(
            onPressed: _scan,
            icon: const Icon(Icons.qr_code_scanner),
            label: const Text('Scan to pair'),
          ),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            onPressed: _pair,
            icon: const Icon(Icons.pin_outlined),
            label: Text(
              s.status == CodeStatus.active ? 'New code' : 'Use a code instead',
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            'Scan the QR code shown in the browser. Pairing replaces the currently paired browser. A code works once and lasts 2 minutes.',
            style: TextStyle(color: Tok.textTertiary, fontSize: 12),
          ),
        ],
      ),
    );
  }

  Widget _viewerCard() {
    final v = c.server.viewerInfo;
    return AppCard(
      title: 'Paired viewer',
      child: !v.paired
          ? const Text(
              'No browser paired.',
              style: TextStyle(color: Tok.textSecondary, fontSize: 14),
            )
          : Column(
              children: [
                ValueRow(
                  'Status',
                  v.connected ? 'Connected' : 'Not connected',
                  mono: false,
                ),
                ValueRow('Paired since', formatTime(v.pairedSince)),
                ValueRow('Access expires', formatTime(v.expiresAt)),
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
                  child: OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(
                      foregroundColor: Tok.danger,
                      side: const BorderSide(color: Tok.danger),
                    ),
                    onPressed: c.pairing.revoke,
                    icon: const Icon(Icons.block),
                    label: const Text('Revoke'),
                  ),
                ),
              ],
            ),
    );
  }

  Widget _permissionsCard() => AppCard(
    title: 'Permissions',
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Camera and microphone are not used yet. You can grant them now so they are ready; nothing is recorded or listened to.',
          style: TextStyle(color: Tok.textSecondary, fontSize: 14),
        ),
        const SizedBox(height: 8),
        _permRow('Camera', 'camera', Permission.camera),
        _permRow('Microphone', 'microphone', Permission.microphone),
      ],
    ),
  );

  Widget _permRow(String label, String key, Permission p) {
    final s = c.permissions[key] ?? 'not_requested';
    final text = s.replaceAll('_', ' ');
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(label, style: const TextStyle(fontSize: 14)),
                Text(
                  text[0].toUpperCase() + text.substring(1),
                  style: TextStyle(
                    fontSize: 13,
                    color: s == 'granted' ? Tok.success : Tok.textSecondary,
                  ),
                ),
              ],
            ),
          ),
          if (s == 'permanently_denied' || s == 'restricted')
            OutlinedButton(
              onPressed: c.openSettings,
              child: const Text('Open settings'),
            )
          else if (s != 'granted')
            OutlinedButton(
              onPressed: () => c.requestPermission(p),
              child: const Text('Allow'),
            ),
        ],
      ),
    );
  }
}
