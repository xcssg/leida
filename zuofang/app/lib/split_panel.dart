import 'package:flutter/material.dart';

import 'app_shell.dart';
import 'app_theme.dart';
import 'native_engine.dart';
import 'zf_widgets.dart';

/// 分流设置面板。
///
/// 两种模式：
///   * all  —— 全量代理，所有应用都进隧道，配置最省心但会干扰系统应用；
///   * apps —— 白名单，只有选中的应用走隧道，默认只代理游戏。
///
/// 白名单模式要求列表非空，否则隧道等于没通。这里不做强制拦截，
/// 交给 [ZfShell.startEngine] 在启动时给明确提示。
class SplitPanel extends StatefulWidget {
  const SplitPanel({super.key, required this.shell});

  final ZfShell shell;

  @override
  State<SplitPanel> createState() => _SplitPanelState();
}

class _SplitPanelState extends State<SplitPanel> {
  /// 应用列表拉取一次就缓存：系统应用动辄两三百个，每次展开都扫一遍太慢。
  List<InstalledApp>? _apps;
  bool _loading = false;

  @override
  Widget build(BuildContext context) {
    final s = widget.shell;
    final apps = s.splitMode == SplitMode.all;
    return ZfPanel(
      title: '分流',
      trailing: ZfStatusChip(
        text: apps ? 'ALL' : '${s.splitApps.length} APPS',
        color: apps ? ZfTheme.warn : ZfTheme.accent,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _modeRow(
            title: '全部应用',
            subtitle: '所有流量经隧道转发，兼容性最好',
            selected: apps,
            onTap: () => s.setSplitMode(SplitMode.all),
          ),
          const SizedBox(height: 6),
          _modeRow(
            title: '仅选中应用',
            subtitle: '只有白名单内的应用走隧道，其余直连',
            selected: !apps,
            onTap: () => s.setSplitMode(SplitMode.apps),
          ),
          if (!apps) ...[
            const SizedBox(height: 10),
            if (s.splitApps.isEmpty)
              const Text(
                '尚未选择应用。白名单为空时无法启动隧道。',
                style: TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 11,
                  height: 1.6,
                  color: ZfTheme.danger,
                ),
              )
            else
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: [
                  for (final p in s.splitApps.take(6)) _pkgChip(p),
                  if (s.splitApps.length > 6)
                    _pkgChip('+${s.splitApps.length - 6}'),
                ],
              ),
            const SizedBox(height: 10),
            OutlinedButton.icon(
              onPressed: _loading ? null : _pick,
              icon: _loading
                  ? const SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(
                        strokeWidth: 1.6,
                        color: ZfTheme.accent,
                      ),
                    )
                  : const Icon(Icons.apps, size: 16),
              label: Text(_loading ? '读取应用列表…' : '选择应用'),
              style: OutlinedButton.styleFrom(
                foregroundColor: ZfTheme.accent,
                side: const BorderSide(color: ZfTheme.accentDim),
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
          const SizedBox(height: 4),
          const Divider(height: 20, color: ZfTheme.line),
          SwitchListTile.adaptive(
            contentPadding: EdgeInsets.zero,
            dense: true,
            value: s.autoReconnect,
            activeColor: ZfTheme.accent,
            onChanged: s.toggleAutoReconnect,
            title: const Text(
              '断线自动重连',
              style: TextStyle(fontSize: 12.5, color: ZfTheme.text),
            ),
            subtitle: const Text(
              '隧道意外中断时自动重新加入线路，最多重试 3 次',
              style: TextStyle(fontSize: 10.5, height: 1.5, color: ZfTheme.textDim),
            ),
          ),
        ],
      ),
    );
  }

  Widget _modeRow({
    required String title,
    required String subtitle,
    required bool selected,
    required VoidCallback onTap,
  }) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(4),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
        decoration: BoxDecoration(
          color: selected ? ZfTheme.accent.withOpacity(0.08) : ZfTheme.panelAlt,
          border: Border.all(color: selected ? ZfTheme.accentDim : ZfTheme.line),
          borderRadius: BorderRadius.circular(4),
        ),
        child: Row(
          children: [
            Icon(
              selected ? Icons.radio_button_checked : Icons.radio_button_unchecked,
              size: 16,
              color: selected ? ZfTheme.accent : ZfTheme.textDim,
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: TextStyle(
                      fontSize: 12.5,
                      color: selected ? ZfTheme.accent : ZfTheme.text,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: const TextStyle(
                      fontSize: 10.5,
                      height: 1.4,
                      color: ZfTheme.textDim,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _pkgChip(String label) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
        decoration: BoxDecoration(
          color: ZfTheme.panelAlt,
          border: Border.all(color: ZfTheme.line),
          borderRadius: BorderRadius.circular(3),
        ),
        child: Text(
          label,
          style: const TextStyle(
            fontFamily: 'monospace',
            fontSize: 10,
            color: ZfTheme.textDim,
          ),
        ),
      );

  Future<void> _pick() async {
    setState(() => _loading = true);
    var apps = _apps;
    if (apps == null) {
      apps = await NativeEngine.listApps();
      _apps = apps;
    }
    if (!mounted) return;
    setState(() => _loading = false);

    if (apps.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('读取不到应用列表，可能是原生层未实现 listApps'),
          duration: Duration(seconds: 3),
        ),
      );
      return;
    }

    final picked = await showModalBottomSheet<List<String>>(
      context: context,
      backgroundColor: ZfTheme.panel,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(10)),
      ),
      builder: (_) => _AppPicker(
        apps: apps!,
        initial: widget.shell.splitApps.toSet(),
      ),
    );
    if (picked != null) {
      await widget.shell.setSplitApps(picked);
    }
  }
}

/// 应用多选弹窗。带搜索框，避免在两三百个包里翻找。
class _AppPicker extends StatefulWidget {
  const _AppPicker({required this.apps, required this.initial});

  final List<InstalledApp> apps;
  final Set<String> initial;

  @override
  State<_AppPicker> createState() => _AppPickerState();
}

class _AppPickerState extends State<_AppPicker> {
  late final Set<String> _sel = {...widget.initial};
  final _query = TextEditingController();

  @override
  void dispose() {
    _query.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final q = _query.text.trim().toLowerCase();
    final list = q.isEmpty
        ? widget.apps
        : widget.apps
            .where((a) =>
                a.label.toLowerCase().contains(q) ||
                a.packageName.toLowerCase().contains(q))
            .toList(growable: false);

    return SafeArea(
      child: SizedBox(
        height: MediaQuery.of(context).size.height * 0.78,
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
              child: Row(
                children: [
                  const Text(
                    '选择应用',
                    style: TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 12,
                      letterSpacing: 2,
                      color: ZfTheme.accent,
                    ),
                  ),
                  const Spacer(),
                  Text(
                    '${_sel.length} 已选',
                    style: const TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 10.5,
                      color: ZfTheme.textDim,
                    ),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: SizedBox(
                height: 42,
                child: TextField(
                  controller: _query,
                  onChanged: (_) => setState(() {}),
                  style: const TextStyle(fontSize: 13, color: ZfTheme.text),
                  decoration: const InputDecoration(
                    hintText: '搜索应用名或包名',
                    prefixIcon: Icon(Icons.search, size: 17, color: ZfTheme.textDim),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: list.isEmpty
                  ? const Center(
                      child: Text(
                        '没有匹配的应用',
                        style: TextStyle(
                          fontFamily: 'monospace',
                          fontSize: 11.5,
                          color: ZfTheme.textDim,
                        ),
                      ),
                    )
                  : ListView.builder(
                      itemCount: list.length,
                      itemBuilder: (_, i) {
                        final a = list[i];
                        final on = _sel.contains(a.packageName);
                        return CheckboxListTile(
                          dense: true,
                          value: on,
                          activeColor: ZfTheme.accent,
                          checkColor: ZfTheme.bg,
                          onChanged: (v) => setState(() {
                            if (v == true) {
                              _sel.add(a.packageName);
                            } else {
                              _sel.remove(a.packageName);
                            }
                          }),
                          title: Text(
                            a.label,
                            style: const TextStyle(
                              fontSize: 12.5,
                              color: ZfTheme.text,
                            ),
                          ),
                          subtitle: Text(
                            a.packageName,
                            style: const TextStyle(
                              fontFamily: 'monospace',
                              fontSize: 10,
                              color: ZfTheme.textDim,
                            ),
                          ),
                        );
                      },
                    ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
              child: Row(
                children: [
                  Expanded(
                    child: OutlinedButton(
                      onPressed: () => setState(_sel.clear),
                      style: OutlinedButton.styleFrom(
                        foregroundColor: ZfTheme.textDim,
                        side: const BorderSide(color: ZfTheme.line),
                        padding: const EdgeInsets.symmetric(vertical: 13),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(4),
                        ),
                      ),
                      child: const Text(
                        '清空',
                        style: TextStyle(
                          fontFamily: 'monospace',
                          fontSize: 12.5,
                          letterSpacing: 1,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    flex: 2,
                    child: FilledButton(
                      onPressed: () => Navigator.of(context).pop(_sel.toList()),
                      style: FilledButton.styleFrom(
                        backgroundColor: ZfTheme.accent.withOpacity(0.16),
                        foregroundColor: ZfTheme.accent,
                        side: const BorderSide(color: ZfTheme.accentDim),
                        padding: const EdgeInsets.symmetric(vertical: 13),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(4),
                        ),
                      ),
                      child: const Text(
                        '确定',
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
            ),
          ],
        ),
      ),
    );
  }
}
