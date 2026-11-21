import 'package:material_ui/material_ui.dart';

import '../app/session_controller.dart';
import 'theme.dart';
import 'widgets.dart';

class FilesPage extends StatelessWidget {
  const FilesPage({super.key, required this.controller});
  final SessionController controller;

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: controller,
    builder: (context, _) {
      final d = controller.device;
      return PageScaffold(
        children: [
          const AppCard(
            title: 'Recordings',
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('No recordings', style: TextStyle(fontSize: 16)),
                SizedBox(height: 6),
                Text(
                  'Recording has not been started. Originals will be listed here once recording exists.',
                  style: TextStyle(color: Tok.textSecondary, fontSize: 14),
                ),
              ],
            ),
          ),
          AppCard(
            title: 'Storage',
            child: Column(
              children: [
                ValueRow('Free', formatBytes(d.storageFreeBytes)),
                ValueRow('Total', formatBytes(d.storageTotalBytes)),
              ],
            ),
          ),
        ],
      );
    },
  );
}
