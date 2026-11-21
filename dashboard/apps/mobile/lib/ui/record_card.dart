import 'package:material_ui/material_ui.dart';

import '../app/session_controller.dart';
import 'theme.dart';
import 'widgets.dart';

String recordingErrorText(String code) => switch (code) {
  'PERMISSION_MIC' =>
    'Microphone permission is needed: recordings always include audio.',
  'LOW_STORAGE' => 'Less than 2 GB free. Free some space, then record again.',
  'PROFILE_UNSUPPORTED' =>
    'This phone cannot record 1920×1080 at 60 fps with the camera in use.',
  'ALREADY_RECORDING' => 'Already recording.',
  'NOT_RECORDING' => 'Not recording.',
  'RECORDING_FINALIZE_FAILED' => 'The recording could not be saved.',
  'CAMERA_FAILED' => 'The camera could not start.',
  _ => 'Recording failed ($code).',
};

String formatElapsed(num? ms) {
  if (ms == null) return unavailable;
  final s = (ms / 1000).floor();
  return '${two(s ~/ 3600)}:${two(s % 3600 ~/ 60)}:${two(s % 60)}';
}

class RecordCard extends StatelessWidget {
  const RecordCard({super.key, required this.controller});
  final SessionController controller;

  Future<void> _toggle(BuildContext context) async {
    final rec = controller.recording;
    final err = rec.isRecording
        ? await rec.stopRecording()
        : await rec.startRecording();
    if (err != null && context.mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(recordingErrorText(err))));
    }
  }

  @override
  Widget build(BuildContext context) {
    final rec = controller.recording;
    final r = rec.recording;
    final state = rec.state;
    final recording = rec.isRecording;
    final profile = r['profile'] as Map?;
    final audio = r['audio'] as Map?;
    final err = r['error'] as String?;
    final warn = r['warning'] as String?;
    final canStart = controller.sessionRunning && !rec.busy;
    return AppCard(
      title: 'Local master recording',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (recording || state == 'stopping' || state == 'finalizing') ...[
            Row(
              children: [
                const Icon(
                  Icons.fiber_manual_record,
                  color: Tok.recordRed,
                  size: 14,
                ),
                const SizedBox(width: 8),
                Text(
                  state == 'recording'
                      ? 'REC ${formatElapsed(r['elapsed_ms'] as num?)}'
                      : (state == 'stopping' ? 'Stopping…' : 'Saving…'),
                  style: const TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.w600,
                    fontFeatures: tabular,
                  ),
                ),
                const Spacer(),
                Text(
                  formatBytes(r['bytes'] as int?),
                  style: const TextStyle(
                    fontFeatures: tabular,
                    color: Tok.textSecondary,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            if (profile != null)
              ValueRow(
                'Profile',
                '${profile['width']}×${profile['height']} · ${profile['fps']} fps · ${(profile['codec'] as String).toUpperCase()}',
              ),
            ValueRow(
              'Audio',
              audio == null
                  ? 'No audio'
                  : '${((audio['sample_rate'] as int) / 1000).toStringAsFixed(0)} kHz · ${audio['channels'] == 2 ? 'stereo' : 'mono'}',
              mono: true,
            ),
            const SizedBox(height: 8),
          ] else
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Text(
                controller.sessionRunning
                    ? 'Records video with audio to Movies/LocalVision. Independent of the PC video.'
                    : 'Start a session to record.',
                style: const TextStyle(color: Tok.textSecondary, fontSize: 14),
              ),
            ),
          if (warn != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(
                warn == 'THERMAL'
                    ? 'Phone is hot. Recording continues; the quality is not lowered.'
                    : 'Storage is running low.',
                style: const TextStyle(color: Tok.warning, fontSize: 14),
              ),
            ),
          if (err != null && !recording)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(
                recordingErrorText(err),
                style: const TextStyle(color: Tok.danger, fontSize: 14),
              ),
            ),
          if (state == 'available' && err == null)
            const Padding(
              padding: EdgeInsets.only(bottom: 8),
              child: Text(
                'Saved. See the Files tab.',
                style: TextStyle(color: Tok.success, fontSize: 14),
              ),
            ),
          SizedBox(
            width: double.infinity,
            height: 52,
            child: recording
                ? FilledButton.icon(
                    style: FilledButton.styleFrom(
                      backgroundColor: Tok.recordRed,
                      foregroundColor: Tok.ink950,
                    ),
                    onPressed: () => _toggle(context),
                    icon: const Icon(Icons.stop),
                    label: const Text('Stop recording'),
                  )
                : OutlinedButton.icon(
                    onPressed: canStart ? () => _toggle(context) : null,
                    icon: const Icon(Icons.fiber_manual_record_outlined),
                    label: Text(rec.busy ? 'Please wait…' : 'Record'),
                  ),
          ),
        ],
      ),
    );
  }
}
