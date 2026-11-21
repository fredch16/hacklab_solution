import 'package:material_ui/material_ui.dart';

import '../app/session_controller.dart';
import 'capture_page.dart';
import 'files_page.dart';
import 'link_page.dart';
import 'theme.dart';
import 'voice_page.dart';
import 'widgets.dart';

class HomeShell extends StatefulWidget {
  const HomeShell({super.key, required this.controller});
  final SessionController controller;

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _tab = 0;

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    final pages = <Widget>[
      CapturePage(controller: c),
      const VoicePage(),
      FilesPage(controller: c),
      LinkPage(controller: c),
    ];
    return Scaffold(
      body: SafeArea(
        bottom: false,
        child: Column(
          children: [
            ListenableBuilder(
              listenable: c,
              builder: (context, child) => StatusLine(controller: c),
            ),
            Expanded(
              child: IndexedStack(index: _tab, children: pages),
            ),
          ],
        ),
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) => setState(() => _tab = i),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.videocam_outlined),
            label: 'Capture',
          ),
          NavigationDestination(icon: Icon(Icons.mic_none), label: 'Voice'),
          NavigationDestination(
            icon: Icon(Icons.folder_outlined),
            label: 'Files',
          ),
          NavigationDestination(
            icon: Icon(Icons.wifi_tethering),
            label: 'Link',
          ),
        ],
      ),
    );
  }
}

String phaseLabel(AppPhase p) => switch (p) {
  AppPhase.idle => 'Starting',
  AppPhase.requestingPermissions => 'Requesting permission',
  AppPhase.ready => 'Ready',
  AppPhase.active => 'Session active',
  AppPhase.interrupted => 'Interrupted',
  AppPhase.error => 'Error',
};

class StatusLine extends StatelessWidget {
  const StatusLine({super.key, required this.controller});
  final SessionController controller;

  @override
  Widget build(BuildContext context) {
    final c = controller;
    final d = c.device;
    final p = c.phase;
    final color = switch (p) {
      AppPhase.active => Tok.teal,
      AppPhase.interrupted => Tok.warning,
      AppPhase.error => Tok.danger,
      _ => Tok.textSecondary,
    };
    final battery = d.batteryPercent == null
        ? 'Battery $unavailable'
        : '${d.batteryPercent}%${d.charging == true
              ? ' charging'
              : d.plugged == true
              ? ' plugged in'
              : ''}';
    final viewers = c.server.viewerInfo.paired ? '1 viewer' : 'No viewer';
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
      decoration: const BoxDecoration(
        color: Tok.ink900,
        border: Border(bottom: BorderSide(color: Tok.ink700)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 12,
            runSpacing: 4,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              const Text(
                'LOCAL VISION',
                style: TextStyle(
                  fontSize: 12,
                  letterSpacing: 1.2,
                  fontWeight: FontWeight.w700,
                ),
              ),
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    p == AppPhase.active ? Icons.circle : Icons.circle_outlined,
                    size: 10,
                    color: color,
                  ),
                  const SizedBox(width: 6),
                  Text(
                    phaseLabel(p),
                    style: TextStyle(fontSize: 13, color: color),
                  ),
                ],
              ),
              Text(
                battery,
                style: const TextStyle(
                  fontSize: 13,
                  color: Tok.textSecondary,
                  fontFeatures: tabular,
                ),
              ),
              Text(
                'Thermal ${d.thermal ?? unavailable}',
                style: const TextStyle(fontSize: 13, color: Tok.textSecondary),
              ),
            ],
          ),
          const SizedBox(height: 2),
          Text(
            'Phone · $viewers · LAN ${c.addresses.isEmpty ? 'not connected' : c.addresses.first}',
            style: const TextStyle(
              fontSize: 12,
              color: Tok.textTertiary,
              fontFeatures: tabular,
            ),
          ),
        ],
      ),
    );
  }
}
