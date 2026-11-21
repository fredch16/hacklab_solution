import '../../platform/device_status.dart';
import 'pairing.dart';

String? _utc(DateTime? t) => t?.toUtc().toIso8601String();

/// Builds the contract `session.snapshot` payload. Unknown values are null.
Map<String, Object?> buildSnapshotPayload({
  required String app,
  required DateTime? sessionStartedUtc,
  required bool keepScreenOn,
  required DeviceStatus device,
  required String? appVersion,
  required Map<String, String> permissions,
  required List<String> addresses,
  required int port,
  required String? certSha256,
  required ViewerInfo viewer,
}) => {
  'app': app,
  'session_started_utc': _utc(sessionStartedUtc),
  'keep_screen_on': keepScreenOn,
  'device': {
    'model': device.model,
    'manufacturer': device.manufacturer,
    'os': device.os,
    'sdk_int': device.sdkInt,
    'app_version': appVersion,
  },
  'permissions': {
    'camera': permissions['camera'] ?? 'not_requested',
    'microphone': permissions['microphone'] ?? 'not_requested',
  },
  'camera': {'state': 'not_started'},
  'recording': {'state': 'not_started'},
  'voice': {'state': 'not_started'},
  'battery': {
    'level_percent': device.batteryPercent,
    'charging': device.charging,
    'plugged': device.plugged,
  },
  'thermal': {'status': device.thermal},
  'storage': {
    'free_bytes': device.storageFreeBytes,
    'total_bytes': device.storageTotalBytes,
  },
  'network': {
    'addresses': addresses,
    'port': port,
    'tls': 'self_signed',
    'cert_sha256': certSha256,
  },
  'viewer': {
    'paired': viewer.paired,
    'connected': viewer.connected,
    'paired_since_utc': _utc(viewer.pairedSince),
    'expires_utc': _utc(viewer.expiresAt),
  },
};
