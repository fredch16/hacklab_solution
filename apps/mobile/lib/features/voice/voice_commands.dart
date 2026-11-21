/// Static demo voice commands (ADR 0004 owner update).
String normalizeSpeech(String text) => text
    .toLowerCase()
    .replaceAll(RegExp(r"[^a-z0-9' ]"), ' ')
    .replaceAll(RegExp(r'\s+'), ' ')
    .trim();

final _politePrefix = RegExp(
  r'^(?:(?:okay|ok|please|can you|could you|would you) )+',
);
final _politeSuffix = RegExp(
  r'(?: (?:please|right now|now|thank you|okay|i think))+$',
);

String normalizeVoiceRequest(String text) =>
    normalizeSpeech(text)
        .replaceFirst(_politePrefix, '')
        .replaceFirst(_politeSuffix, '');

/// Match a whole spoken request after optional polite phrasing.
String? matchVoiceCommand(String text) {
  return switch (normalizeVoiceRequest(text)) {
    'record' ||
    'start recording' ||
    'start the recording' ||
    'begin recording' ||
    'begin the recording' => 'recording.start',
    'stop recording' ||
    'stop the recording' ||
    'end recording' ||
    'end the recording' => 'recording.stop',
    _ => null,
  };
}
