import 'dart:io';

import 'package:flutter/services.dart';
import 'package:permission_handler/permission_handler.dart';

import '../features/connectivity/private_address.dart';

/// Every field is null when the platform cannot report it.
class DeviceStatus {
  const DeviceStatus({
    this.model,
    this.manufacturer,
    this.os,
    this.sdkInt,
    this.batteryPercent,
    this.charging,
    this.plugged,
    this.thermal,
    this.storageFreeBytes,
    this.storageTotalBytes,
  });
  const DeviceStatus.unknown() : this();

  final String? model, manufacturer, os, thermal;
  final int? sdkInt, batteryPercent, storageFreeBytes, storageTotalBytes;
  final bool? charging, plugged;

  DeviceStatus withBattery(BatteryStatus b) => DeviceStatus(
    model: model,
    manufacturer: manufacturer,
    os: os,
    sdkInt: sdkInt,
    batteryPercent: b.percent,
    charging: b.charging,
    plugged: b.plugged,
    thermal: thermal,
    storageFreeBytes: storageFreeBytes,
    storageTotalBytes: storageTotalBytes,
  );

  @override
  bool operator ==(Object other) =>
      other is DeviceStatus &&
      other.model == model &&
      other.manufacturer == manufacturer &&
      other.os == os &&
      other.thermal == thermal &&
      other.sdkInt == sdkInt &&
      other.batteryPercent == batteryPercent &&
      other.storageFreeBytes == storageFreeBytes &&
      other.storageTotalBytes == storageTotalBytes &&
      other.charging == charging &&
      other.plugged == plugged;

  @override
  int get hashCode => Object.hash(
    model,
    manufacturer,
    os,
    thermal,
    sdkInt,
    batteryPercent,
    storageFreeBytes,
    storageTotalBytes,
    charging,
  );
}

class BatteryStatus {
  const BatteryStatus(this.percent, this.charging, this.plugged);
  final int? percent;
  final bool? charging, plugged;
}

abstract class DeviceStatusSource {
  Future<DeviceStatus> read();

  /// Pushed immediately on battery/plug changes.
  Stream<BatteryStatus> get batteryEvents;
}

String batteryLabel(DeviceStatus d) => switch ((d.charging, d.plugged)) {
  (true, _) => 'Charging',
  (_, true) => 'Plugged in, not charging',
  (false, _) => 'On battery',
  _ => 'Unavailable',
};

class ChannelDeviceStatusSource implements DeviceStatusSource {
  static const _channel = MethodChannel('dev.localvision/device');
  static const _events = EventChannel('dev.localvision/device_events');

  @override
  Stream<BatteryStatus> get batteryEvents => _events
      .receiveBroadcastStream()
      .map((e) => _battery(Map<String, Object?>.from(e as Map)))
      .handleError((Object _) {}); // iOS/no plugin: stream stays silent

  static BatteryStatus _battery(Map<String, Object?> m) => BatteryStatus(
    m['batteryPercent'] as int?,
    m['charging'] as bool?,
    m['plugged'] as bool?,
  );

  @override
  Future<DeviceStatus> read() async {
    try {
      final m = await _channel.invokeMapMethod<String, Object?>(
        'getDeviceStatus',
      );
      if (m == null) return const DeviceStatus.unknown();
      final release = m['osRelease'] as String?;
      return DeviceStatus(
        model: m['model'] as String?,
        manufacturer: m['manufacturer'] as String?,
        os: release == null ? null : 'Android $release',
        sdkInt: m['sdkInt'] as int?,
        batteryPercent: m['batteryPercent'] as int?,
        charging: m['charging'] as bool?,
        plugged: m['plugged'] as bool?,
        thermal: m['thermal'] as String?,
        storageFreeBytes: m['storageFreeBytes'] as int?,
        storageTotalBytes: m['storageTotalBytes'] as int?,
      );
    } on MissingPluginException {
      return const DeviceStatus.unknown(); // iOS side arrives in a later slice
    } on PlatformException {
      return const DeviceStatus.unknown();
    }
  }
}

/// Cellular/data interfaces are never advertised: their addresses are CGNAT
/// or public, change every session and would force a new certificate.
bool isCellularInterface(String name) {
  final n = name.toLowerCase();
  return const [
    'rmnet',
    'v4-rmnet',
    'ccmni',
    'pdp',
    'wwan',
    'ppp',
    'seth',
    'usb_rmnet',
  ].any(n.startsWith);
}

/// Pure selection so it is testable: non-loopback, non-cellular, reachable IPv4.
List<String> selectLanAddresses(
  Map<String, List<InternetAddress>> byInterface,
) {
  final out = <String>{
    for (final e in byInterface.entries)
      if (!isCellularInterface(e.key))
        for (final a in e.value)
          if (a.type == InternetAddressType.IPv4 &&
              !a.isLoopback &&
              isAllowedPeer(a))
            a.address,
  }.toList()..sort();
  return out;
}

/// IPv4 addresses the server can be reached on (Wi-Fi, hotspot, ethernet).
Future<List<String>> localIpv4Addresses() async {
  try {
    final ifs = await NetworkInterface.list(type: InternetAddressType.IPv4);
    return selectLanAddresses({for (final i in ifs) i.name: i.addresses});
  } on SocketException {
    return const [];
  }
}

/// Contract strings: granted|denied|permanently_denied|restricted|limited|not_requested
abstract class PermissionPort {
  Future<String> status(Permission p);
  Future<String> request(Permission p);
  Future<void> openSettings();
}

String mapPermission(PermissionStatus s) => switch (s) {
  PermissionStatus.granted => 'granted',
  PermissionStatus.denied => 'denied',
  PermissionStatus.permanentlyDenied => 'permanently_denied',
  PermissionStatus.restricted => 'restricted',
  PermissionStatus.limited => 'limited',
  PermissionStatus.provisional => 'granted',
};

class HandlerPermissionPort implements PermissionPort {
  final _asked = <Permission>{};

  @override
  Future<String> status(Permission p) async {
    final s = await p.status;
    // Android reports "denied" before the first request; we cannot tell it
    // apart from a real denial after an app restart, so only this run is tracked.
    if (s == PermissionStatus.denied && !_asked.contains(p))
      return 'not_requested';
    return mapPermission(s);
  }

  @override
  Future<String> request(Permission p) async {
    _asked.add(p);
    return mapPermission(await p.request());
  }

  @override
  Future<void> openSettings() => openAppSettings();
}
