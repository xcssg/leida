import 'dart:async';

import 'package:flutter/material.dart';

import 'account_sheet.dart';
import 'app_shell.dart';
import 'app_theme.dart';
import 'capture_panel.dart';
import 'link_panel.dart';
import 'renew_panel.dart';
import 'room_panel.dart';
import 'session_card.dart';
import 'split_panel.dart';
import 'zf_widgets.dart';

/// 首页只负责组装，所有区块都在各自文件里。
///
/// 拆分的收益：以前一个 650 行的文件里同时管输入框控制器、动画计时器、
/// 五个面板的绘制，改一处要通读全文；现在每块独立，改分流不会碰到卡片。
class HomePage extends StatefulWidget {
  const HomePage({super.key, required this.shell});

  final ZfShell shell;

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  Timer? _refresh;

  @override
  void initState() {
    super.initState();
    // 房间在线人数是服务端算的，30 秒刷一次足够，太频繁会吃掉批量请求配额。
    _refresh = Timer.periodic(const Duration(seconds: 30), (_) {
      widget.shell.refreshRooms();
    });
    widget.shell.addListener(_onShell);
  }

  @override
  void dispose() {
    _refresh?.cancel();
    widget.shell.removeListener(_onShell);
    super.dispose();
  }

  void _onShell() {
    if (mounted) setState(() {});
  }

  void _toast(String m) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(m), duration: const Duration(seconds: 2)),
    );
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.shell;
    return Scaffold(
      appBar: AppBar(
        title: const Text('ZUOFANG'),
        actions: [
          ZfStatusChip(
            text: s.engine.running ? 'RUNNING' : 'IDLE',
            color: s.engine.running ? ZfTheme.accent : ZfTheme.textDim,
            pulse: s.engine.running,
          ),
          const SizedBox(width: 8),
          IconButton(
            tooltip: '账号',
            onPressed: () => showAccountSheet(context, s),
            icon: const Icon(
              Icons.person_outline,
              size: 19,
              color: ZfTheme.textDim,
            ),
          ),
        ],
      ),
      body: RefreshIndicator(
        color: ZfTheme.accent,
        backgroundColor: ZfTheme.panel,
        onRefresh: s.refreshRooms,
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            SessionCard(shell: s),
            const SizedBox(height: 14),
            LinkPanel(shell: s),
            const SizedBox(height: 14),
            EnginePanel(shell: s),
            const SizedBox(height: 14),
            RoomPanel(shell: s, onToast: _toast),
            const SizedBox(height: 14),
            SplitPanel(shell: s),
            const SizedBox(height: 14),
            CapturePanel(shell: s),
            const SizedBox(height: 14),
            RenewPanel(shell: s, onToast: _toast),
            const SizedBox(height: 14),
            _actionPanel(s),
          ],
        ),
      ),
    );
  }

  Widget _actionPanel(ZfShell s) {
    final running = s.engine.running;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        ZfActionButton(
          label: running ? '停 止 隧 道' : '启 动 隧 道',
          icon: running ? Icons.power_settings_new : Icons.bolt,
          busy: s.busy,
          destructive: running,
          onPressed: running ? s.stopEngine : () => s.startEngine(),
        ),
        if (s.error != null) ...[
          const SizedBox(height: 10),
          Text(
            s.error!,
            textAlign: TextAlign.center,
            style: const TextStyle(
              fontFamily: 'monospace',
              fontSize: 11,
              color: ZfTheme.danger,
            ),
          ),
        ],
      ],
    );
  }
}
