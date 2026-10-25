import 'package:flutter/material.dart';

import 'api.dart';
import 'app_shell.dart';
import 'app_theme.dart';
import 'format_utils.dart';
import 'zf_widgets.dart';

/// 房间选择面板：手填房间码 + 房间列表。
///
/// 做成 StatefulWidget 是因为输入框控制器要在这里持有并释放，
/// 之前放在 HomePage 里会让首页状态和控件生命周期纠缠在一起。
class RoomPanel extends StatefulWidget {
  const RoomPanel({super.key, required this.shell, required this.onToast});

  final ZfShell shell;
  final void Function(String message) onToast;

  @override
  State<RoomPanel> createState() => _RoomPanelState();
}

class _RoomPanelState extends State<RoomPanel> {
  final _code = TextEditingController();

  @override
  void dispose() {
    _code.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.shell;
    return ZfPanel(
      title: '房间',
      trailing: IconButton(
        onPressed: s.refreshRooms,
        iconSize: 16,
        visualDensity: VisualDensity.compact,
        tooltip: '刷新房间',
        icon: const Icon(Icons.refresh, color: ZfTheme.textDim),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: SizedBox(
                  height: 44,
                  child: TextField(
                    controller: _code,
                    textCapitalization: TextCapitalization.characters,
                    style: const TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 14,
                      letterSpacing: 3,
                      color: ZfTheme.text,
                    ),
                    decoration: const InputDecoration(hintText: '房间码'),
                    onSubmitted: (_) => _join(s),
                  ),
                ),
              ),
              const SizedBox(width: 8),
              SizedBox(
                width: 92,
                height: 44,
                child: FilledButton(
                  onPressed: s.busy ? null : () => _join(s),
                  style: FilledButton.styleFrom(
                    backgroundColor: ZfTheme.accent.withOpacity(0.16),
                    foregroundColor: ZfTheme.accent,
                    side: const BorderSide(color: ZfTheme.accentDim),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(4),
                    ),
                  ),
                  child: const Text(
                    '加入',
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
          const SizedBox(height: 12),
          if (s.session != null) _joinedBanner(s),
          if (s.rooms.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 12),
              child: Text(
                '暂无可用房间',
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 11.5,
                  color: ZfTheme.textDim,
                ),
              ),
            )
          else
            for (final r in s.rooms) _tile(s, r),
        ],
      ),
    );
  }

  void _join(ZfShell s) {
    final code = _code.text.trim();
    if (code.isEmpty) {
      widget.onToast('请先填写房间码');
      return;
    }
    s.joinByCode(code);
  }

  Widget _joinedBanner(ZfShell s) {
    final sess = s.session!;
    return Container(
      padding: const EdgeInsets.all(10),
      margin: const EdgeInsets.only(bottom: 10),
      decoration: BoxDecoration(
        color: ZfTheme.accent.withOpacity(0.07),
        border: Border.all(color: ZfTheme.accentDim),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Row(
        children: [
          const Icon(Icons.link, size: 14, color: ZfTheme.accent),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '已加入 ${shortId(sess.sessionId)}',
              style: const TextStyle(
                fontFamily: 'monospace',
                fontSize: 11.5,
                color: ZfTheme.accent,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _tile(ZfShell s, RoomInfo r) {
    final full = r.capacity > 0 && r.online >= r.capacity;
    final joined = s.session?.roomId == r.id;
    return InkWell(
      onTap: full ? null : () => s.joinByCode(r.code),
      borderRadius: BorderRadius.circular(4),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 11),
        margin: const EdgeInsets.only(bottom: 6),
        decoration: BoxDecoration(
          color: ZfTheme.panelAlt,
          border: Border.all(color: joined ? ZfTheme.accentDim : ZfTheme.line),
          borderRadius: BorderRadius.circular(4),
        ),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    r.title,
                    style: const TextStyle(fontSize: 13, color: ZfTheme.text),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    '${r.code} · ${r.online}/${r.capacity}',
                    style: const TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 10.5,
                      color: ZfTheme.textDim,
                    ),
                  ),
                ],
              ),
            ),
            ZfStatusChip(
              text: joined ? 'ACTIVE' : (full ? 'FULL' : 'JOIN'),
              color: joined
                  ? ZfTheme.accent
                  : (full ? ZfTheme.textDim : ZfTheme.accent),
            ),
          ],
        ),
      ),
    );
  }
}
