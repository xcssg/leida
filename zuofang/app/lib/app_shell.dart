import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'api.dart';
import 'native_engine.dart';
import 'session_store.dart';

/// 全局应用状态。
class ZfScope extends InheritedNotifier<ZfShell> {
  const ZfScope({super.key, required ZfShell shell, required super.child})
      : super(notifier: shell);

  static ZfShell of(BuildContext context) {
    final c = context.dependOnInheritedWidgetOfExactType<ZfScope>();
    assert(c != null, 'ZfScope 缺失');
    return c!.notifier!;
  }
}

enum ShellStage { booting, disclosure, auth, ready }

class ZfShell extends ChangeNotifier {
  ZfShell({required this.api, required this.store});

  final ZfApi api;
  final SessionStore store;

  ShellStage stage = ShellStage.booting;
  bool busy = false;
  String? error;

  CardInfo? card;
  int remainingSeconds = 0;
  ClientConfig? config;
  List<RoomInfo> rooms = const [];
  JoinResult? session;
  EngineStatus engine = EngineStatus.empty;
  CaptureStats capture = CaptureStats.empty;

  /// 最近 60 秒的 RTT 采样，-1 表示该秒没拿到有效值（丢包或未跑起来）。
  /// 只有引擎 running 时才会往里推，避免停机后图表变成一条假线。
  final List<int> rttSamples = <int>[];
  static const int rttCapacity = 60;

  /// 分流偏好。改动会落盘，下次启动直接沿用。
  SplitMode splitMode = SplitMode.apps;
  List<String> splitApps = const [];

  /// 隧道掉线后是否自动重连（默认开）。
  bool autoReconnect = true;

  Timer? _ticker;
  Timer? _heartbeat;
  WebSocketChannel? _stateSocket;
  StreamSubscription? _stateSub;
  Map<String, dynamic> serverState = const {};

  bool _forced = false;

  /// 用户主动点了「停止」：此时即使引擎报 stopped 也不能自动拉起来。
  bool _userStopped = false;

  /// 连续自动重连次数，超过 [_maxReconnect] 就放弃并把原因写进 error。
  int _reconnectTries = 0;
  static const int _maxReconnect = 3;

  /// 防止轮询 tick 重入：一次重连可能要几百毫秒，期间其他 tick 不该再触发。
  bool _reconnecting = false;

  /// 从来没有启动过引擎时不做自动重连（否则一进首页就会自己弹 VPN 授权）。
  bool _everStarted = false;

  bool get authed => api.token != null && api.token!.isNotEmpty;
  String get cardKey => card?.cardKey ?? '';

  Future<void> boot() async {
    // 分流偏好与会话无关，登录与否都要先读出来，否则首页会闪一下默认值。
    await _loadSplitPrefs();
    try {
      api.device = await store.deviceToken();
      final token = await store.readToken();
      if (token != null && token.isNotEmpty) {
        api.token = token;
        card = await api.heartbeat();
        remainingSeconds = card!.remainingSeconds;
        await _afterAuth();
        return;
      }
    } catch (_) {
      // token 失效 / 卡密到期 / 被封 → 回到登录页
      api.token = null;
      await store.clear();
      card = null;
    }
    stage = ShellStage.disclosure;
    notifyListeners();
  }

  void acceptDisclosure() {
    stage = authed ? ShellStage.ready : ShellStage.auth;
    notifyListeners();
  }

  /// 卡密登录：首次使用即激活并开始计时。
  Future<bool> activateCard(String key) async {
    _setBusy(true);
    try {
      api.device = await store.deviceToken();
      final info = await api.activate(key);
      await store.saveSession(api.token!, info.cardKey);
      card = info;
      remainingSeconds = info.remainingSeconds;
      await _afterAuth();
      return true;
    } catch (e) {
      error = _msg(e);
      notifyListeners();
      return false;
    } finally {
      _setBusy(false);
    }
  }

  /// 续期：用一张新卡密给当前卡密叠加时长。
  Future<int?> renew(String newKey) async {
    _setBusy(true);
    try {
      final r = await api.renew(newKey);
      card = r.card;
      remainingSeconds = r.card.remainingSeconds;
      notifyListeners();
      return r.days;
    } catch (e) {
      error = _msg(e);
      notifyListeners();
      return null;
    } finally {
      _setBusy(false);
    }
  }

  Future<void> logout() async {
    try {
      await api.logout();
    } catch (_) {}
    await stopEngine();
    await store.clear();
    _teardown();
    _heartbeat?.cancel();
    card = null;
    remainingSeconds = 0;
    rooms = const [];
    session = null;
    _forced = false;
    stage = ShellStage.auth;
    notifyListeners();
  }

  Future<void> refreshRooms() async {
    try {
      rooms = await api.rooms();
      notifyListeners();
    } catch (e) {
      error = _msg(e);
      notifyListeners();
    }
  }

  Future<void> joinByCode(String code) async {
    _setBusy(true);
    try {
      session = await api.join(code.trim().toUpperCase());
    } catch (e) {
      error = _msg(e);
    } finally {
      _setBusy(false);
    }
  }

  Future<void> startEngine({bool capture = false}) async {
    final s = session;
    if (s == null) {
      error = '请先加入线路';
      notifyListeners();
      return;
    }
    // 白名单模式下空名单等于把所有应用都排除，隧道起来了也不通，
    // 与其让用户以为是网络问题，不如在启动前就拦住。
    if (splitMode == SplitMode.apps && splitApps.isEmpty) {
      error = '分流模式为「仅选中应用」，请先挑选应用';
      notifyListeners();
      return;
    }
    _userStopped = false;
    _setBusy(true);
    try {
      final ok = await NativeEngine.requestPermission();
      if (!ok) {
        error = 'VPN 授权被拒绝';
        return;
      }
      engine = await NativeEngine.start(
        relay: s.relay,
        sessionId: s.sessionId,
        token: s.token,
        dns: config?.dns ?? '1.1.1.1',
        mtu: config?.mtu ?? 1400,
        splitMode: splitMode,
        splitApps: splitApps,
        capture: capture,
      );
      _everStarted = true;
      if (engine.running) _reconnectTries = 0;
      _startPolling();
    } catch (e) {
      error = _msg(e);
    } finally {
      _setBusy(false);
    }
  }

  Future<void> stopEngine() async {
    _userStopped = true;
    _reconnectTries = 0;
    try {
      engine = await NativeEngine.stop();
    } catch (_) {}
    _ticker?.cancel();
    _ticker = null;
    notifyListeners();
  }

  /// 改分流模式。切到 apps 且没有任何选中应用时清空为待选状态，
  /// 由界面引导用户去挑。
  Future<void> setSplitMode(SplitMode mode) async {
    if (splitMode == mode) return;
    splitMode = mode;
    notifyListeners();
    await store.saveSplitMode(mode.wire);
  }

  Future<void> setSplitApps(List<String> packages) async {
    // 去重并保持用户勾选顺序，方便界面上回显
    final seen = <String>{};
    final out = <String>[];
    for (final p in packages) {
      if (p.isEmpty || !seen.add(p)) continue;
      out.add(p);
    }
    splitApps = out;
    notifyListeners();
    await store.saveSplitApps(out);
  }

  Future<void> toggleAutoReconnect(bool v) async {
    autoReconnect = v;
    if (v) _reconnectTries = 0;
    notifyListeners();
  }

  Future<void> _loadSplitPrefs() async {
    try {
      final mode = await store.readSplitMode();
      splitMode = mode == 'all' ? SplitMode.all : SplitMode.apps;
      splitApps = await store.readSplitApps();
    } catch (_) {
      // 安全存储不可用（比如极少数机型 Keystore 异常）就退回默认值
      splitMode = SplitMode.apps;
      splitApps = const [];
    }
  }

  Future<void> toggleCapture() async {
    if (capture.active) {
      await stopEngine();
    } else {
      await startEngine(capture: true);
    }
  }

  Future<void> _afterAuth() async {
    stage = ShellStage.ready;
    notifyListeners();
    try {
      config = await api.clientConfig();
    } catch (_) {}
    await refreshRooms();
    _startHeartbeat();
    _connectStateStream();
    notifyListeners();
  }

  void _startHeartbeat() {
    _heartbeat?.cancel();
    _heartbeat = Timer.periodic(const Duration(seconds: 60), (_) async {
      try {
        final info = await api.heartbeat();
        card = info;
        remainingSeconds = info.remainingSeconds;
        notifyListeners();
      } catch (e) {
        // 401（token 失效）/ 402（到期）/ 403（封禁）→ 强制回登录页
        final code = e is ZfApiException ? e.statusCode : 0;
        if (code == 401 || code == 402 || code == 403) {
          await _forceLogout(e is ZfApiException ? e.message : '登录状态失效');
        }
      }
    });
  }

  Future<void> _forceLogout(String reason) async {
    if (_forced) return;
    _forced = true;
    await stopEngine();
    await store.clear();
    _teardown();
    _heartbeat?.cancel();
    api.token = null;
    card = null;
    remainingSeconds = 0;
    error = reason;
    stage = ShellStage.auth;
    notifyListeners();
  }

  void _startPolling() {
    _ticker?.cancel();
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) async {
      try {
        final prev = engine;
        engine = await NativeEngine.status();
        if (engine.running) {
          capture = await NativeEngine.captureStatus();
          _pushRtt(engine.latencyMs);
        } else if (prev.running) {
          // 刚从 running 掉下来：记一个丢包点，曲线才有断口
          _pushRtt(-1);
        }
        notifyListeners();
        _maybeReconnect(prev);
      } catch (_) {}
    });
  }

  /// 维持固定长度的环形缓冲。满了就丢最老的，图上永远是最近一分钟。
  void _pushRtt(int ms) {
    rttSamples.add(ms < 0 ? -1 : ms);
    while (rttSamples.length > rttCapacity) {
      rttSamples.removeAt(0);
    }
  }

  /// 掉线自动重连。
  ///
  /// 触发条件全部满足才重连：曾经跑起来过、不是用户主动停的、开关打开、
  /// 上一次是 running 而现在 terminated、没在重连中、没超过次数上限。
  /// 重连走 [_reconnect] 而不是直接 startEngine，避免把 busy 状态推到界面上
  /// 让按钮闪。
  void _maybeReconnect(EngineStatus prev) {
    if (!_everStarted || _userStopped || !autoReconnect) return;
    if (!prev.running || !engine.terminated) return;
    if (_reconnecting) return;
    if (_reconnectTries >= _maxReconnect) {
      // 明确告诉用户放弃了，否则界面只会停在 ERROR 上不明所以
      if (error == null) {
        error = '隧道已断开，自动重连 ${_maxReconnect} 次均失败';
      }
      return;
    }
    _reconnecting = true;
    _reconnectTries++;
    // 短暂延时：立刻重连大概率还是同一个会话被服务端踢掉的状态
    Future<void>.delayed(const Duration(seconds: 3), _reconnect);
  }

  Future<void> _reconnect() async {
    try {
      if (_userStopped || !authed) return;
      // 会话票据是一次性的，重连必须重新 join 拿新票据，
      // 否则 relay 侧会当成重放直接拒掉。
      final cur = session;
      if (cur == null) return;
      try {
        session = await api.joinById(cur.roomId);
      } catch (_) {
        // 重新 join 失败就保留旧票据再试一次，有些部署允许同票据重连
      }
      final s = session;
      if (s == null) return;
      engine = await NativeEngine.start(
        relay: s.relay,
        sessionId: s.sessionId,
        token: s.token,
        dns: config?.dns ?? '1.1.1.1',
        mtu: config?.mtu ?? 1400,
        splitMode: splitMode,
        splitApps: splitApps,
        capture: capture.active,
      );
      if (engine.running) {
        _reconnectTries = 0;
        error = null;
        _startPolling();
        notifyListeners();
      }
    } catch (_) {
      // 失败不在这里报错，让下一轮轮询继续判定
    } finally {
      _reconnecting = false;
    }
  }

  Future<void> _connectStateStream() async {
    _teardown();
    try {
      final url = await api.stateStreamUrl();
      if (url.isEmpty) return;
      final ch = WebSocketChannel.connect(Uri.parse(url));
      _stateSocket = ch;
      _stateSub = ch.stream.listen(
        (frame) {
          try {
            final v = jsonDecode('$frame');
            if (v is Map<String, dynamic>) {
              serverState = v;
              final rem = (v['remaining_seconds'] as num?)?.toInt();
              if (rem != null) {
                remainingSeconds = rem;
                notifyListeners();
              }
            }
          } catch (_) {}
        },
        onError: (_) {},
        cancelOnError: false,
      );
    } catch (_) {
      // 状态流不可用不阻塞主流程
    }
  }

  void _teardown() {
    _stateSub?.cancel();
    _stateSub = null;
    _stateSocket?.sink.close();
    _stateSocket = null;
  }

  void _setBusy(bool v) {
    busy = v;
    if (v) error = null;
    notifyListeners();
  }

  String _msg(Object e) => e is ZfApiException ? '${e.code}: ${e.message}' : e.toString();

  @override
  void dispose() {
    _ticker?.cancel();
    _heartbeat?.cancel();
    _teardown();
    super.dispose();
  }
}
