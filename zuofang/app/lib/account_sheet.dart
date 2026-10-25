import 'package:flutter/material.dart';

import 'app_shell.dart';
import 'app_theme.dart';
import 'format_utils.dart';
import 'zf_widgets.dart';

/// 账号弹窗：卡密详情 + 退出登录。
Future<void> showAccountSheet(BuildContext context, ZfShell shell) {
  return showModalBottomSheet<void>(
    context: context,
    backgroundColor: ZfTheme.panel,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(10)),
    ),
    builder: (ctx) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(18),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              '卡密信息',
              style: TextStyle(
                fontFamily: 'monospace',
                fontSize: 12,
                letterSpacing: 2,
                color: ZfTheme.accent,
              ),
            ),
            const SizedBox(height: 14),
            ZfMetric(label: '卡密', value: shell.card?.masked ?? '-'),
            ZfMetric(label: '套餐', value: '${shell.card?.planDays ?? 0}', unit: '天'),
            ZfMetric(
              label: '到期',
              value: shell.card?.expiresAt == null
                  ? '-'
                  : fmtTime(shell.card!.expiresAt!),
            ),
            ZfMetric(
              label: '剩余时长',
              value: fmtRemain(shell.remainingSeconds),
            ),
            ZfMetric(
              label: '绑定设备',
              value: (shell.card?.boundDevice ?? '').isEmpty
                  ? '-'
                  : shortId(shell.card!.boundDevice),
            ),
            ZfMetric(label: '引擎', value: shell.engine.state),
            const SizedBox(height: 16),
            OutlinedButton(
              onPressed: () async {
                Navigator.of(ctx).pop();
                // stage 会切成 auth，_Root 自动渲染卡密登录页
                await shell.logout();
              },
              style: OutlinedButton.styleFrom(
                foregroundColor: ZfTheme.danger,
                side: const BorderSide(color: Color(0x55FF5C72)),
                padding: const EdgeInsets.symmetric(vertical: 14),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(4),
                ),
              ),
              child: const Text(
                '退出登录',
                style: TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 12.5,
                  letterSpacing: 1,
                ),
              ),
            ),
          ],
        ),
      ),
    ),
  );
}
