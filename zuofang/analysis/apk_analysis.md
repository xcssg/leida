# ZUOFANG (com.zuofang.app) APK 逆向分析报告

> 样本：`8896368e-..._attachment.zip`（实为 APK，20,166,022 字节）
> 工具链：自研纯 Python 解析器（`_work/axml.py` 解析二进制 AndroidManifest；`_work/dexdump.py` 解析 DEX 并反汇编）
> 环境无 aapt / apktool / jadx / pip，全部手工实现

---

## 1. 样本基本画像

| 项 | 值 |
|---|---|
| 包名 | `com.zuofang.app` |
| 应用名 | `ZUOFANG` |
| versionName / versionCode | `0.4.2` / `19` |
| minSdk / targetSdk / compileSdk | 29 / 36 / 36 |
| 构建链 | Flutter（Dart AOT）+ AGP 9.1.0 + Kotlin + Rust(NDK) |
| 体积构成 | `libflutter.so` 11.7 MB、`libapp.so` 5.3 MB、`libzuofang_native.so` 1.5 MB、`classes.dex` 856 KB |
| 目标游戏 | `com.tencent.tmgp.dfm`（三角洲行动），在 `<queries>` 中显式声明 |

`lib/` 下只有 `arm64-v8a`，无 armeabi-v7a / x86_64：只面向 64 位真机。

---

## 2. 组件与权限（Manifest 反编译结果）

```
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_SPECIAL_USE"/>
<uses-permission android:name="android.permission.POST_NOTIFICATIONS"/>

<service android:name="com.zuofang.app.ZfVpnService"
         android:permission="android.permission.BIND_VPN_SERVICE"
         android:exported="false"
         android:foregroundServiceType="0x40000000">   <!-- specialUse -->
  <property android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
            android:value="Local game traffic forwarding and user-requested packet capture"/>
</service>

<activity android:name="com.zuofang.app.MainActivity"
          android:exported="true"
          android:launchMode="singleTop"
          android:theme="@style/...NormalTheme"/>
```

结论：**没有 `QUERY_ALL_PACKAGES`、没有无障碍服务、没有悬浮窗、没有 root**。整个 App 的能力面就是一条本地 VPN 隧道 + 一个 Flutter UI。

---

## 3. Java 层（classes.dex）

### 3.1 R8 混淆强度

```
"r8-mode": "full"
pg-map-id 存在，但 APK 内不含 mapping.txt
```

- 包名保留（`com.zuofang.app`），业务类名被压缩：`Lmb;`、`Lyc;`、`La0;`、`Lb0;` 等。
- `Lmb;` 经反汇编确认为 Flutter 的 `FlutterActivity` R8 重命名体：字符串常量含 `FlutterActivity`、`cached_engine_id`、`dart_entrypoint`、`background_mode`、`destroy_engine_with_activity`、`enable_state_restoration`、`io.flutter.embedding.android.NormalTheme`。
- `Lyc;` 为 MethodChannel / EventChannel 封装，`MainActivity.onDestroy` 中回调 `Lyc.a("closed","页面已关闭",...)`。

### 3.2 全部业务代码只有一个 JNI 桥

```java
public final class NativeBridge {
    static { System.loadLibrary("zuofang_native"); }
    static native String prepare(ZfVpnService svc, String jsonConfig);
    static native String start(ZfVpnService svc, String jsonConfig);
    static native String status();
    static native String stop();
}
```

### 3.3 ZfVpnService 状态机（反汇编还原）

```
字段：a=AtomicBoolean 运行标志  b=单线程 Executor  d=锁  e=ParcelFileDescriptor(TUN)
      f=status(static)  g=最后原因(static)

onStartCommand(intent):
  if ("STOP".equals(action)) { stop("用户停止"); return START_NOT_STICKY; }
  if (f == "starting" || f == "running") return START_NOT_STICKY;   // 幂等防重入
  建立通知渠道 "zuofang-capture" / "ZUOFANG 采集"
  PendingIntent#1 -> MainActivity，PendingIntent#2 -> action="STOP"
  startForeground(NOTIF_ID, notification, FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
  b.execute(() -> {  // 后台线程
      ParcelFileDescriptor pfd = VpnService.Builder(...)
            .addAddress / addRoute / addDnsServer / setMtu ...
            .establish();          // 典型 TUN 配置
      NativeBridge.start(this, configJson);   // 把 fd 交给 Rust
  })

onRevoke()  -> stop("VPN 授权已撤销")
onDestroy() -> stop("服务销毁")
stop(reason):
  if (!a.compareAndSet(true,false)) return;
  synchronized(d){ NativeBridge.stop(); if (e!=null) e.close(); e=null; }
  g = reason; f = "stopped";
```

`MainActivity.onActivityResult`：

```java
if (requestCode == 771) {           // VpnService.prepare() 的请求码
    if (resultCode == RESULT_OK)
        startForegroundService(new Intent(this, ZfVpnService.class)
                                 .putExtra("config", g));  // Dart 侧下发的 JSON 配置
}
```

**Java 层不含任何协议/加密/业务逻辑**，只负责：申请 VPN 授权 → 建立 TUN → 把 fd 与 JSON 配置交给 Rust。

---

## 4. 原生层 libzuofang_native.so（Rust）

### 4.1 导出符号

```
Java_com_zuofang_app_NativeBridge_prepare
Java_com_zuofang_app_NativeBridge_start
Java_com_zuofang_app_NativeBridge_status
Java_com_zuofang_app_NativeBridge_stop
eva_start / eva_status / eva_stop / eva_string_free
eva_hotspot_enable / eva_hotspot_status
```

段表：`.text` 0xFE054、`.rodata` 0x19D08、dynsym 仅 94 项 → 静态链接为主，符号被裁剪。

依赖组件（由字符串与导入表推断）：

| 组件 | 版本/说明 |
|---|---|
| tokio | 1.54.1（多线程 runtime + UDP） |
| serde_json | 1.0.154（JNI 两侧 JSON 交互） |
| bytes | 1.12.1 |
| miniz_oxide | 压缩（抓包数据上行压缩） |
| ipstack | 自带用户态 TCP/IP 栈（TUN 内直接处理 TCP，不必回落到内核） |
| jni | JNI 绑定 crate |

### 4.2 自研传输协议 TGCP（从字符串池还原）

```
握手：expected client hello / waiting server hello / duplicate hello / bad hello
      invalid DH secret / invalid DH peer      -> 基于 DH 的会话密钥协商
      TGCP handshake timeout / TGCP idle timeout
报头：bad TGCP header / TGCP direction / TGCP size limit / TGCP buffer overflow / unaligned body
抓包：invalid capture message / short capture fragment / empty capture fragment
      invalid capture control body / fragmented capture control / capture message too large
      invalid UDP capture header / invalid UDP capture frame
      capture already running / capture already prepared / capture startup cancelled
      capture sequence exhausted / capture session expired / invalid capture token
      capture bank was not acknowledged / capture UDP heartbeat timed out
      too many pending banks / bank mirror queue full
      EVC3 UDP session required
统计：dropped / datagrams / max_queue_us / rtt_us / session_id / token / expires_at / server_time
其它：VPN socket protection failed / eva-forward / Rust runtime / protect
```

还原出的运行模型：

1. **握手**：客户端发 `client hello`（含 DH 公钥）→ 服务端回 `server hello`（含服务端 DH 公钥 + 会话参数）→ 双方算共享密钥，派生 AEAD 密钥；重复 hello、方向错、尺寸越界都直接拒绝。
2. **数据面**：自定义 UDP 报文（TGCP 头：magic + version + direction + session_id + seq + len + AEAD tag），载荷是 TUN 里取出的 IP 包或抓包帧。
3. **抓包**：`capture` 会话带 token 与序号，按 bank（镜像队列）批量上行，需要 ACK，队列满/序号耗尽/心跳超时都会终止会话。
4. **socket 保护**：所有 socket 必须走 `VpnService.protect()`，否则隧道自身流量会回环进 TUN。
5. **指标**：RTT、丢包（dropped）、队列最大延迟（max_queue_us）由 native 侧维护，通过 `status()` 以 JSON 回给 Dart。

### 4.3 是否可"破解"

| 攻击面 | 结论 |
|---|---|
| 去广告 / 解锁 UI 限制 | UI 全在 Dart AOT，逻辑不依赖服务端的部分可改，但收益低 |
| 绕过登录 / 卡密 | 校验在云端（`/api/auth/*` + `redeem`），本地只缓存 token（flutter_secure_storage → Keystore/Tink）。改本地无法获得可用服务端会话 |
| 伪造订阅 / 无限时长 | `expires_at`、`server_time` 由服务端签发，握手时校验；本地改时间无效 |
| 直接复刻服务端 | 需逆向 TGCP 完整二进制布局 + DH 参数 + AEAD 算法；`libapp.so` 是 Dart AOT 快照（无符号表、无源码），`libzuofang_native.so` 是 strip 过的 Rust，恢复成本远高于自己实现 |
| 重打包注入 | 无签名校验代码，可重打包；但改不出可用的服务端授权 |
| 抓包看明文 | 全链路 AEAD + DH，最多看到报文尺寸与节奏 |

**结论：不存在"一键破解"路径。** 真正有价值的是"功能可 100% 仿造"——它做的事情本质上是：

> 用户授权 VPN → 本地 TUN 接管指定 App 的流量 → Rust 侧用自研 UDP 加密协议把 IP 包送到中继 → 中继出网 / 或做流量镜像抓包 → Flutter UI 展示会话、延迟、房间、卡密。

这套架构完全可以用公开技术栈自建。

---

## 5. Flutter 层（libapp.so / Dart AOT）

### 5.1 自有 Dart 文件

```
package:eva_client/main.dart
package:eva_client/api.dart
package:eva_client/app_shell.dart
package:eva_client/app_theme.dart
package:eva_client/zf_widgets.dart
package:eva_client/native_engine.dart
package:eva_client/radar.dart
package:eva_client/session_store.dart
```

### 5.2 类与关键标识

```
ZfApi / ZfApiException / EvaApp / ZfShell / ZfDisclosure / EvaField
ZfMetrics / ZfParagraph / EvaTextController / ZfScrollBehavior
_RadarState / RadarState
authPage / authClient / authChallenge / captcha_id / captcha_answer
room_code / room_id / device_token / isAuthenticated
restoreSession / createSession / stateSocket / latency / expires_at
"State stream connection timed out"
```

### 5.3 接口清单（字符串池提取）

```
POST /api/auth/captcha?purpose=register
POST /api/auth/login
POST /api/auth/logout
GET  /api/auth/me
POST /api/auth/presence
POST /api/auth/redeem           # 卡密兑换
GET  /api/client-config
GET  /api/rooms
GET  /api/rooms/{room_id}
POST /api/join                  # 用 room_code 加入房间
GET  /api/state
GET  /api/state_stream_url      # 返回 WebSocket 地址
POST /capture-sessions          # 申请抓包会话
GET  /capture-status
```

请求头：`X-Account-Token`、`X-ZUOFANG-Capture-Protocol: zuofang.client.session.v1`

本地存储：`flutter_secure_storage`（Android Keystore / Tink 加密），键名含 `device_token`、会话 token。

### 5.4 加密相关符号

```
AES_GCM_NoPadding
RSA_ECB_OAEPwithSHA_256andMGF1Padding     # 疑似握手/敏感字段传输
base64Encode / base64Decode / jsonEncode / jsonDecode / WebSocket
```

---

## 6. 可仿造范围（本次交付）

| 模块 | 原实现 | 仿造实现 |
|---|---|---|
| UI | Flutter + 自定义 Eva* 组件 + radar 动效 | Flutter（`eva_client/lib/`），同风格组件、雷达页、会话页 |
| VPN 壳 | Kotlin ZfVpnService + TUN | 同结构 Kotlin 实现 |
| JNI 桥 | NativeBridge 4 个方法 | 同名同签名的 Kotlin + Rust 实现 |
| 隧道 | Rust/tokio + 自研 TGCP + DH + AEAD | Rust（无 async runtime）+ TGCP v1（X25519 + HMAC 计数器 keystream + HMAC tag）`eva_client/rust/` |
| 后端 | 账号/卡密/房间/状态流 | Python3 标准库 HTTP + WebSocket（`server/`） |
| 中继 | UDP 转发 + 抓包镜像 | Python3 线程 + 单 socket UDP 中继（`relay/`） |

协议细节见 `PROTOCOL.md`，构建方式见 `README.md`。
