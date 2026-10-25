import 'dart:convert';

import 'package:flutter/services.dart';

/// 分流模式。Android 侧据此决定 VpnService 的允许/禁止名单。
enum SplitMode {
  /// 全局：所有应用都走隧道。
  all,

  /// 白名单：只有 [NativeEngine.start] 里传入的包名走隧道。
  apps,
}

extension SplitModeCode on SplitMode {
  String get wire => this == SplitMode.all ? 'all' : 'apps';
}

/// 已安装应用（供分流选择使用）。
class InstalledApp {
  const InstalledApp({required this.packageName, required this.label, required this.system});

  final String packageName;
  final String label;
  final bool system;

  factory InstalledApp.fromJson(Map<String, dynamic> j) => InstalledApp(
        packageName: '${j['package'] ?? ''}',
        label: '${j['label'] ?? j['package'] ?? ''}',
        system: j['system'] == true,
      );
}

/// 与 Kotlin `NativeBridge` 同名同签名的通道封装。
///
/// Kotlin 侧：
/// ```kotlin
/// object NativeBridge {
///     init { System.loadLibrary("zuofang_native") }
///     external fun prepare(service: ZfVpnService, configJson: String): String
///     external fun start(service: ZfVpnService, configJson: String): String
///     external fun status(): String
///     external fun stop(): String
///     external fun captureStatus(): String
/// }
/// ```
class NativeEngine {
  static const MethodChannel _ch = MethodChannel('com.zuofang.app/engine');

  /// 探针：宿主是否已加载 Rust 库。
  static Future<bool> available() async {
    try {
      final r = await _ch.invokeMethod<String>('status');
      return r != null && !r.contains('原生库未加载');
    } on PlatformException {
      return false;
    } on MissingPluginException {
      return false;
    }
  }

  /// 申请 VPN 授权（对应原版 requestCode 771）。
  /// 返回 true 表示用户已授权，可以 start。
  static Future<bool> requestPermission() async {
    final r = await _ch.invokeMethod<bool>('prepare');
    return r ?? false;
  }

  /// 建立 TUN 并把 fd 交给 Rust 引擎。
  ///
  /// [splitApps] 只在 [splitMode] 为 [SplitMode.apps] 时生效；为空表示
  /// 白名单里没有任何应用，Service 会退化成「只代理自身」，等于断开，
  /// 所以调用方必须先确认列表非空。
  static Future<EngineStatus> start({
    required String relay,
    required String sessionId,
    required String token,
    required String dns,
    required int mtu,
    SplitMode splitMode = SplitMode.apps,
    List<String> splitApps = const [],
    bool capture = false,
  }) async {
    final payload = jsonEncode({
      'relay': relay,
      'session_id': sessionId,
      'token': token,
      'dns': dns,
      'mtu': mtu,
      'capture': capture,
      'split_mode': splitMode.wire,
      'split_apps': splitApps,
    });
    final raw = await _ch.invokeMethod<String>('start', {'config': payload});
    return EngineStatus.fromJson(_decode(raw));
  }

  static Future<EngineStatus> status() async {
    final raw = await _ch.invokeMethod<String>('status');
    return EngineStatus.fromJson(_decode(raw));
  }

  static Future<EngineStatus> stop() async {
    final raw = await _ch.invokeMethod<String>('stop');
    return EngineStatus.fromJson(_decode(raw));
  }

  static Future<CaptureStats> captureStatus() async {
    final raw = await _ch.invokeMethod<String>('captureStatus');
    return CaptureStats.fromJson(_decode(raw));
  }

  /// 列出可作为分流目标的应用。宿主没实现该方法时返回空列表，UI 退化成手填包名。
  static Future<List<InstalledApp>> listApps() async {
    try {
      final raw = await _ch.invokeMethod<String>('listApps');
      if (raw == null || raw.isEmpty) return const [];
      final v = jsonDecode(raw);
      if (v is! List) return const [];
      return v
          .whereType<Map<String, dynamic>>()
          .map(InstalledApp.fromJson)
          .where((a) => a.packageName.isNotEmpty)
          .toList(growable: false);
    } on PlatformException {
      return const [];
    } on MissingPluginException {
      return const [];
    }
  }

  static Map<String, dynamic> _decode(String? raw) {
    if (raw == null || raw.isEmpty) return const {};
    try {
      final v = jsonDecode(raw);
      return v is Map<String, dynamic> ? v : const {};
    } catch (_) {
      return {'error': raw};
    }
  }
}

class EngineStatus {
  const EngineStatus({
    required this.state,
    required this.sessionId,
    required this.latencyMs,
    required this.rxBytes,
    required this.txBytes,
    required this.dropped,
    required this.expiresAt,
    required this.reason,
  });

  final String state;
  final String sessionId;
  final int latencyMs;
  final int rxBytes;
  final int txBytes;
  final int dropped;
  final DateTime? expiresAt;
  final String reason;

  bool get running => state == 'running';

  /// 已经终止（失败或主动停止），不会自己回到 running。
  bool get terminated => state == 'error' || state == 'stopped';

  static const empty = EngineStatus(
    state: 'stopped',
    sessionId: '',
    latencyMs: 0,
    rxBytes: 0,
    txBytes: 0,
    dropped: 0,
    expiresAt: null,
    reason: '',
  );

  factory EngineStatus.fromJson(Map<String, dynamic> j) {
    final exp = (j['expires_at'] as num?)?.toInt();
    return EngineStatus(
      state: '${j['state'] ?? j['status'] ?? 'stopped'}',
      sessionId: '${j['session_id'] ?? ''}',
      latencyMs: (j['rtt_us'] as num?)?.toInt() != null
          ? ((j['rtt_us'] as num).toInt() / 1000).round()
          : (j['latency_ms'] as num?)?.toInt() ?? 0,
      rxBytes: (j['rx_bytes'] as num?)?.toInt() ?? 0,
      txBytes: (j['tx_bytes'] as num?)?.toInt() ?? 0,
      dropped: (j['dropped'] as num?)?.toInt() ?? 0,
      expiresAt: exp == null || exp == 0
          ? null
          : DateTime.fromMillisecondsSinceEpoch(exp * 1000).toLocal(),
      reason: '${j['reason'] ?? j['error'] ?? ''}',
    );
  }
}

class CaptureStats {
  const CaptureStats({
    required this.active,
    required this.packets,
    required this.bytes,
    required this.banks,
    required this.expiresAt,
  });

  final bool active;
  final int packets;
  final int bytes;
  final int banks;
  final DateTime? expiresAt;

  static const empty =
      CaptureStats(active: false, packets: 0, bytes: 0, banks: 0, expiresAt: null);

  factory CaptureStats.fromJson(Map<String, dynamic> j) {
    final exp = (j['expires_at'] as num?)?.toInt();
    return CaptureStats(
      active: j['active'] == true,
      packets: (j['packets'] as num?)?.toInt() ?? 0,
      bytes: (j['bytes'] as num?)?.toInt() ?? 0,
      banks: (j['banks'] as num?)?.toInt() ?? 0,
      expiresAt: exp == null || exp == 0
          ? null
          : DateTime.fromMillisecondsSinceEpoch(exp * 1000).toLocal(),
    );
  }
}
