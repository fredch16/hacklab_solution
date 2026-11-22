import 'package:flutter/services.dart';
import 'package:material_ui/material_ui.dart';
import 'package:path_provider/path_provider.dart';

import 'app/session_controller.dart';
import 'features/camera/capture_controller.dart';
import 'features/connectivity/certificate.dart';
import 'features/recording/recording_controller.dart';
import 'features/voice/voice_controller.dart';
import 'features/connectivity/local_server.dart';
import 'features/connectivity/pairing.dart';
import 'platform/camera_platform.dart';
import 'platform/device_status.dart';
import 'ui/home_shell.dart';
import 'ui/theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final dir = await getApplicationSupportDirectory();
  final pairing = PairingService();
  final cameraPlatform = ChannelCameraPlatform();
  final capture = CaptureController(cameraPlatform);
  final recording = RecordingController(cameraPlatform);
  final voice = VoiceController(cameraPlatform);
  final certs = CertificateService(dir);
  final controller = SessionController(
    pairing: pairing,
    capture: capture,
    recording: recording,
    voice: voice,
    deviceSource: ChannelDeviceStatusSource(),
    permissionPort: HandlerPermissionPort(),
    serverFactory: (payload, onMessage) => LocalServer(
      pairing: pairing,
      certs: certs,
      addresses: localIpv4Addresses,
      snapshotPayload: payload,
      onMessage: onMessage,
      advertise: (port, fingerprint) async {
        try {
          await const MethodChannel('dev.localvision/discovery').invokeMethod(
            'advertise',
            {'port': ?port, 'fingerprint': ?fingerprint},
          );
        } on MissingPluginException {
          // Manual setup is available where the native advertiser is absent.
        } on PlatformException {
          // Discovery failures must not interrupt camera or manual pairing.
        }
      },
      assetLoader: (name) async {
        try {
          return (await rootBundle.load('assets/dashboard/$name')).buffer
              .asUint8List();
        } catch (_) {
          return null;
        }
      },
    ),
  );
  await controller.init();
  runApp(LocalVisionApp(controller: controller));
}

class LocalVisionApp extends StatelessWidget {
  const LocalVisionApp({super.key, required this.controller});
  final SessionController controller;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'P.E.T.E.R.',
    debugShowCheckedModeBanner: false,
    theme: buildTheme(),
    home: HomeShell(controller: controller),
  );
}
