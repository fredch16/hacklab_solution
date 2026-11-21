import 'package:material_ui/material_ui.dart';

import '../app/session_controller.dart';
import 'theme.dart';
import 'widgets.dart';

String voiceStateLabel(String state, String? error) => switch (state) {
  'unavailable' => 'Voice · Starts with the video stream',
  'loading_model' => 'Voice · Loading models…',
  'listening' => 'Voice · Listening for the wake phrase',
  'wake_detected' => 'Voice · Listening for command',
  'recognizing' => 'Voice · Transcribing',
  'cooling_down' => 'Voice · Getting ready to listen',
  'muted' => 'Voice · Muted',
  'error' => switch (error) {
    'PERMISSION_MIC' => 'Voice · Microphone permission needed',
    'MODEL_MISSING' => 'Voice · Speech models are missing',
    'VOICE_PROCESSING_FAILED' => 'Voice · Restart the stream to retry',
    'INVALID_WAKE_PHRASE' => 'Voice · This wake phrase cannot be used',
    _ => 'Voice · Error',
  },
  _ => 'Voice · $state',
};

class VoicePage extends StatelessWidget {
  const VoicePage({super.key, required this.controller});
  final SessionController controller;

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([controller, controller.voice]),
    builder: (context, _) {
      final v = controller.voice;
      final state = v.state;
      final phrase = v.phrase;
      final active =
          state == 'listening' ||
          state == 'wake_detected' ||
          state == 'recognizing';
      return PageScaffold(
        children: [
          AppCard(
            title: 'Voice',
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                StatusChip(
                  voiceStateLabel(state, v.voice['error'] as String?),
                  icon: active
                      ? Icons.mic
                      : (state == 'muted' ? Icons.mic_off : Icons.mic_none),
                  color: active
                      ? Tok.teal
                      : (state == 'error' ? Tok.danger : Tok.textSecondary),
                ),
                const SizedBox(height: 12),
                ValueRow(
                  'Wake phrase',
                  phrase == null ? null : '“$phrase”',
                  mono: false,
                ),
                if (phrase == null)
                  const Padding(
                    padding: EdgeInsets.only(top: 4),
                    child: Text(
                      'Set a wake name in the dashboard.',
                      style: TextStyle(color: Tok.textSecondary, fontSize: 14),
                    ),
                  ),
                const SizedBox(height: 8),
                const Text(
                  'Offline on this phone: no audio leaves the device. The phrase is set from the PC dashboard. Transcripts are kept in memory for this session only.',
                  style: TextStyle(color: Tok.textTertiary, fontSize: 12),
                ),
              ],
            ),
          ),
          if (v.partial != null)
            AppCard(
              title: 'Hearing now',
              child: Text(
                v.partial!.isEmpty ? '…' : v.partial!,
                style: const TextStyle(fontSize: 18, color: Tok.blue),
              ),
            ),
          AppCard(
            title: 'Transcripts',
            child: v.transcripts.isEmpty
                ? const Text(
                    'No transcripts yet.',
                    style: TextStyle(color: Tok.textSecondary, fontSize: 14),
                  )
                : Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [for (final t in v.transcripts) _row(t)],
                  ),
          ),
        ],
      );
    },
  );

  Widget _row(Map<String, Object?> t) {
    final text = t['text'] as String;
    final reason = t['end_reason'] as String;
    final when = DateTime.tryParse(t['ended_utc'] as String? ?? '');
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            text.isEmpty ? '(no speech)' : text,
            style: TextStyle(
              fontSize: 16,
              color: text.isEmpty ? Tok.textTertiary : Tok.textPrimary,
            ),
          ),
          Text(
            '${formatTime(when)} · ${reason.replaceAll('_', ' ')}',
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
