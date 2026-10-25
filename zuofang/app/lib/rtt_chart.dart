import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'app_theme.dart';

/// RTT 折线图。
///
/// 只画最近 [samples] 个点，横轴滚动。低于 [goodMs] 走主色，低于 [warnMs]
/// 走警告色，再高走危险色 —— 让「抖动」在余光里就能看出来，不必读数字。
class RttChart extends StatelessWidget {
  const RttChart({
    super.key,
    required this.values,
    this.capacity = 60,
    this.goodMs = 80,
    this.warnMs = 200,
    this.height = 96,
  });

  /// 采样序列（毫秒）。空值用 -1 表示丢包。
  final List<int> values;
  final int capacity;
  final int goodMs;
  final int warnMs;
  final double height;

  @override
  Widget build(BuildContext context) {
    final pts = values.length > capacity
        ? values.sublist(values.length - capacity)
        : values;
    return SizedBox(
      height: height,
      child: CustomPaint(
        painter: _RttPainter(
          values: pts,
          capacity: capacity,
          goodMs: goodMs,
          warnMs: warnMs,
        ),
        child: const SizedBox.expand(),
      ),
    );
  }
}

class _RttPainter extends CustomPainter {
  _RttPainter({
    required this.values,
    required this.capacity,
    required this.goodMs,
    required this.warnMs,
  });

  final List<int> values;
  final int capacity;
  final int goodMs;
  final int warnMs;

  @override
  void paint(Canvas canvas, Size size) {
    // 网格
    final grid = Paint()
      ..color = ZfTheme.line.withOpacity(0.55)
      ..strokeWidth = 0.7;
    for (var i = 1; i < 4; i++) {
      final y = size.height * i / 4;
      canvas.drawLine(Offset(0, y), Offset(size.width, y), grid);
    }
    for (var i = 1; i < 6; i++) {
      final x = size.width * i / 6;
      canvas.drawLine(Offset(x, 0), Offset(x, size.height), grid);
    }

    final valid = values.where((v) => v >= 0).toList();
    if (valid.isEmpty) {
      _text(canvas, size, '等待采样', ZfTheme.textDim);
      return;
    }

    // 纵轴上限：至少 100ms，留 15% 余量，避免满分线贴顶
    final peak = valid.reduce(math.max);
    final top = math.max(100, (peak * 1.15).ceil());

    final stepX = size.width / math.max(1, capacity - 1);
    // 点位从右往左排，最新一帧贴右边，看起来像示波器推进
    final offsetX = size.width - (values.length - 1) * stepX;

    Offset at(int i) {
      final v = values[i].clamp(0, top);
      final y = size.height - (v / top) * (size.height - 4) - 2;
      return Offset(offsetX + i * stepX, y);
    }

    // 面积
    final area = Path();
    var started = false;
    for (var i = 0; i < values.length; i++) {
      if (values[i] < 0) continue;
      final p = at(i);
      if (!started) {
        area.moveTo(p.dx, size.height);
        area.lineTo(p.dx, p.dy);
        started = true;
      } else {
        area.lineTo(p.dx, p.dy);
      }
    }
    if (started) {
      area.lineTo(at(values.length - 1).dx, size.height);
      area.close();
      canvas.drawPath(
        area,
        Paint()
          ..shader = LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [
              _colorFor(peak).withOpacity(0.26),
              _colorFor(peak).withOpacity(0.02),
            ],
          ).createShader(Offset.zero & size),
      );
    }

    // 折线（逐段取色，方便一眼看出哪段在飙）
    final line = Paint()
      ..strokeWidth = 1.6
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round;
    for (var i = 1; i < values.length; i++) {
      if (values[i] < 0 || values[i - 1] < 0) continue;
      line.color = _colorFor(values[i]);
      canvas.drawLine(at(i - 1), at(i), line);
    }

    // 最新点
    final last = values.last;
    if (last >= 0) {
      final p = at(values.length - 1);
      final c = _colorFor(last);
      canvas.drawCircle(p, 3.4, Paint()..color = c.withOpacity(0.22));
      canvas.drawCircle(p, 1.8, Paint()..color = c);
    }

    _text(canvas, size, '$peak ms', ZfTheme.textDim, align: _Align.topLeft);
  }

  Color _colorFor(int ms) {
    if (ms < 0) return ZfTheme.textDim;
    if (ms <= goodMs) return ZfTheme.accent;
    if (ms <= warnMs) return ZfTheme.warn;
    return ZfTheme.danger;
  }

  void _text(Canvas canvas, Size size, String s, Color c,
      {_Align align = _Align.center}) {
    final tp = TextPainter(
      text: TextSpan(
        text: s,
        style: TextStyle(fontFamily: 'monospace', fontSize: 9.5, color: c),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    final dx = align == _Align.center ? (size.width - tp.width) / 2 : 2.0;
    final dy = align == _Align.center ? (size.height - tp.height) / 2 : 2.0;
    tp.paint(canvas, Offset(dx, dy));
  }

  @override
  bool shouldRepaint(covariant _RttPainter old) =>
      old.values.length != values.length ||
      (values.isNotEmpty && old.values.isNotEmpty && old.values.last != values.last) ||
      old.capacity != capacity;
}

enum _Align { center, topLeft }
