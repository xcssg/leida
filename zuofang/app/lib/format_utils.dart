/// 展示层格式化工具。
///
/// 之前这些函数是 `_HomePageState` 的 static 方法，拆页面之后要给多个
/// 文件用，抽成顶层函数最省事 —— 它们不依赖任何实例状态。
library;

/// 字节数转人类可读。保留两位小数在 GB 级别有用，KB 级别一位就够。
String fmtBytes(int b) {
  if (b < 0) return '-';
  if (b < 1024) return '$b B';
  if (b < 1024 * 1024) return '${(b / 1024).toStringAsFixed(1)} KB';
  if (b < 1024 * 1024 * 1024) return '${(b / 1048576).toStringAsFixed(1)} MB';
  return '${(b / 1073741824).toStringAsFixed(2)} GB';
}

/// yyyy-MM-dd HH:mm。不引入 intl，少一个依赖少一份包体。
String fmtTime(DateTime t) {
  String p(int v) => v.toString().padLeft(2, '0');
  return '${t.year}-${p(t.month)}-${p(t.day)} ${p(t.hour)}:${p(t.minute)}';
}

/// 长 id 折中显示：`abcdef…1234`。
///
/// 会话 id 是 20 位十进制 u64，完整显示会把面板撑爆，只留头尾足够区分。
String shortId(String id) =>
    id.length <= 10 ? id : '${id.substring(0, 6)}…${id.substring(id.length - 4)}';

/// 剩余秒数转「x 天 / y 小时」。卡密以天计，引擎侧以秒计，两边都要用。
String fmtRemain(int seconds) {
  if (seconds <= 0) return '已到期';
  final d = seconds ~/ 86400;
  if (d > 0) return '$d 天';
  final h = seconds ~/ 3600;
  if (h > 0) return '$h 小时';
  return '${seconds ~/ 60} 分钟';
}

/// 归一化卡密：大写、去掉非字母数字、补 ZF- 前缀、每 4 位加横线。
///
/// 用户从微信/QQ 复制粘贴时常带着空格、全角横线、甚至前后有文字，
/// 这里做一次宽松清洗，服务端仍会再校验一遍，所以不必担心放行脏数据。
String normalizeCard(String raw) {
  var v = raw.toUpperCase().replaceAll(RegExp(r'[^A-Z0-9]'), '');
  if (v.startsWith('ZF')) v = v.substring(2);
  if (v.isEmpty) return '';
  final buf = StringBuffer('ZF-');
  for (var i = 0; i < v.length; i++) {
    if (i > 0 && i % 4 == 0) buf.write('-');
    buf.write(v[i]);
  }
  return buf.toString();
}
