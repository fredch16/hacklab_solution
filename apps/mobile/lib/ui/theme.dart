import 'package:material_ui/material_ui.dart';

/// Quiet Precision tokens (ui/design-tokens.json).
class Tok {
  static const ink950 = Color(0xFF080D14);
  static const ink900 = Color(0xFF101824);
  static const ink800 = Color(0xFF172334);
  static const ink700 = Color(0xFF253549);
  static const textPrimary = Color(0xFFEAF2F9);
  static const textSecondary = Color(0xFFA2B4C5);
  static const textTertiary = Color(0xFF71859B);
  static const teal = Color(0xFF5EDBC8);
  static const blue = Color(0xFF79B9F2);
  static const warning = Color(0xFFF4BD6A);
  static const danger = Color(0xFFF27B7B);
  static const recordRed = Color(0xFFFF6873);
  static const success = Color(0xFF73DCB0);
}

const tabular = [FontFeature.tabularFigures()];

ThemeData buildTheme() {
  const scheme = ColorScheme.dark(
    surface: Tok.ink900,
    primary: Tok.teal,
    onPrimary: Tok.ink950,
    secondary: Tok.blue,
    error: Tok.danger,
    onSurface: Tok.textPrimary,
    outline: Tok.ink700,
  );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: Tok.ink950,
    canvasColor: Tok.ink950,
    textTheme: ThemeData.dark().textTheme.apply(
      bodyColor: Tok.textPrimary,
      displayColor: Tok.textPrimary,
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: Tok.ink900,
      indicatorColor: Tok.ink700,
      labelTextStyle: WidgetStatePropertyAll(
        const TextStyle(fontSize: 12, color: Tok.textSecondary),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: const Size(48, 48),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(11)),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        minimumSize: const Size(48, 48),
        foregroundColor: Tok.textPrimary,
        side: const BorderSide(color: Tok.ink700),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(11)),
      ),
    ),
    dialogTheme: const DialogThemeData(backgroundColor: Tok.ink800),
  );
}
