import 'package:material_ui/material_ui.dart';

import 'theme.dart';

const unavailable = 'Unavailable';

String formatBytes(int? b) {
  if (b == null) return unavailable;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  var v = b.toDouble();
  var i = 0;
  while (v >= 1000 && i < u.length - 1) {
    v /= 1000;
    i++;
  }
  return '${v.toStringAsFixed(i < 2 ? 0 : 1)} ${u[i]}';
}

String two(int n) => n.toString().padLeft(2, '0');

String formatTime(DateTime? t) {
  if (t == null) return unavailable;
  final l = t.toLocal();
  return '${l.year}-${two(l.month)}-${two(l.day)} ${two(l.hour)}:${two(l.minute)}';
}

class AppCard extends StatelessWidget {
  const AppCard({super.key, this.title, required this.child});
  final String? title;
  final Widget child;

  @override
  Widget build(BuildContext context) => Container(
    width: double.infinity,
    padding: const EdgeInsets.all(16),
    decoration: BoxDecoration(
      color: Tok.ink900,
      borderRadius: BorderRadius.circular(16),
      border: Border.all(color: Tok.ink700),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (title != null) ...[
          Semantics(
            header: true,
            child: Text(
              title!.toUpperCase(),
              style: const TextStyle(
                fontSize: 12,
                letterSpacing: 1.0,
                color: Tok.textSecondary,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
          const SizedBox(height: 12),
        ],
        child,
      ],
    ),
  );
}

class StatusChip extends StatelessWidget {
  const StatusChip(
    this.label, {
    super.key,
    this.color = Tok.textSecondary,
    this.icon,
  });
  final String label;
  final Color color;
  final IconData? icon;

  @override
  Widget build(BuildContext context) => Container(
    constraints: const BoxConstraints(minHeight: 32),
    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
    decoration: BoxDecoration(
      color: Tok.ink800,
      borderRadius: BorderRadius.circular(8),
      border: Border.all(color: Tok.ink700),
    ),
    child: Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon ?? Icons.circle, size: icon == null ? 8 : 16, color: color),
        const SizedBox(width: 6),
        Flexible(
          child: Text(
            label,
            style: TextStyle(fontSize: 12, color: Tok.textPrimary),
          ),
        ),
      ],
    ),
  );
}

class ValueRow extends StatelessWidget {
  const ValueRow(this.label, this.value, {super.key, this.mono = true});
  final String label;
  final String? value;
  final bool mono;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 6),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          flex: 4,
          child: Text(
            label,
            style: const TextStyle(color: Tok.textSecondary, fontSize: 14),
          ),
        ),
        Expanded(
          flex: 6,
          child: Text(
            value ?? unavailable,
            textAlign: TextAlign.end,
            style: TextStyle(
              fontSize: 14,
              color: value == null ? Tok.textTertiary : Tok.textPrimary,
              fontFeatures: mono ? tabular : null,
            ),
          ),
        ),
      ],
    ),
  );
}

/// One column on phones in portrait, two columns when wide (landscape).
class ResponsiveColumns extends StatelessWidget {
  const ResponsiveColumns(this.children, {super.key});
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, c) {
      Widget col(List<Widget> items) => Column(
        children: [
          for (final w in items)
            Padding(padding: const EdgeInsets.only(bottom: 12), child: w),
        ],
      );
      if (c.maxWidth < 640) return col(children);
      final left = [for (var i = 0; i < children.length; i += 2) children[i]];
      final right = [for (var i = 1; i < children.length; i += 2) children[i]];
      return Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(child: col(left)),
          const SizedBox(width: 12),
          Expanded(child: col(right)),
        ],
      );
    },
  );
}

class PageScaffold extends StatelessWidget {
  const PageScaffold({super.key, required this.children});
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => SafeArea(
    child: SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: ResponsiveColumns(children),
    ),
  );
}
