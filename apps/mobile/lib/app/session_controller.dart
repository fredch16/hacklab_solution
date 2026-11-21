import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:wakelock_plus/wakelock_plus.dart';

import '../features/camera/capture_controller.dart';
import '../features/recording/recording_controller.dart';
import '../features/connectivity/local_server.dart';
import '../features/connectivity/pairing.dart';
import '../features/connectivity/signaling.dart';
import '../features/connectivity/snapshot.dart';
import '../platform/device_status.dart';

/// Contract `app` values (requesting_permissions is transient).
enum AppPhase { idle, requestingPermissions, ready, active, interrupted, error }

extension on AppPhase {
  String get wire => switch (this) {
    AppPhase.requestingPermissions => 'requesting_permissions',
    _ => name,
  };
}

typedef LocalServerFactory = LocalServer Function(
  SnapshotBuilder payload,
  void Function(String raw) onMessage,
);

/// Small explicit state machine:
///   idle --init--> ready --start--> active <--paused/resumed--> interrupted
///   active|interrupted --end--> ready;  start failure --> error
class SessionController extends ChangeNotifier with WidgetsBindingObserver {
  SessionController({
    required this.pairing,
    required this.capture,
    required this.recording,
    required LocalServerFactory serverFactory,
    required this.deviceSource,
    required this.permissionPort,
    Future<List<String>> Function()? addresses,
    Future<void> Function(bool)? wakelock,
    Future<String?> Function()? appVersion,
    this.pollInterval = const Duration(seconds: 5),
    DateTime Function()? clock,
  }) : _addresses = addresses ?? localIpv4Addresses,
       _wakelock = wakelock ?? ((b) => WakelockPlus.toggle(enable: b)),
       _appVersionLoader = appVersion ?? _packageVersion,
       _now = clock ?? DateTime.now {
    relay = SignalingRelay(
      port: capture,
      recording: recording,
      send: (type, payload, {requestId}) =>
          server.sendMessage(type, payload, requestId: requestId),
    );
    capture.sender = (type, payload, {requestId}) =>
        server.sendMessage(type, payload, requestId: requestId);
    capture.addListener(_publish);
    recording.addListener(_publish);
    server = serverFactory(snapshotPayload, (raw) => relay.handle(raw));
    _serverSub = server.events.listen((_) {
      // Viewer gone (revoked, replaced, tab closed): nobody to send video to.
      if (capture.streamState != 'idle' && !server.viewerInfo.connected) {
        capture.stopStream('browser_stop');
      }
      notifyListeners();
    });
    _batterySub = deviceSource.batteryEvents.listen((b) {
      device = device.withBattery(b);
      _publish();
    }, onError: (Object _) {});
  }

  final PairingService pairing;
  final CaptureController capture;
  final RecordingController recording;
  late final SignalingRelay relay;
  final DeviceStatusSource deviceSource;
  final PermissionPort permissionPort;
  final Duration pollInterval;
  final Future<List<String>> Function() _addresses;
  final Future<void> Function(bool) _wakelock;
  final Future<String?> Function() _appVersionLoader;
  final DateTime Function() _now;
  late final LocalServer server;
  late final StreamSubscription<ServerEvent> _serverSub;
  late final StreamSubscription<BatteryStatus> _batterySub;

  AppPhase phase = AppPhase.idle;
  String? errorMessage;
  DateTime? sessionStartedUtc;
  bool keepScreenOn = false;
  DeviceStatus device = const DeviceStatus.unknown();
  List<String> addresses = const [];
  bool addressesNotCovered = false;
  Map<String, String> permissions = {
    'camera': 'not_requested',
    'microphone': 'not_requested',
  };
  String? appVersion;
  Timer? _poll;
  var _busy = false;

  bool get sessionRunning =>
      phase == AppPhase.active || phase == AppPhase.interrupted;

  static Future<String?> _packageVersion() async {
    try {
      final i = await PackageInfo.fromPlatform();
      return '${i.version}+${i.buildNumber}';
    } catch (_) {
      return null;
    }
  }

  Map<String, Object?> snapshotPayload() => buildSnapshotPayload(
    app: phase.wire,
    sessionStartedUtc: sessionStartedUtc,
    keepScreenOn: keepScreenOn,
    device: device,
    appVersion: appVersion,
    permissions: permissions,
    addresses: addresses,
    port: server.port,
    certSha256: server.certSha256,
    viewer: server.viewerInfo,
    camera: capture.cameraJson(),
    stream: capture.streamJson(),
    recording: recording.recording,
    recordings: recording.recordings,
  );

  Future<void> init() async {
    WidgetsBinding.instance.addObserver(this);
    appVersion = await _appVersionLoader();
    await recording.refresh();
    await _refresh();
    if (phase == AppPhase.idle) phase = AppPhase.ready;
    notifyListeners();
  }

  Future<void> startSession() async {
    if (sessionRunning || _busy) return;
    _busy = true;
    try {
      errorMessage = null;
      await _refresh();
      await server.start();
      addressesNotCovered = false;
      try {
        await _wakelock(true);
        keepScreenOn = true;
      } catch (_) {
        keepScreenOn = false;
      }
      sessionStartedUtc = _now().toUtc();
      phase = AppPhase.active;
      _poll = Timer.periodic(pollInterval, (_) => _refresh());
      await _requestCameraPermission();
    } catch (e) {
      phase = AppPhase.error;
      errorMessage = 'Could not start the local server: ${e.runtimeType}';
      await server.stop();
    } finally {
      _busy = false;
      _publish();
    }
  }

  /// Session start only asks for permissions (camera and microphone, so remote
  /// Start video / Record need no phone tap); the camera starts on demand.
  Future<void> _requestCameraPermission() async {
    if (permissions['camera'] != 'granted') {
      await requestPermission(Permission.camera);
    }
    if (permissions['microphone'] != 'granted') {
      await requestPermission(Permission.microphone);
    }
    if (permissions['camera'] == 'granted') {
      capture.permissionGranted();
    } else {
      capture.denyPermission();
    }
  }

  /// "Grant camera" action: ask, or send the user to settings when blocked.
  Future<void> grantCamera() async {
    final s = permissions['camera'];
    if (s == 'permanently_denied' || s == 'restricted') {
      await openSettings();
    } else {
      await requestPermission(Permission.camera);
    }
  }

  Future<void> endSession() async {
    if (!sessionRunning) return;
    _poll?.cancel();
    await recording.end();
    await capture.end();
    await server.stop();
    try {
      await _wakelock(false);
    } catch (_) {}
    keepScreenOn = false;
    sessionStartedUtc = null;
    phase = AppPhase.ready;
    notifyListeners();
  }

  Future<void> requestPermission(Permission p) async {
    final before = phase;
    phase = AppPhase.requestingPermissions;
    _publish();
    await permissionPort.request(p);
    phase = before;
    await _refresh();
  }

  Future<void> openSettings() => permissionPort.openSettings();

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused && phase == AppPhase.active) {
      phase = AppPhase.interrupted;
      capture.setInterrupted(true);
      _publish();
    } else if (state == AppLifecycleState.resumed) {
      if (phase == AppPhase.interrupted) phase = AppPhase.active;
      capture.setInterrupted(false);
      _refresh();
    }
  }

  Future<void> _refresh() async {
    final d = await deviceSource.read();
    final a = await _addresses();
    final p = {
      'camera': await permissionPort.status(Permission.camera),
      'microphone': await permissionPort.status(Permission.microphone),
    };
    device = d;
    addresses = a;
    permissions = p;
    addressesNotCovered =
        server.running && !server.certAddresses.toSet().containsAll(a);
    _publish();
    // Permission granted later (system settings or the Grant camera button).
    if (sessionRunning &&
        capture.permissionDenied &&
        p['camera'] == 'granted') {
      capture.permissionGranted();
    }
  }

  void _publish() {
    server.notifyStateChanged();
    notifyListeners();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _poll?.cancel();
    _serverSub.cancel();
    _batterySub.cancel();
    capture.removeListener(_publish);
    recording.removeListener(_publish);
    server.dispose();
    super.dispose();
  }
}
