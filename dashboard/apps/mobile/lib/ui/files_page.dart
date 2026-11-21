import 'package:material_ui/material_ui.dart';

import '../app/session_controller.dart';
import 'record_card.dart';
import 'theme.dart';
import 'widgets.dart';

class FilesPage extends StatelessWidget {
  const FilesPage({super.key, required this.controller});
  final SessionController controller;

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([controller, controller.recording]),
    builder: (context, _) {
      final d = controller.device;
      final recs = controller.recording.recordings;
      return PageScaffold(
        children: [
          AppCard(
            title: 'Recordings',
            child: recs.isEmpty
                ? const Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('No recordings', style: TextStyle(fontSize: 16)),
                      SizedBox(height: 6),
                      Text(
                        'Recordings made on this phone appear here, newest first. They are saved in Movies/LocalVision.',
                        style: TextStyle(
                          color: Tok.textSecondary,
                          fontSize: 14,
                        ),
                      ),
                    ],
                  )
                : Column(children: [for (final r in recs) _row(r)]),
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

  Widget _row(Map<String, Object?> r) {
    final dur = formatElapsed(r['duration_ms'] as num?);
    final fps = r['fps_measured'] is num
        ? '${(r['fps_measured'] as num).toStringAsFixed(1)} fps'
        : 'fps unavailable';
    final created = DateTime.tryParse(r['created_utc'] as String? ?? '');
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            r['filename'] as String,
            style: const TextStyle(fontSize: 15, fontFeatures: tabular),
          ),
          const SizedBox(height: 2),
          Text(
            '${formatTime(created)} · $dur · ${formatBytes(r['size_bytes'] as int?)}',
            style: const TextStyle(
              fontSize: 13,
              color: Tok.textSecondary,
              fontFeatures: tabular,
            ),
          ),
          Text(
            '${r['width']}×${r['height']} · $fps · ${(r['codec'] as String).toUpperCase()}',
            style: const TextStyle(
              fontSize: 13,
              color: Tok.textTertiary,
              fontFeatures: tabular,
            ),
          ),
        ],
      ),
    );
  }
}
