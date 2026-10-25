import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'app_shell.dart';
import 'app_theme.dart';
import 'auth_page.dart';
import 'home_page.dart';
import 'session_store.dart';

/// 可通过 `--dart-define=ZF_API=https://host:port` 覆盖。
const _kApiBase = String.fromEnvironment('ZF_API', defaultValue: 'http://10.0.2.2:8090');

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
    statusBarColor: Colors.transparent,
    statusBarIconBrightness: Brightness.light,
    systemNavigationBarColor: ZfTheme.bg,
    systemNavigationBarIconBrightness: Brightness.light,
  ));
  runApp(const ZfApp());
}

class ZfApp extends StatefulWidget {
  const ZfApp({super.key});

  @override
  State<ZfApp> createState() => _ZfAppState();
}

class _ZfAppState extends State<ZfApp> {
  late final ZfShell _shell = ZfShell(
    api: ZfApi(baseUrl: _kApiBase),
    store: SessionStore(),
  );

  @override
  void initState() {
    super.initState();
    _shell.boot();
  }

  @override
  void dispose() {
    _shell.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ZfScope(
      shell: _shell,
      child: MaterialApp(
        title: 'ZUOFANG',
        debugShowCheckedModeBanner: false,
        theme: ZfTheme.build(),
        scrollBehavior: const ZfScrollBehavior(),
        home: const _Root(),
      ),
    );
  }
}

class _Root extends StatelessWidget {
  const _Root();

  @override
  Widget build(BuildContext context) {
    final shell = ZfScope.of(context);
    final Widget page = switch (shell.stage) {
      ShellStage.booting => const _Splash(),
      ShellStage.disclosure => DisclosurePage(shell: shell),
      ShellStage.auth => AuthPage(shell: shell),
      ShellStage.ready => HomePage(shell: shell),
    };
    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 260),
      child: KeyedSubtree(key: ValueKey(shell.stage), child: page),
    );
  }
}

class _Splash extends StatelessWidget {
  const _Splash();

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              'ZUOFANG',
              style: TextStyle(
                fontFamily: 'monospace',
                fontSize: 20,
                letterSpacing: 6,
                fontWeight: FontWeight.w700,
                color: ZfTheme.text,
              ),
            ),
            SizedBox(height: 22),
            SizedBox(
              width: 90,
              child: LinearProgressIndicator(
                minHeight: 2,
                backgroundColor: ZfTheme.line,
                color: ZfTheme.accent,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
