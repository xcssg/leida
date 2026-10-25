import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

/// 服务端返回的业务错误。
class ZfApiException implements Exception {
  ZfApiException(this.statusCode, this.code, this.message);

  final int statusCode;
  final String code;
  final String message;

  @override
  String toString() => 'ZfApiException($statusCode $code: $message)';
}

class RoomInfo {
  RoomInfo({
    required this.id,
    required this.code,
    required this.title,
    required this.online,
    required this.capacity,
    required this.expiresAt,
  });

  final String id;
  final String code;
  final String title;
  final int online;
  final int capacity;
  final DateTime? expiresAt;

  factory RoomInfo.fromJson(Map<String, dynamic> j) => RoomInfo(
        id: '${j['id']}',
        code: '${j['code'] ?? ''}',
        title: '${j['title'] ?? '未命名线路'}',
        online: (j['online'] as num?)?.toInt() ?? 0,
        capacity: (j['capacity'] as num?)?.toInt() ?? 0,
        expiresAt: _ts(j['expires_at']),
      );
}

DateTime? _ts(dynamic v) {
  final n = (v as num?)?.toInt();
  if (n == null || n == 0) return null;
  return DateTime.fromMillisecondsSinceEpoch(n * 1000, isUtc: true).toLocal();
}

/// 卡密视图：服务端 `/api/card/*` 的 `card` 字段。
class CardInfo {
  CardInfo({
    required this.cardKey,
    required this.planDays,
    required this.expiresAt,
    required this.remainingSeconds,
    required this.expired,
    required this.boundDevice,
    required this.note,
    required this.batch,
  });

  final String cardKey;
  final int planDays;
  final DateTime? expiresAt;
  final int remainingSeconds;
  final bool expired;
  final String boundDevice;
  final String note;
  final String batch;

  /// 只显示首尾，避免截图泄露整串卡密。
  String get masked {
    if (cardKey.length <= 12) return cardKey;
    return '${cardKey.substring(0, 11)}****${cardKey.substring(cardKey.length - 4)}';
  }

  factory CardInfo.fromJson(Map<String, dynamic> j) => CardInfo(
        cardKey: '${j['card_key'] ?? ''}',
        planDays: (j['plan_days'] as num?)?.toInt() ?? 0,
        expiresAt: _ts(j['expires_at']),
        remainingSeconds: (j['remaining_seconds'] as num?)?.toInt() ?? 0,
        expired: j['expired'] == true,
        boundDevice: '${j['bound_device'] ?? ''}',
        note: '${j['note'] ?? ''}',
        batch: '${j['batch'] ?? ''}',
      );
}

class JoinResult {
  JoinResult({
    required this.sessionId,
    required this.relay,
    required this.token,
    required this.expiresAt,
    this.roomId = '',
    this.roomCode = '',
    this.roomTitle = '',
  });

  final String sessionId;
  final String relay;
  final String token;
  final DateTime? expiresAt;

  /// 房间信息在这里冗余一份：自动重连时票据已失效，只能靠 roomId 重新 join，
  /// 而 rooms 列表可能刚好被刷新掉。
  final String roomId;
  final String roomCode;
  final String roomTitle;

  factory JoinResult.fromJson(Map<String, dynamic> j) {
    final room = j['room'];
    final r = room is Map ? room : const {};
    return JoinResult(
      sessionId: '${j['session_id']}',
      relay: '${j['relay']}',
      token: '${j['token'] ?? j['ticket'] ?? ''}',
      expiresAt: _ts(j['expires_at']),
      roomId: '${r['id'] ?? ''}',
      roomCode: '${r['code'] ?? ''}',
      roomTitle: '${r['title'] ?? ''}',
    );
  }
}

class ClientConfig {
  ClientConfig({
    required this.mtu,
    required this.dns,
    required this.captureEnabled,
    required this.notice,
  });

  final int mtu;
  final String dns;
  final bool captureEnabled;
  final String notice;

  factory ClientConfig.fromJson(Map<String, dynamic> j) => ClientConfig(
        mtu: (j['mtu'] as num?)?.toInt() ?? 1400,
        dns: '${j['dns'] ?? '1.1.1.1'}',
        captureEnabled: j['capture_enabled'] == true,
        notice: '${j['notice'] ?? ''}',
      );
}

/// 卡密 API 客户端（对应 `server/webapi.py`）。
///
/// 鉴权模型：卡密即账号。`activate` 拿 token，之后所有请求带
/// `X-Card-Token` 与 `X-Device-Token`；token 到期或卡密被封都会 401/403。
class ZfApi {
  ZfApi({required this.baseUrl, http.Client? client})
      : _client = client ?? http.Client();

  final String baseUrl;
  final http.Client _client;

  String? _token;
  String _device = '';

  static const stateStreamUrlPath = '/api/state_stream_url';

  String? get token => _token;
  set token(String? v) => _token = v;

  String get device => _device;

  /// 设备指纹由 SessionStore 生成后注入，服务端用它做单设备绑定。
  set device(String v) => _device = v;

  Map<String, String> _headers() => {
        'content-type': 'application/json; charset=utf-8',
        if (_token != null && _token!.isNotEmpty) 'X-Card-Token': _token!,
        if (_device.isNotEmpty) 'X-Device-Token': _device,
      };

  Uri _u(String path, [Map<String, String>? q]) =>
      Uri.parse('$baseUrl$path').replace(queryParameters: q);

  Future<Map<String, dynamic>> _send(
    String method,
    String path, {
    Map<String, dynamic>? body,
    Map<String, String>? query,
  }) async {
    final req = http.Request(method, _u(path, query))
      ..headers.addAll(_headers());
    if (body != null) req.body = jsonEncode(body);

    final streamed = await _client
        .send(req)
        .timeout(const Duration(seconds: 20), onTimeout: () {
      throw ZfApiException(0, 'timeout', '请求超时，检查网络或服务器地址');
    });
    final resp = await http.Response.fromStream(streamed);
    final text = utf8.decode(resp.bodyBytes, allowMalformed: true);

    Map<String, dynamic> json;
    try {
      final decoded = jsonDecode(text);
      json = decoded is Map<String, dynamic> ? decoded : {'data': decoded};
    } catch (_) {
      throw ZfApiException(resp.statusCode, 'bad_json',
          text.isEmpty ? '空响应' : text.substring(0, text.length.clamp(0, 160)));
    }

    if (resp.statusCode >= 400) {
      throw ZfApiException(
        resp.statusCode,
        '${json['code'] ?? 'http_${resp.statusCode}'}',
        '${json['message'] ?? json['error'] ?? '服务端拒绝'}',
      );
    }
    return json;
  }

  // ---------- 卡密 ----------

  /// 卡密登录/激活。首次使用会把卡密与该设备绑定并开始计时。
  Future<CardInfo> activate(String cardKey) async {
    final j = await _send('POST', '/api/card/activate', body: {
      'card_key': cardKey.trim(),
      'device': _device,
    });
    _token = '${j['token'] ?? j['card_token'] ?? ''}';
    final card = j['card'];
    return card is Map<String, dynamic>
        ? CardInfo.fromJson(card)
        : CardInfo.fromJson(const {});
  }

  /// 当前卡密信息（用于启动时校验 token 是否仍然有效）。
  Future<CardInfo> me() async => CardInfo.fromJson(await _send('GET', '/api/card/me'));

  /// 心跳，同时刷新服务端 last_seen 并返回最新卡密信息。
  Future<CardInfo> heartbeat() async =>
      CardInfo.fromJson(await _send('POST', '/api/card/heartbeat', body: const {}));

  /// 续期：用一张未使用的卡密给当前卡密叠加时长。
  Future<({int addedSeconds, int days, CardInfo card})> renew(String newCardKey) async {
    final j = await _send('POST', '/api/card/renew', body: {'card_key': newCardKey.trim()});
    final card = j['card'];
    return (
      addedSeconds: (j['added_seconds'] as num?)?.toInt() ?? 0,
      days: (j['days'] as num?)?.toInt() ?? 0,
      card: card is Map<String, dynamic>
          ? CardInfo.fromJson(card)
          : CardInfo.fromJson(const {}),
    );
  }

  Future<void> logout() async {
    try {
      await _send('POST', '/api/card/logout', body: const {});
    } finally {
      _token = null;
    }
  }

  // ---------- 配置 / 线路 ----------

  Future<ClientConfig> clientConfig() async =>
      ClientConfig.fromJson(await _send('GET', '/api/client-config'));

  Future<List<RoomInfo>> rooms() async {
    final j = await _send('GET', '/api/rooms');
    final list = (j['rooms'] as List?) ?? const [];
    return list
        .whereType<Map<String, dynamic>>()
        .map(RoomInfo.fromJson)
        .toList(growable: false);
  }

  Future<JoinResult> join(String roomCode) async =>
      JoinResult.fromJson(await _send('POST', '/api/join', body: {
        'room_code': roomCode,
      }));

  Future<JoinResult> joinById(String roomId) async =>
      JoinResult.fromJson(await _send('POST', '/api/rooms/$roomId/join', body: const {}));

  Future<Map<String, dynamic>> state() => _send('GET', '/api/state');

  Future<String> stateStreamUrl() async {
    final j = await _send('GET', stateStreamUrlPath);
    return '${j['url'] ?? ''}';
  }

  void close() => _client.close();
}
