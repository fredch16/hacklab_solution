import 'package:material_ui/material_ui.dart';

import '../app/session_controller.dart';
import 'theme.dart';

class SessionButton extends StatelessWidget {
  const SessionButton({super.key, required this.controller});
  final SessionController controller;

  @override
  Widget build(BuildContext context) {
    final c = controller;
    final running = c.sessionRunning;
    final starting = c.phase == AppPhase.idle;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (c.errorMessage != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(
              c.errorMessage!,
              style: const TextStyle(color: Tok.danger, fontSize: 14),
            ),
          ),
        running
            ? OutlinedButton.icon(
                onPressed: c.endSession,
                icon: const Icon(Icons.stop_circle_outlined),
                label: const Text('End session'),
              )
            : FilledButton.icon(
                onPressed: starting ? null : c.startSession,
                icon: const Icon(Icons.play_arrow),
                label: const Text('Start session'),
              ),
      ],
    );
  }
}
