import 'package:material_ui/material_ui.dart';

/// Peter tokens: the studio navy, teal for live, orange from the app icon (same palette as the web dashboard).
class Tok {
  static const ink950 = Color(0xFF070B18);
  static const ink900 = Color(0xFF0E1630);
  static const ink800 = Color(0xFF16213F);
  static const ink700 = Color(0xFF253256);
  static const textPrimary = Color(0xFFEEF2FB);
  static const textSecondary = Color(0xFFA9B6D3);
  static const textTertiary = Color(0xFF7484A8);
  static const teal = Color(0xFF5EEAD4);
  static const blue = Color(0xFF7AB8FF);
  static const orange = Color(0xFFFF8A3D);
  static const warning = Color(0xFFFFC35A);
  static const danger = Color(0xFFFF6B6B);
  static const recordRed = Color(0xFFFF4D5E);
  static const success = Color(0xFF6EE7B7);
}

const tabular = [FontFeature.tabularFigures()];

ThemeData buildTheme() {
  const scheme = ColorScheme.dark(
    surface: Tok.ink900,
    primary: Tok.teal,
    onPrimary: Tok.ink950,
    secondary: Tok.orange,
    tertiary: Tok.blue,
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
    cardTheme: CardThemeData(
      color: Tok.ink900,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(20),
        side: const BorderSide(color: Tok.ink700),
      ),
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: Tok.ink900,
      indicatorColor: Tok.orange.withValues(alpha: 0.22),
      labelTextStyle: WidgetStatePropertyAll(
        const TextStyle(fontSize: 12, color: Tok.textSecondary),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: const Size(48, 48),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
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
