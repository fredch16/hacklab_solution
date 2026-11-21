import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../platform/camera_platform.dart';
import '../connectivity/signaling.dart';

const _recordingKeys = [
  'state',
  'id',
  'started_utc',
  'elapsed_ms',
  'bytes',
  'profile',
  'audio',
  'error',
  'warning',
];

/// Contract `recording` snapshot object: every key present, null when unknown.
Map<String, Object?> normalizeRecording(Map<String, Object?>? m) => {
  for (final k in _recordingKeys) k: m?[k],
  if (m?['state'] == null) 'state': 'idle',
};

/// Mirrors the native master recorder (fixed 1080p60 profile, ADR 0003).
class RecordingController extends ChangeNotifier implements RecordingPort {
  RecordingController(this.platform) {
    _sub = platform.events.listen(_onEvent, onError: (Object _) {});
  }

  final CameraPlatform platform;
  late final StreamSubscription<Map<String, Object?>> _sub;

  Map<String, Object?> recording = normalizeRecording(null);
  List<Map<String, Object?>> recordings = const [];

  String get state => recording['state'] as String;
  bool get isRecording => state == 'recording';
  bool get busy =>
      const {'starting', 'recording', 'stopping', 'finalizing'}.contains(state);

  /// Reads native state once (app start, so previous recordings are listed).
  Future<void> refresh() async {
    final r = await platform.recordState();
    if (r.recording != null) recording = normalizeRecording(r.recording);
    recordings = r.recordings;
    notifyListeners();
  }

  @override
  Future<String?> startRecording() => platform.recordStart();

  @override
  Future<String?> stopRecording() => platform.recordStop('user');

  /// Session end: finalize any recording so the file is playable.
  Future<void> end() async {
    if (isRecording) await platform.recordStop('session_ended');
  }

  void _onEvent(Map<String, Object?> e) {
    switch (e['event']) {
      case 'recording':
        recording = normalizeRecording(
          e['recording'] == null
              ? null
              : Map<String, Object?>.from(e['recording'] as Map),
        );
        notifyListeners();
      case 'recordings':
        recordings = [
          for (final i in (e['items'] as List? ?? const []))
            Map<String, Object?>.from(i as Map),
        ];
        notifyListeners();
    }
  }

  @override
  void dispose() {
    _sub.cancel();
    super.dispose();
  }
}
