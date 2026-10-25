import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'app_theme.dart';
import 'zf_widgets.dart';

/// 节点状态。
enum RadarNodeState { idle, ok, warn, bad }

class RadarBlip {
  RadarBlip({
    required this.label,
    required this.distance, // 0.0 ~ 1.0，相对半径
    required this.angle, // 弧度
    required this.state,
  });

  final String label;
  final double distance;
  final double angle;
  final RadarNodeState state;

  Color get color => switch (state) {
        RadarNodeState.ok => ZfTheme.accent,
        RadarNodeState.warn => ZfTheme.warn,
        RadarNodeState.bad => ZfTheme.danger,
        RadarNodeState.idle => ZfTheme.textDim,
      };
}

/// 扫掠式雷达：中心是自己，一圈是链路上的关键节点。
class RadarView extends StatefulWidget {
  const RadarView({
    super.key,
    required this.blips,
    this.size = 260,
    this.caption,
  });

  final List<RadarBlip> blips;
  final double size;
  final String? caption;

  @override
  State<RadarView> createState() => _RadarViewState();
}

class _RadarViewState extends State<RadarView> with SingleTickerProviderStateMixin {
  late final AnimationController _sweep = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 2600),
  )..repeat();

  @override
  void dispose() {
    _sweep.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        SizedBox(
          width: widget.size,
          height: widget.size,
          child: AnimatedBuilder(
            animation: _sweep,
            builder: (_, __) => CustomPaint(
              painter: _RadarPainter(
                sweep: _sweep.value,
                blips: widget.blips,
              ),
            ),
          ),
        ),
        if (widget.caption != null) ...[
          const SizedBox(height: 12),
          Text(
            widget.caption!,
            textAlign: TextAlign.center,
            style: const TextStyle(
              fontFamily: 'monospace',
              fontSize: 11,
              letterSpacing: 1.4,
              color: ZfTheme.textDim,
            ),
          ),
        ],
      ],
    );
  }
}

class _RadarPainter extends CustomPainter {
  _RadarPainter({required this.sweep, required this.blips});

  final double sweep;
  final List<RadarBlip> blips;

  @override
  void paint(Canvas canvas, Size size) {
    final c = Offset(size.width / 2, size.height / 2);
    final r = math.min(size.width, size.height) / 2 - 6;

    // 底盘
    canvas.drawCircle(
      c,
      r,
      Paint()..color = const Color(0xFF0C1218),
    );

    // 同心圆
    final ring = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 0.9
      ..color = ZfTheme.line;
    for (var i = 1; i <= 4; i++) {
      canvas.drawCircle(c, r * i / 4, ring);
    }

    // 十字线
    final axis = Paint()
      ..strokeWidth = 0.6
      ..color = ZfTheme.line;
    canvas.drawLine(Offset(c.dx - r, c.dy), Offset(c.dx + r, c.dy), axis);
    canvas.drawLine(Offset(c.dx, c.dy - r), Offset(c.dx, c.dy + r), axis);

    // 扫描扇形
    final start = sweep * 2 * math.pi - math.pi / 2;
    const span = math.pi / 3.2;
    final rect = Rect.fromCircle(center: c, radius: r);
    canvas.drawArc(
      rect,
      start - span,
      span,
      true,
      Paint()
        ..shader = SweepGradient(
          startAngle: start - span,
          endAngle: start,
          colors: const [Color(0x0035E0B0), Color(0x5535E0B0)],
          transform: GradientRotation(0),
        ).createShader(rect),
    );
    canvas.drawLine(
      c,
      Offset(c.dx + r * math.cos(start), c.dy + r * math.sin(start)),
      Paint()
        ..strokeWidth = 1.2
        ..color = ZfTheme.accent.withOpacity(0.7),
    );

    // 中心点
    canvas.drawCircle(c, 4, Paint()..color = ZfTheme.accent);
    canvas.drawCircle(
      c,
      9,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1
        ..color = ZfTheme.accent.withOpacity(0.35),
    );

    // 节点
    for (final b in blips) {
      final pos = Offset(
        c.dx + r * b.distance * math.cos(b.angle),
        c.dy + r * b.distance * math.sin(b.angle),
      );
      canvas.drawCircle(pos, 7, Paint()..color = b.color.withOpacity(0.16));
      canvas.drawCircle(pos, 3, Paint()..color = b.color);
      _label(canvas, b.label, pos, b.color);
    }
  }

  void _label(Canvas canvas, String text, Offset pos, Color color) {
    final tp = TextPainter(
      text: TextSpan(
        text: text,
        style: TextStyle(
          fontFamily: 'monospace',
          fontSize: 9.5,
          color: color,
          letterSpacing: 0.6,
        ),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    final dy = pos.dy > 40 ? pos.dy - 18 : pos.dy + 8;
    tp.paint(canvas, Offset(pos.dx - tp.width / 2, dy));
  }

  @override
  bool shouldRepaint(covariant _RadarPainter old) =>
      old.sweep != sweep || old.blips != blips;
}

/// 雷达区块（带外框与图例）。
class RadarPanel extends StatelessWidget {
  const RadarPanel({
    super.key,
    required this.blips,
    required this.legend,
    this.caption,
  });

  final List<RadarBlip> blips;
  final List<(String, Color)> legend;
  final String? caption;

  @override
  Widget build(BuildContext context) {
    return ZfPanel(
      title: '链路雷达',
      trailing: Wrap(
        spacing: 10,
        children: [
          for (final (name, color) in legend)
            Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(width: 5, height: 5, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
                const SizedBox(width: 4),
                Text(
                  name,
                  style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 9.5,
                    color: ZfTheme.textDim,
                  ),
                ),
              ],
            ),
        ],
      ),
      child: Center(
        child: RadarView(blips: blips, caption: caption),
      ),
    );
  }
}
