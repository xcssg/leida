import 'package:flutter/material.dart';

import 'app_shell.dart';
import 'app_theme.dart';
import 'format_utils.dart';
import 'zf_widgets.dart';

/// 卡密续期面板。
///
/// 续期逻辑在服务端：把一张未使用的新卡密时长叠加到当前卡密上，绑定设备不变。
/// 客户端只负责把输入规整成 `ZF-XXXX-XXXX-XXXX-XXXX` 再提交。
class RenewPanel extends StatefulWidget {
  const RenewPanel({super.key, required this.shell, required this.onToast});

  final ZfShell shell;
  final void Function(String message) onToast;

  @override
  State<RenewPanel> createState() => _RenewPanelState();
}

class _RenewPanelState extends State<RenewPanel> {
  final _key = TextEditingController();

  @override
  void dispose() {
    _key.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.shell;
    final card = s.card;
    return ZfPanel(
      title: '卡密续期',
      trailing: Text(
        card == null ? '-' : '${card.planDays}D 套餐',
        style: const TextStyle(
          fontFamily: 'monospace',
          fontSize: 10.5,
          color: ZfTheme.textDim,
        ),
      ),
      accentColor: ZfTheme.warn,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (card != null) ...[
            ZfMetric(label: '当前卡密', value: card.masked),
            ZfMetric(
              label: '到期时间',
              value: card.expiresAt == null ? '未激活' : fmtTime(card.expiresAt!),
              color: card.expired ? ZfTheme.danger : ZfTheme.warn,
            ),
            ZfMetric(
              label: '剩余天数',
              value: (card.remainingSeconds / 86400).toStringAsFixed(1),
              unit: '天',
              color: ZfTheme.accent,
            ),
            const SizedBox(height: 12),
          ],
          Row(
            children: [
              Expanded(
                child: SizedBox(
                  height: 44,
                  child: TextField(
                    controller: _key,
                    textCapitalization: TextCapitalization.characters,
                    style: const TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 13,
                      letterSpacing: 1.6,
                      color: ZfTheme.text,
                    ),
                    decoration: const InputDecoration(
                      hintText: 'ZF-XXXX-XXXX-XXXX-XXXX',
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 8),
              SizedBox(
                width: 92,
                height: 44,
                child: FilledButton(
                  onPressed: s.busy ? null : _submit,
                  style: FilledButton.styleFrom(
                    backgroundColor: ZfTheme.warn.withOpacity(0.14),
                    foregroundColor: ZfTheme.warn,
                    side: const BorderSide(color: Color(0x55F2B45A)),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(4),
                    ),
                  ),
                  child: const Text(
                    '续期',
                    style: TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 12.5,
                      letterSpacing: 1,
                    ),
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          const Text(
            '输入一张未使用过的新卡密，时长会叠加到当前卡密上，绑定设备不变。',
            style: TextStyle(fontSize: 11, height: 1.6, color: ZfTheme.textDim),
          ),
        ],
      ),
    );
  }

  Future<void> _submit() async {
    final s = widget.shell;
    final key = normalizeCard(_key.text);
    if (key.isEmpty) {
      widget.onToast('请先填写卡密');
      return;
    }
    final days = await s.renew(key);
    if (!mounted) return;
    if (days != null) {
      _key.clear();
      widget.onToast('续期成功，+$days 天');
    } else {
      widget.onToast(s.error ?? '续期失败');
    }
  }
}
