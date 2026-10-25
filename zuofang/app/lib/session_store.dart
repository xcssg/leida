import 'dart:convert';
import 'dart:math';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// 卡密 token / 卡密原文 / 设备指纹的本地持久化。
/// 走 flutter_secure_storage（Android Keystore + EncryptedSharedPreferences）。
class SessionStore {
  SessionStore({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
            );

  static const _kToken = 'zuofang.card_token';
  static const _kCard = 'zuofang.card_key';
  static const _kDevice = 'zuofang.device_token';
  static const _kSplitMode = 'zuofang.split_mode';
  static const _kSplitApps = 'zuofang.split_apps';

  final FlutterSecureStorage _storage;

  Future<String?> readToken() => _storage.read(key: _kToken);
  Future<String?> readCard() => _storage.read(key: _kCard);

  /// 分流模式只有 all / apps 两种取值，其余一律当 apps。
  Future<String> readSplitMode() async =>
      (await _storage.read(key: _kSplitMode)) == 'all' ? 'all' : 'apps';

  Future<void> saveSplitMode(String mode) =>
      _storage.write(key: _kSplitMode, value: mode == 'all' ? 'all' : 'apps');

  /// 白名单以换行分隔存成单个字符串，避免再引入一套 JSON 序列化。
  Future<List<String>> readSplitApps() async {
    final raw = await _storage.read(key: _kSplitApps);
    if (raw == null || raw.isEmpty) return const [];
    return raw
        .split('\n')
        .map((e) => e.trim())
        .where((e) => e.isNotEmpty)
        .toList(growable: false);
  }

  Future<void> saveSplitApps(List<String> apps) => _storage.write(
        key: _kSplitApps,
        value: apps.join('\n'),
      );

  Future<void> saveSession(String token, String cardKey) async {
    await _storage.write(key: _kToken, value: token);
    await _storage.write(key: _kCard, value: cardKey);
  }

  Future<void> clear() async {
    await _storage.delete(key: _kToken);
    await _storage.delete(key: _kCard);
  }

  /// 设备指纹只生成一次，用于服务端单设备绑定。
  Future<String> deviceToken() async {
    final existing = await _storage.read(key: _kDevice);
    if (existing != null && existing.isNotEmpty) return existing;
    final rnd = Random.secure();
    final bytes = List<int>.generate(24, (_) => rnd.nextInt(256));
    final token = base64Url.encode(bytes).replaceAll('=', '');
    await _storage.write(key: _kDevice, value: token);
    return token;
  }
}
