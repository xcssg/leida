import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

/// 深色 + 青绿荧光的技术面板风格。
class ZfTheme {
  static const bg = Color(0xFF07090C);
  static const panel = Color(0xFF10151B);
  static const panelAlt = Color(0xFF161D25);
  static const line = Color(0xFF243039);
  static const accent = Color(0xFF35E0B0);
  static const accentDim = Color(0xFF1B8F73);
  static const warn = Color(0xFFF2B45A);
  static const danger = Color(0xFFFF5C72);
  static const text = Color(0xFFE6EEF4);
  static const textDim = Color(0xFF7E8F9C);

  static ThemeData build() {
    final base = ThemeData.dark(useMaterial3: true);
    return base.copyWith(
      scaffoldBackgroundColor: bg,
      colorScheme: base.colorScheme.copyWith(
        primary: accent,
        secondary: accentDim,
        surface: panel,
        error: danger,
        onSurface: text,
      ),
      textTheme: base.textTheme.apply(
        bodyColor: text,
        displayColor: text,
        fontFamily: 'monospace',
      ),
      appBarTheme: const AppBarTheme(
        backgroundColor: bg,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(
          fontFamily: 'monospace',
          fontSize: 16,
          letterSpacing: 2,
          fontWeight: FontWeight.w600,
          color: text,
        ),
      ),
      dividerTheme: const DividerThemeData(color: line, thickness: 1, space: 1),
      snackBarTheme: const SnackBarThemeData(
        backgroundColor: panelAlt,
        contentTextStyle: TextStyle(color: text, fontFamily: 'monospace'),
        behavior: SnackBarBehavior.floating,
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: panelAlt,
        isDense: true,
        contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
        hintStyle: const TextStyle(color: textDim, fontFamily: 'monospace'),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(6),
          borderSide: const BorderSide(color: line),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(6),
          borderSide: const BorderSide(color: accent),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(6),
          borderSide: const BorderSide(color: danger),
        ),
        focusedErrorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(6),
          borderSide: const BorderSide(color: danger),
        ),
      ),
      textSelectionTheme: const TextSelectionThemeData(
        cursorColor: accent,
        selectionColor: Color(0x5535E0B0),
        selectionHandleColor: accent,
      ),
    );
  }
}

/// 通用滚动行为：桌面端也能拖拽。
class ZfScrollBehavior extends MaterialScrollBehavior {
  const ZfScrollBehavior();

  @override
  Set<PointerDeviceKind> get dragDevices => const {
        PointerDeviceKind.touch,
        PointerDeviceKind.mouse,
        PointerDeviceKind.trackpad,
        PointerDeviceKind.stylus,
      };
}
