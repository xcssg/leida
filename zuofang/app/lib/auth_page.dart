import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'app_shell.dart';
import 'app_theme.dart';
import 'zf_widgets.dart';

/// 卡密登录页（卡密即账号，无需注册）。
class AuthPage extends StatefulWidget {
  const AuthPage({super.key, required this.shell});

  final ZfShell shell;

  @override
  State<AuthPage> createState() => _AuthPageState();
}

class _AuthPageState extends State<AuthPage> {
  final _card = TextEditingController();

  @override
  void initState() {
    super.initState();
    widget.shell.addListener(_onShell);
  }

  @override
  void dispose() {
    widget.shell.removeListener(_onShell);
    _card.dispose();
    super.dispose();
  }

  void _onShell() {
    if (mounted) setState(() {});
  }

  /// 输入时自动格式化：大写、去空格、每 4 位补横线。
  String _format(String raw) {
    var v = raw.toUpperCase().replaceAll(RegExp(r'[^A-Z0-9]'), '');
    if (v.startsWith('ZF')) v = v.substring(2);
    final buf = <String>[];
    for (var i = 0; i < v.length && i < 16; i += 4) {
      buf.add(v.substring(i, (i + 4).clamp(0, v.length)));
    }
    return buf.isEmpty ? '' : 'ZF-${buf.join('-')}';
  }

  Future<void> _submit() async {
    final key = _card.text.trim();
    if (key.length < 8) {
      _toast('请输入完整卡密，格式 ZF-XXXX-XXXX-XXXX-XXXX');
      return;
    }
    final ok = await widget.shell.activateCard(key);
    if (!mounted) return;
    if (!ok) _toast(widget.shell.error ?? '登录失败');
  }

  Future<void> _paste() async {
    final data = await Clipboard.getData(Clipboard.kTextPlain);
    final t = data?.text ?? '';
    if (t.isEmpty) return;
    final f = _format(t);
    setState(() => _card.text = f);
    _submit();
  }

  void _toast(String m) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(m)));

  @override
  Widget build(BuildContext context) {
    final s = widget.shell;
    return ZfGridBackground(
      child: Scaffold(
        backgroundColor: Colors.transparent,
        body: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 28),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const _Brand(),
                    const SizedBox(height: 28),
                    ZfPanel(
                      title: '卡密登录',
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          SizedBox(
                            height: 48,
                            child: TextField(
                              controller: _card,
                              textCapitalization: TextCapitalization.characters,
                              inputFormatters: [
                                FilteringTextInputFormatter.allow(RegExp(r'[A-Za-z0-9\-]')),
                              ],
                              onChanged: (v) {
                                final f = _format(v);
                                if (f != v) {
                                  _card.value = TextEditingValue(
                                    text: f,
                                    selection: TextSelection.collapsed(offset: f.length),
                                  );
                                }
                              },
                              style: const TextStyle(
                                fontFamily: 'monospace',
                                fontSize: 15,
                                letterSpacing: 1.8,
                                color: ZfTheme.text,
                              ),
                              decoration: const InputDecoration(
                                hintText: 'ZF-XXXX-XXXX-XXXX-XXXX',
                                prefixIcon: Icon(Icons.vpn_key_outlined,
                                    size: 17, color: ZfTheme.textDim),
                                prefixIconConstraints: BoxConstraints(minWidth: 40),
                              ),
                            ),
                          ),
                          const SizedBox(height: 10),
                          Text(
                            '首次登录即激活并绑定本机，之后换设备需联系管理员解绑',
                            style: TextStyle(
                              fontFamily: 'monospace',
                              fontSize: 10.5,
                              height: 1.5,
                              color: ZfTheme.textDim.withOpacity(0.85),
                            ),
                          ),
                          const SizedBox(height: 18),
                          ZfActionButton(
                            label: '登 录 / 激 活',
                            busy: s.busy,
                            onPressed: _submit,
                          ),
                          const SizedBox(height: 10),
                          TextButton(
                            onPressed: s.busy ? null : _paste,
                            child: const Text(
                              '从剪贴板粘贴并登录',
                              style: TextStyle(
                                fontFamily: 'monospace',
                                fontSize: 12,
                                color: ZfTheme.textDim,
                              ),
                            ),
                          ),
                          if (s.error != null) ...[
                            const SizedBox(height: 6),
                            Text(
                              s.error!,
                              style: const TextStyle(
                                fontFamily: 'monospace',
                                fontSize: 11.5,
                                color: ZfTheme.danger,
                              ),
                            ),
                          ],
                        ],
                      ),
                    ),
                    const SizedBox(height: 18),
                    Text(
                      '仅用于自有网络的流量转发与授权抓包',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 10.5,
                        color: ZfTheme.textDim.withOpacity(0.7),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _Brand extends StatelessWidget {
  const _Brand();

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Container(
          width: 52,
          height: 52,
          decoration: BoxDecoration(
            border: Border.all(color: ZfTheme.accent, width: 1.4),
            borderRadius: BorderRadius.circular(4),
          ),
          alignment: Alignment.center,
          child: const Text(
            'ZF',
            style: TextStyle(
              fontFamily: 'monospace',
              fontSize: 17,
              fontWeight: FontWeight.w700,
              letterSpacing: 1,
              color: ZfTheme.accent,
            ),
          ),
        ),
        const SizedBox(height: 14),
        const Text(
          'ZUOFANG',
          style: TextStyle(
            fontFamily: 'monospace',
            fontSize: 20,
            letterSpacing: 5,
            fontWeight: FontWeight.w700,
            color: ZfTheme.text,
          ),
        ),
        const SizedBox(height: 6),
        const Text(
          'LOCAL TRAFFIC RELAY',
          style: TextStyle(
            fontFamily: 'monospace',
            fontSize: 10,
            letterSpacing: 2.6,
            color: ZfTheme.textDim,
          ),
        ),
      ],
    );
  }
}

/// 首次启动声明页。
class DisclosurePage extends StatelessWidget {
  const DisclosurePage({super.key, required this.shell});

  final ZfShell shell;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(22),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 460),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Text(
                    'ZUOFANG',
                    style: TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 22,
                      letterSpacing: 5,
                      fontWeight: FontWeight.w700,
                      color: ZfTheme.text,
                    ),
                  ),
                  const SizedBox(height: 6),
                  const Text(
                    '本地流量转发与授权抓包',
                    style: TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 11,
                      letterSpacing: 1.6,
                      color: ZfTheme.textDim,
                    ),
                  ),
                  const SizedBox(height: 26),
                  ZfDisclosure(
                    accepted: false,
                    onAccept: shell.acceptDisclosure,
                    lines: const [
                      '本程序通过 Android VpnService 建立本地 VPN，仅转发你主动选择的本机应用流量。',
                      '不会收集通讯录、短信、位置、相册等与转发无关的数据。',
                      '抓包功能只在你手动开启后运行，数据仅上传到你自己的中继服务。',
                      '不得用于任何未获授权的网络干扰、作弊或侵犯他人权益的行为。',
                      '继续使用即表示你已获得所处网络与设备的所有必要授权。',
                    ],
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
