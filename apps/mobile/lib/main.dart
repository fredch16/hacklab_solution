import 'package:flutter/services.dart' show rootBundle;
import 'package:material_ui/material_ui.dart';
import 'package:path_provider/path_provider.dart';

import 'app/session_controller.dart';
import 'features/camera/capture_controller.dart';
import 'features/connectivity/certificate.dart';
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
  final capture = CaptureController(ChannelCameraPlatform());
  final certs = CertificateService(dir);
  final controller = SessionController(
    pairing: pairing,
    capture: capture,
    deviceSource: ChannelDeviceStatusSource(),
    permissionPort: HandlerPermissionPort(),
    serverFactory: (payload, onMessage) => LocalServer(
      pairing: pairing,
      certs: certs,
      addresses: localIpv4Addresses,
      snapshotPayload: payload,
      onMessage: onMessage,
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
    title: 'Local Vision',
    debugShowCheckedModeBanner: false,
    theme: buildTheme(),
    home: HomeShell(controller: controller),
  );
}
