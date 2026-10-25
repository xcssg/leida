import 'package:flutter/material.dart';

import 'app_shell.dart';
import 'app_theme.dart';
import 'format_utils.dart';
import 'zf_widgets.dart';

/// 抓包面板。
///
/// 抓包本质上是把隧道里的明文包镜像落盘，用来排查「某个游戏为什么连不上」。
/// 界面上只暴露包数/字节/镜像块三个数字，不做实时抓包列表 —— 手机上滚不赢。
class CapturePanel extends StatelessWidget {
  const CapturePanel({super.key, required this.shell});

  final ZfShell shell;

  @override
  Widget build(BuildContext context) {
    final c = shell.capture;
    return ZfPanel(
      title: '抓包',
      accentColor: ZfTheme.warn,
      trailing: ZfStatusChip(
        text: c.active ? 'CAPTURING' : 'OFF',
        color: c.active ? ZfTheme.warn : ZfTheme.textDim,
        pulse: c.active,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          ZfMetrics(
            items: [
              ZfMetric(label: '包数', value: '${c.packets}'),
              ZfMetric(label: '字节', value: fmtBytes(c.bytes)),
              ZfMetric(label: '镜像块', value: '${c.banks}'),
              ZfMetric(
                label: '到期',
                value: c.expiresAt == null ? '-' : fmtTime(c.expiresAt!),
              ),
            ],
          ),
          const SizedBox(height: 12),
          OutlinedButton.icon(
            onPressed: shell.busy ? null : shell.toggleCapture,
            icon: Icon(
              c.active ? Icons.stop_circle_outlined : Icons.play_circle_outline,
              size: 17,
            ),
            label: Text(c.active ? '停止抓包' : '开始抓包'),
            style: OutlinedButton.styleFrom(
              foregroundColor: ZfTheme.warn,
              side: const BorderSide(color: Color(0x66F2B45A)),
              padding: const EdgeInsets.symmetric(vertical: 12),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(4),
              ),
              textStyle: const TextStyle(
                fontFamily: 'monospace',
                fontSize: 12.5,
                letterSpacing: 1,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
