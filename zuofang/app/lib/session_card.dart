import 'package:flutter/material.dart';

import 'app_shell.dart';
import 'app_theme.dart';
import 'format_utils.dart';
import 'zf_widgets.dart';

/// 会话概览：剩余时长大数字 + 四个关键指标。
///
/// 单独抽出来是因为它是首页唯一一块「看一眼就知道还能用多久」的区域，
/// 后续要做桌面小组件或通知栏快捷展示时可以复用同一个组件。
class SessionCard extends StatelessWidget {
  const SessionCard({super.key, required this.shell});

  final ZfShell shell;

  @override
  Widget build(BuildContext context) {
    final left = Duration(seconds: shell.remainingSeconds);
    final days = left.inDays;
    return ZfPanel(
      title: '会话概览',
      trailing: Text(
        shell.card?.masked ?? '未登录',
        style: const TextStyle(
          fontFamily: 'monospace',
          fontSize: 11,
          color: ZfTheme.textDim,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text(
                days > 0 ? '$days' : '${left.inHours}',
                style: const TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 40,
                  height: 1,
                  fontWeight: FontWeight.w700,
                  color: ZfTheme.accent,
                ),
              ),
              const SizedBox(width: 6),
              Padding(
                padding: const EdgeInsets.only(bottom: 4),
                child: Text(
                  days > 0 ? 'DAYS LEFT' : 'HOURS LEFT',
                  style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 10,
                    letterSpacing: 1.6,
                    color: ZfTheme.textDim,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          ZfMetrics(
            items: [
              ZfMetric(label: '延迟', value: '${shell.engine.latencyMs}', unit: 'ms'),
              ZfMetric(label: '丢包', value: '${shell.engine.dropped}'),
              ZfMetric(
                label: '下行',
                value: fmtBytes(shell.engine.rxBytes),
                color: ZfTheme.accent,
              ),
              ZfMetric(
                label: '上行',
                value: fmtBytes(shell.engine.txBytes),
                color: ZfTheme.warn,
              ),
            ],
          ),
        ],
      ),
    );
  }
}

/// 引擎状态明细。出问题时的排查入口，所以把会话 id、MTU、DNS、到期时间
/// 都摊开来 —— 用户报障时截图就能定位。
class EnginePanel extends StatelessWidget {
  const EnginePanel({super.key, required this.shell});

  final ZfShell shell;

  @override
  Widget build(BuildContext context) {
    final e = shell.engine;
    final cfg = shell.config;
    return ZfPanel(
      title: '引擎状态',
      trailing: ZfStatusChip(
        text: e.state.toUpperCase(),
        color: e.running
            ? ZfTheme.accent
            : (e.state == 'error' ? ZfTheme.danger : ZfTheme.textDim),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ZfMetric(
            label: '会话 ID',
            value: e.sessionId.isEmpty ? '-' : shortId(e.sessionId),
          ),
          ZfMetric(label: '中继', value: shell.session?.relay ?? '-'),
          ZfMetric(
            label: '房间',
            value: (shell.session?.roomTitle.isNotEmpty ?? false)
                ? '${shell.session!.roomTitle} · ${shell.session!.roomCode}'
                : '-',
          ),
          ZfMetric(label: 'MTU', value: '${cfg?.mtu ?? 1400}'),
          ZfMetric(label: 'DNS', value: cfg?.dns ?? '1.1.1.1'),
          if (e.expiresAt != null)
            ZfMetric(
              label: '到期',
              value: fmtTime(e.expiresAt!),
              color: ZfTheme.warn,
            ),
          if (e.reason.isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(
              e.reason,
              style: const TextStyle(
                fontFamily: 'monospace',
                fontSize: 11,
                color: ZfTheme.danger,
              ),
            ),
          ],
          if (cfg != null && cfg.notice.isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(
              cfg.notice,
              style: const TextStyle(
                fontSize: 11.5,
                height: 1.6,
                color: ZfTheme.textDim,
              ),
            ),
          ],
        ],
      ),
    );
  }
}
