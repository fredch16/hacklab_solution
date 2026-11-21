import 'package:material_ui/material_ui.dart';

import 'theme.dart';
import 'widgets.dart';

class VoicePage extends StatelessWidget {
  const VoicePage({super.key});

  @override
  Widget build(BuildContext context) => const PageScaffold(
    children: [
      AppCard(
        title: 'Voice',
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            StatusChip('Voice · Not started', icon: Icons.mic_off_outlined),
            SizedBox(height: 12),
            Text(
              'Wake word and offline speech transcription arrive in a later '
              'update. Nothing is listening and no transcript exists.',
              style: TextStyle(color: Tok.textSecondary, fontSize: 14),
            ),
          ],
        ),
      ),
    ],
  );
}
