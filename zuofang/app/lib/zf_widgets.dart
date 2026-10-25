import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import 'app_theme.dart';

/// 带四角缺口的科技感面板。
class ZfPanel extends StatelessWidget {
  const ZfPanel({
    super.key,
    required this.child,
    this.title,
    this.trailing,
    this.padding = const EdgeInsets.all(16),
    this.accentColor,
  });

  final Widget child;
  final String? title;
  final Widget? trailing;
  final EdgeInsets padding;
  final Color? accentColor;

  @override
  Widget build(BuildContext context) {
    final c = accentColor ?? ZfTheme.accent;
    return Container(
      decoration: BoxDecoration(
        color: ZfTheme.panel,
        border: Border.all(color: ZfTheme.line),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Stack(
        children: [
          Positioned(left: 0, top: 0, child: _Corner(color: c, flipX: true, flipY: true)),
          Positioned(right: 0, top: 0, child: _Corner(color: c)),
          Positioned(left: 0, bottom: 0, child: _Corner(color: c, flipY: true)),
          Positioned(right: 0, bottom: 0, child: _Corner(color: c, flipX: true)),
          Padding(
            padding: padding,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                if (title != null) ...[
                  Row(
                    children: [
                      Container(width: 3, height: 12, color: c),
                      const SizedBox(width: 8),
                      Text(
                        title!.toUpperCase(),
                        style: TextStyle(
                          fontFamily: 'monospace',
                          fontSize: 11,
                          letterSpacing: 2.4,
                          fontWeight: FontWeight.w700,
                          color: c,
                        ),
                      ),
                      const Spacer(),
                      if (trailing != null) trailing!,
                    ],
                  ),
                  const SizedBox(height: 12),
                ],
                child,
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _Corner extends StatelessWidget {
  const _Corner({required this.color, this.flipX = false, this.flipY = false});

  final Color color;
  final bool flipX;
  final bool flipY;

  @override
  Widget build(BuildContext context) {
    return Transform.scale(
      scaleX: flipX ? -1 : 1,
      scaleY: flipY ? -1 : 1,
      child: CustomPaint(
        size: const Size(12, 12),
        painter: _CornerPainter(color),
      ),
    );
  }
}

class _CornerPainter extends CustomPainter {
  _CornerPainter(this.color);
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()
      ..color = color
      ..strokeWidth = 1.4
      ..style = PaintingStyle.stroke;
    final path = Path()
      ..moveTo(0, size.height)
      ..lineTo(0, 0)
      ..lineTo(size.width, 0);
    canvas.drawPath(path, p);
  }

  @override
  bool shouldRepaint(covariant _CornerPainter old) => old.color != color;
}

/// 单行指标：左标签右数值。
class ZfMetric extends StatelessWidget {
  const ZfMetric({
    super.key,
    required this.label,
    required this.value,
    this.unit,
    this.color,
    this.mono = true,
  });

  final String label;
  final String value;
  final String? unit;
  final Color? color;
  final bool mono;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Row(
        children: [
          Text(
            label,
            style: const TextStyle(
              fontFamily: 'monospace',
              fontSize: 12,
              color: ZfTheme.textDim,
              letterSpacing: 0.6,
            ),
          ),
          const Spacer(),
          Text(
            value,
            style: TextStyle(
              fontFamily: mono ? 'monospace' : null,
              fontSize: 13,
              fontWeight: FontWeight.w600,
              color: color ?? ZfTheme.text,
            ),
          ),
          if (unit != null) ...[
            const SizedBox(width: 4),
            Text(
              unit!,
              style: const TextStyle(
                fontFamily: 'monospace',
                fontSize: 10,
                color: ZfTheme.textDim,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

/// 指标墙：自适应多列。
class ZfMetrics extends StatelessWidget {
  const ZfMetrics({super.key, required this.items, this.columns = 2});

  final List<ZfMetric> items;
  final int columns;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, constraints) {
      final cols = constraints.maxWidth < 340 ? 1 : columns;
      final width = (constraints.maxWidth - (cols - 1) * 12) / cols;
      return Wrap(
        spacing: 12,
        runSpacing: 4,
        children: [
          for (final m in items) SizedBox(width: width, child: m),
        ],
      );
    });
  }
}

/// 段落块：标题 + 正文。
class ZfParagraph extends StatelessWidget {
  const ZfParagraph({super.key, required this.title, required this.body});

  final String title;
  final String body;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: const TextStyle(
              fontFamily: 'monospace',
              fontSize: 12,
              fontWeight: FontWeight.w700,
              color: ZfTheme.accent,
              letterSpacing: 0.8,
            ),
          ),
          const SizedBox(height: 5),
          Text(
            body,
            style: const TextStyle(
              fontSize: 12.5,
              height: 1.65,
              color: ZfTheme.textDim,
            ),
          ),
        ],
      ),
    );
  }
}

/// 合规声明卡片（原版 ZfDisclosure 的等价物）。
class ZfDisclosure extends StatelessWidget {
  const ZfDisclosure({super.key, required this.lines, this.accepted = false, this.onAccept});

  final List<String> lines;
  final bool accepted;
  final VoidCallback? onAccept;

  @override
  Widget build(BuildContext context) {
    return ZfPanel(
      title: '使用声明',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final l in lines)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Padding(
                    padding: EdgeInsets.only(top: 5, right: 8),
                    child: Icon(Icons.circle, size: 5, color: ZfTheme.accentDim),
                  ),
                  Expanded(
                    child: Text(
                      l,
                      style: const TextStyle(
                        fontSize: 12,
                        height: 1.6,
                        color: ZfTheme.textDim,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          const SizedBox(height: 6),
          SizedBox(
            width: double.infinity,
            child: accepted
                ? Row(
                    children: const [
                      Icon(Icons.verified_user_outlined, size: 15, color: ZfTheme.accent),
                      SizedBox(width: 6),
                      Text(
                        '已确认',
                        style: TextStyle(
                          fontFamily: 'monospace',
                          fontSize: 12,
                          color: ZfTheme.accent,
                        ),
                      ),
                    ],
                  )
                : OutlinedButton(
                    onPressed: onAccept,
                    style: OutlinedButton.styleFrom(
                      foregroundColor: ZfTheme.accent,
                      side: const BorderSide(color: ZfTheme.accentDim),
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(4)),
                      padding: const EdgeInsets.symmetric(vertical: 14),
                    ),
                    child: const Text(
                      '我已阅读并确认',
                      style: TextStyle(fontFamily: 'monospace', fontSize: 12.5, letterSpacing: 1),
                    ),
                  ),
          ),
        ],
      ),
    );
  }
}

/// 状态胶囊。
class ZfStatusChip extends StatelessWidget {
  const ZfStatusChip({super.key, required this.text, required this.color, this.pulse = false});

  final String text;
  final Color color;
  final bool pulse;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withOpacity(0.12),
        border: Border.all(color: color.withOpacity(0.55)),
        borderRadius: BorderRadius.circular(3),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          _Dot(color: color, pulse: pulse),
          const SizedBox(width: 6),
          Text(
            text,
            style: TextStyle(
              fontFamily: 'monospace',
              fontSize: 10.5,
              letterSpacing: 1.1,
              color: color,
            ),
          ),
        ],
      ),
    );
  }
}

class _Dot extends StatefulWidget {
  const _Dot({required this.color, required this.pulse});
  final Color color;
  final bool pulse;

  @override
  State<_Dot> createState() => _DotState();
}

class _DotState extends State<_Dot> with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 900),
  );

  @override
  void initState() {
    super.initState();
    if (widget.pulse) _c.repeat(reverse: true);
  }

  @override
  void didUpdateWidget(covariant _Dot old) {
    super.didUpdateWidget(old);
    if (widget.pulse && !_c.isAnimating) {
      _c.repeat(reverse: true);
    } else if (!widget.pulse && _c.isAnimating) {
      _c.stop();
      _c.value = 1;
    }
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _c,
      builder: (_, __) => Container(
        width: 6,
        height: 6,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: widget.color.withOpacity(widget.pulse ? 0.35 + 0.65 * _c.value : 1),
        ),
      ),
    );
  }
}

/// 主操作按钮。
class ZfActionButton extends StatelessWidget {
  const ZfActionButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.busy = false,
    this.destructive = false,
    this.icon,
  });

  final String label;
  final VoidCallback? onPressed;
  final bool busy;
  final bool destructive;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    final c = destructive ? ZfTheme.danger : ZfTheme.accent;
    final enabled = onPressed != null && !busy;
    return SizedBox(
      height: 50,
      child: FilledButton(
        onPressed: enabled ? onPressed : null,
        style: FilledButton.styleFrom(
          backgroundColor: enabled ? c.withOpacity(0.16) : ZfTheme.panelAlt,
          foregroundColor: enabled ? c : ZfTheme.textDim,
          side: BorderSide(color: enabled ? c : ZfTheme.line),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(4)),
        ),
        child: busy
            ? SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2, color: c),
              )
            : Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  if (icon != null) ...[Icon(icon, size: 17), const SizedBox(width: 8)],
                  Text(
                    label,
                    style: const TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 13.5,
                      letterSpacing: 2.2,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ],
              ),
      ),
    );
  }
}

/// 网格背景。
class ZfGridBackground extends StatelessWidget {
  const ZfGridBackground({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Stack(
      children: [
        const Positioned.fill(child: CustomPaint(painter: _GridPainter())),
        child,
      ],
    );
  }
}

class _GridPainter extends CustomPainter {
  const _GridPainter();

  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()
      ..color = const Color(0x0FFFFFFF)
      ..strokeWidth = 0.5;
    const step = 28.0;
    for (double x = 0; x < size.width; x += step) {
      canvas.drawLine(Offset(x, 0), Offset(x, size.height), p);
    }
    for (double y = 0; y < size.height; y += step) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), p);
    }
    final glow = Paint()
      ..shader = ui.Gradient.radial(
        Offset(size.width * 0.5, 0),
        size.width * 0.9,
        [const Color(0x2235E0B0), const Color(0x00000000)],
      );
    canvas.drawRect(Offset.zero & size, glow);
  }

  @override
  bool shouldRepaint(covariant CustomPainter old) => false;
}
