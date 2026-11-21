import 'package:material_ui/material_ui.dart';

import '../platform/device_status.dart';
import '../app/session_controller.dart';
import 'session_button.dart';
import 'theme.dart';
import 'widgets.dart';

class CapturePage extends StatelessWidget {
  const CapturePage({super.key, required this.controller});
  final SessionController controller;

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: controller,
    builder: (context, _) {
      final c = controller;
      final d = c.device;
      final v = c.server.viewerInfo;
      return PageScaffold(
        children: [
          AppCard(
            title: 'Camera',
            child: Column(
              children: [
                AspectRatio(
                  aspectRatio: 16 / 9,
                  child: Container(
                    decoration: BoxDecoration(
                      color: Colors.black,
                      borderRadius: BorderRadius.circular(18),
                      border: Border.all(color: Tok.ink700),
                    ),
                    alignment: Alignment.center,
                    child: const Text(
                      'Camera · Not started',
                      style: TextStyle(color: Tok.textSecondary, fontSize: 14),
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                const Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    StatusChip('WebRTC · Not started', icon: Icons.cast),
                    StatusChip(
                      'Local master · Not started',
                      icon: Icons.save_outlined,
                    ),
                  ],
                ),
              ],
            ),
          ),
          AppCard(
            title: 'Session',
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  c.sessionRunning
                      ? 'Local dashboard is served on this network.'
                      : 'The local dashboard server runs only while a session is active.',
                  style: const TextStyle(
                    color: Tok.textSecondary,
                    fontSize: 14,
                  ),
                ),
                const SizedBox(height: 12),
                SessionButton(controller: c),
              ],
            ),
          ),
          AppCard(
            title: 'Device health',
            child: Column(
              children: [
                ValueRow(
                  'Battery',
                  d.batteryPercent == null
                      ? null
                      : '${d.batteryPercent}% · ${batteryLabel(d)}',
                ),
                ValueRow('Thermal', d.thermal, mono: false),
                ValueRow(
                  'Storage free',
                  d.storageFreeBytes == null
                      ? null
                      : formatBytes(d.storageFreeBytes),
                ),
              ],
            ),
          ),
          AppCard(
            title: 'Viewer',
            child: Column(
              children: [
                ValueRow(
                  'Browser',
                  v.paired
                      ? (v.connected
                            ? 'Paired · connected'
                            : 'Paired · not connected')
                      : 'No viewer paired',
                  mono: false,
                ),
                if (v.paired)
                  ValueRow('Paired since', formatTime(v.pairedSince)),
              ],
            ),
          ),
        ],
      );
    },
  );
}
