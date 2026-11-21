import 'dart:async';

import 'package:material_ui/material_ui.dart';

import '../platform/device_status.dart';
import '../app/session_controller.dart';
import 'record_card.dart';
import 'session_button.dart';
import 'theme.dart';
import 'widgets.dart';

class CapturePage extends StatelessWidget {
  const CapturePage({super.key, required this.controller});
  final SessionController controller;

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([controller, controller.recording]),
    builder: (context, _) {
      final c = controller;
      final d = c.device;
      final v = c.server.viewerInfo;
      return PageScaffold(
        children: [
          AppCard(
            title: 'Camera',
            child: Column(
              children: [
                CameraViewport(controller: c),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    StatusChip(
                      webrtcChipLabel(c),
                      icon: Icons.cast,
                      color: c.capture.streamState == 'connected'
                          ? Tok.teal
                          : Tok.textSecondary,
                    ),
                    const StatusChip(
                      'Local master · Idle',
                      icon: Icons.save_outlined,
                    ),
                  ],
                ),
              ],
            ),
          ),
          AppCard(
            title: 'Session',
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  c.sessionRunning
                      ? 'Local dashboard is served on this network.'
                      : 'Starting a session turns on the camera preview (it asks for camera permission) and serves the local dashboard. Video goes to the browser only when you click Start video there.',
                  style: const TextStyle(
                    color: Tok.textSecondary,
                    fontSize: 14,
                  ),
                ),
                const SizedBox(height: 12),
                SessionButton(controller: c),
              ],
            ),
          ),
          RecordCard(controller: c),
          AppCard(
            title: 'Device health',
            child: Column(
              children: [
                ValueRow(
                  'Battery',
                  d.batteryPercent == null
                      ? null
                      : '${d.batteryPercent}% · ${batteryLabel(d)}',
                ),
                ValueRow('Thermal', d.thermal, mono: false),
                ValueRow(
                  'Storage free',
                  d.storageFreeBytes == null
                      ? null
                      : formatBytes(d.storageFreeBytes),
                ),
              ],
            ),
          ),
          AppCard(
            title: 'Viewer',
            child: Column(
              children: [
                ValueRow(
                  'Browser',
                  v.paired
                      ? (v.connected
                            ? 'Paired · connected'
                            : 'Paired · not connected')
                      : 'No viewer paired',
                  mono: false,
                ),
                if (v.paired)
                  ValueRow('Paired since', formatTime(v.pairedSince)),
              ],
            ),
          ),
        ],
      );
    },
  );
}

String webrtcChipLabel(SessionController c) {
  final cap = c.capture;
  switch (cap.streamState) {
    case 'connecting':
      return 'WebRTC · Connecting';
    case 'connected':
      final st = cap.streamStats;
      if (st == null) return 'WebRTC · Connected';
      final parts = <String>[
        [
          st['codec'],
          if (st['width'] != null && st['height'] != null)
            '${st['width']}×${st['height']}',
        ].whereType<Object>().join(' '),
        if (st['fps'] is num) '${(st['fps'] as num).toStringAsFixed(1)} fps',
        if (st['bitrate_bps'] is num)
          '${((st['bitrate_bps'] as num) / 1e6).toStringAsFixed(1)} Mbps',
      ].where((e) => e.isNotEmpty).toList();
      return 'WebRTC · ${parts.isEmpty ? 'Connected' : parts.join(' · ')}';
    case 'failed':
      return 'WebRTC · Failed (${cap.streamError ?? 'error'})';
    default:
      return c.server.viewerInfo.paired
          ? 'WebRTC · Idle'
          : 'WebRTC · Idle (no viewer)';
  }
}

class CameraViewport extends StatefulWidget {
  const CameraViewport({super.key, required this.controller});
  final SessionController controller;

  @override
  State<CameraViewport> createState() => _CameraViewportState();
}

class _CameraViewportState extends State<CameraViewport> {
  Offset? _ring; // local tap position of the focus ring
  Timer? _ringTimer;

  @override
  void dispose() {
    _ringTimer?.cancel();
    super.dispose();
  }

  /// Displayed (rotated) normalised point -> preview buffer normalised point.
  static (double, double) toBuffer(double u, double v, int rotation) =>
      switch (rotation % 360) {
        90 => (v, 1 - u),
        180 => (1 - u, 1 - v),
        270 => (1 - v, u),
        _ => (u, v),
      };

  void _tap(TapDownDetails d, Size size) {
    final cap = widget.controller.capture;
    final (bx, by) = toBuffer(
      (d.localPosition.dx / size.width).clamp(0.0, 1.0),
      (d.localPosition.dy / size.height).clamp(0.0, 1.0),
      cap.previewRotation,
    );
    cap.focus(bx, by);
    _ringTimer?.cancel();
    setState(() => _ring = d.localPosition);
    _ringTimer = Timer(const Duration(milliseconds: 1200), () {
      if (mounted) setState(() => _ring = null);
    });
  }

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    final cap = controller.capture;
    final st = cap.state;
    Widget? texture;
    if ((st == 'previewing' || st == 'transmitting') &&
        cap.textureId != null &&
        cap.previewWidth != null) {
      final w = cap.previewWidth!.toDouble();
      final h = cap.previewHeight!.toDouble();
      final turns = (cap.previewRotation ~/ 90) % 4;
      final quarter = turns.isOdd;
      texture = AspectRatio(
        aspectRatio: quarter ? h / w : w / h,
        child: LayoutBuilder(
          builder: (context, c) => GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTapDown: (d) => _tap(d, Size(c.maxWidth, c.maxHeight)),
            child: Stack(
              fit: StackFit.expand,
              children: [
                FittedBox(
                  fit: BoxFit.cover,
                  clipBehavior: Clip.hardEdge,
                  child: RotatedBox(
                    quarterTurns: turns,
                    child: SizedBox(
                      width: w,
                      height: h,
                      child: Texture(textureId: cap.textureId!),
                    ),
                  ),
                ),
                if (_ring != null)
                  Positioned(
                    left: _ring!.dx - 32,
                    top: _ring!.dy - 32,
                    child: IgnorePointer(
                      child: Container(
                        width: 64,
                        height: 64,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          border: Border.all(color: Tok.teal, width: 2),
                        ),
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ),
      );
    }
    final running = controller.sessionRunning;
    final msg = switch (st) {
      'not_started' =>
        running
            ? 'Camera off · starts when the PC clicks Start video'
            : 'Camera off · start a session first',
      'initializing' => 'Camera · Starting…',
      'interrupted' => 'Camera · Interrupted — the app is in the background',
      'error' => switch (cap.error) {
        'PERMISSION_CAMERA' => 'Camera · Permission needed',
        'DEVICE_IN_USE' => 'Camera · In use by another app',
        'CAPABILITY_UNSUPPORTED' => 'Camera · Not supported on this device',
        _ => 'Camera · No frames from the camera',
      },
      _ => 'Camera · Starting…',
    };
    // Centered, not cropped: the operator must see the camera's full field of view.
    return Center(
      child: ClipRRect(
        borderRadius: BorderRadius.circular(18),
        child: Container(
          constraints: const BoxConstraints(maxHeight: 420),
          decoration: BoxDecoration(
            color: Colors.black,
            border: Border.all(color: Tok.ink700),
            borderRadius: BorderRadius.circular(18),
          ),
          child:
              texture ??
              AspectRatio(
                aspectRatio: 16 / 9,
                child: Center(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          msg,
                          textAlign: TextAlign.center,
                          style: const TextStyle(
                            color: Tok.textSecondary,
                            fontSize: 14,
                          ),
                        ),
                        if (st == 'error' &&
                            running &&
                            cap.permissionDenied) ...[
                          const SizedBox(height: 12),
                          FilledButton(
                            onPressed: controller.grantCamera,
                            child: Text(
                              controller.permissions['camera'] ==
                                      'permanently_denied'
                                  ? 'Open settings'
                                  : 'Grant camera',
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
                ),
              ),
        ),
      ),
    );
  }
}
