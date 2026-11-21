/// Static demo voice commands (ADR 0004 owner update): only these phrases act.
String normalizeSpeech(String text) => text
    .toLowerCase()
    .replaceAll(RegExp(r"[^a-z0-9' ]"), ' ')
    .replaceAll(RegExp(r'\s+'), ' ')
    .trim();

/// 'recording.stop', 'recording.start' or null. Stop is checked first.
String? matchVoiceCommand(String text) {
  final t = ' ${normalizeSpeech(text)} ';
  const stops = [
    ' stop recording ',
    ' stop the recording ',
    ' end recording ',
    ' end the recording ',
  ];
  const starts = [
    ' start recording ',
    ' start the recording ',
    ' begin recording ',
    ' begin the recording ',
  ];
  if (stops.any(t.contains)) return 'recording.stop';
  if (starts.any(t.contains) || t.contains(' record '))
    return 'recording.start';
  return null;
}
