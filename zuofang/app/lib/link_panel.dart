import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'app_shell.dart';
import 'app_theme.dart';
import 'radar.dart';
import 'rtt_chart.dart';
import 'zf_widgets.dart';

/// 链路可视化：雷达拓扑 + RTT 曲线。
///
/// 两块放同一个面板，因为它们回答的是同一个问题 ——「现在链路好不好」。
/// 雷达给拓扑视角（哪一段红了），曲线给时间视角（抖动是刚发生的还是持续）。
class LinkPanel extends StatelessWidget {
  const LinkPanel({super.key, required this.shell});

  final ZfShell shell;

  @override
  Widget build(BuildContext context) {
    final running = shell.engine.running;
    return ZfPanel(
      title: '链路',
      trailing: ZfStatusChip(
        text: running ? '${shell.engine.latencyMs} MS' : 'OFFLINE',
        color: running ? ZfTheme.accent : ZfTheme.textDim,
        pulse: running,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // 这里直接用 RadarView 而不是 RadarPanel：外层已经是 ZfPanel，
          // 再用 RadarPanel 会套出双层边框和重复标题。
          Center(
            child: RadarView(
              blips: _blips(),
              size: math.min(_radarSize(context), 260),
              caption: running
                  ? '已接入 ${shell.session?.relay ?? '-'}'
                  : '未建立隧道，点击下方启动',
            ),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 10,
            runSpacing: 4,
            children: const [
              _LegendDot('正常', ZfTheme.accent),
              _LegendDot('波动', ZfTheme.warn),
              _LegendDot('异常', ZfTheme.danger),
              _LegendDot('未连接', ZfTheme.textDim),
            ],
          ),
          const SizedBox(height: 14),
          Row(
            children: const [
              Text(
                'RTT',
                style: TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 10,
                  letterSpacing: 1.8,
                  color: ZfTheme.textDim,
                ),
              ),
              SizedBox(width: 8),
              Expanded(child: Divider(color: ZfTheme.line, height: 1)),
              SizedBox(width: 8),
              Text(
                '60s',
                style: TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 9.5,
                  color: ZfTheme.textDim,
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          RttChart(values: shell.rttSamples),
        ],
      ),
    );
  }

  /// 雷达尺寸跟着面板宽度走，小屏缩到 210 免得图和边缘打架。
  double _radarSize(BuildContext context) {
    final w = MediaQuery.of(context).size.width;
    return w < 360 ? 210 : 250;
  }

  /// 雷达点位。
  ///
  /// 距离/角度是写死的：只是为了把五个关键节点在圆盘上摊开不重叠，
  /// 不代表真实拓扑。状态才是信息量所在。
  List<RadarBlip> _blips() {
    final on = shell.engine.running;
    final relayOk = (shell.session?.relay ?? '').isNotEmpty;
    final lat = shell.engine.latencyMs;
    return [
      RadarBlip(
        label: 'TUN',
        distance: 0.26,
        angle: -1.05,
        state: on ? RadarNodeState.ok : RadarNodeState.idle,
      ),
      RadarBlip(
        label: 'RELAY',
        distance: 0.55,
        angle: 0.55,
        state: !on
            ? RadarNodeState.idle
            : (relayOk ? RadarNodeState.ok : RadarNodeState.bad),
      ),
      RadarBlip(
        label: 'AUTH',
        distance: 0.42,
        angle: 2.35,
        state: shell.authed ? RadarNodeState.ok : RadarNodeState.bad,
      ),
      RadarBlip(
        label: 'PEER',
        distance: 0.78,
        angle: -2.2,
        state: on ? RadarNodeState.ok : RadarNodeState.idle,
      ),
      RadarBlip(
        label: '${lat}ms',
        distance: 0.9,
        angle: 2.9,
        state: !on
            ? RadarNodeState.idle
            : (lat < 80
                ? RadarNodeState.ok
                : (lat < 200 ? RadarNodeState.warn : RadarNodeState.bad)),
      ),
    ];
  }
}

/// 图例圆点。原来复用 RadarPanel 的图例，拆开后自带一份。
class _LegendDot extends StatelessWidget {
  const _LegendDot(this.name, this.color);

  final String name;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 5,
          height: 5,
          decoration: BoxDecoration(color: color, shape: BoxShape.circle),
        ),
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
    );
  }
}
