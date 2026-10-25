# ZUOFANG

自建卡密授权 + 安卓客户端 + UDP 中继的完整一套。

由原 EVA 客户端（逆向结论见 [analysis/apk_analysis.md](analysis/apk_analysis.md)）全量改名、
重写授权层与传输层得到。**授权、中继、出网全部在你自己的机器上**，客户端不依赖任何第三方账号体系。

- 隧道协议规格：[PROTOCOL.md](PROTOCOL.md)

---

## 1. 架构

```
┌────────────────────┐   HTTPS    ┌──────────────────────────────────────┐
│  Android 客户端     │ ─────────► │  server/main.py（单进程）             │
│  Flutter + Kotlin  │            │                                      │
│  + Rust 引擎        │            │  ├── HTTP  卡密 API / 管理台 / WS     │
│                    │            │  ├── UDP   TGCP 中继（握手/加解密）    │
└─────────┬──────────┘            │  ├── 出网   tun  或 userspace        │
          │  UDP (TGCP v1)        │  └── SQLite 卡密/票据/会话/审计       │
          └──────────────────────►└──────────────────────────────────────┘
```

**为什么合成一个进程**：封禁必须立刻生效。拆成「卡密服务 + 中继」两个进程时，
中继拿不到卡状态，只能靠轮询或额外消息通道，慢且容易漂。
同进程共享 SQLite 和内存里的会话表，管理员点一下封禁就能把在线会话踢干净。

**授权链路**：`卡密 → activate → token → join → 一次性票据 → 握手成功`
票据是唯一入网凭证，一次性、2 分钟有效、绑定卡密+设备+线路。

---

## 2. 目录

```
zuofang/
├── server/                     # 服务端（Python3 标准库，零第三方依赖）
│   ├── main.py                 # 统一入口：HTTP + UDP 同进程 + 5 个子命令
│   ├── conf.py                 # 全部配置（环境变量）
│   ├── cards.py                # 卡密编解码 + 校验码
│   ├── store.py                # SQLite：卡密/token/票据/房间/审计
│   ├── proto.py                # TGCP v1：X25519 / 密钥派生 / 加解密 / 防重放
│   ├── netstack.py             # 用户态转发（免 root，UDP）
│   ├── tunmode.py              # TUN 模式（root，全协议）
│   ├── relayd.py               # UDP 中继：会话管理 + 票据核销 + 状态复查
│   ├── webapi.py               # HTTP：卡密 API + 管理接口
│   ├── ws.py                   # WebSocket 状态推送
│   ├── admin.html              # 单文件管理台
│   └── tests_*.py              # 对齐测试 / 转发测试 / 端到端测试
├── app/                        # 安卓客户端
│   ├── lib/                    # Dart：19 个文件，见 §6
│   ├── android/app/            # Kotlin：MainActivity / ZfVpnService / NativeBridge
│   └── rust/                   # Rust 引擎 → libzuofang_native.so（arm64-v8a）
├── tools/tgcp_client.py        # 协议自测客户端（不依赖 App）
├── build.sh                    # 电脑上一键出 APK
└── .github/workflows/build-apk.yml   # 手机上用 GitHub Actions 出 APK
```

---

## 3. 快速开始

### 3.1 服务端（3 条命令）

```bash
cd zuofang/server

# 1) 初始化库（自动建 3 条线路）
python3 main.py init

# 2) 发 5 张 30 天的卡
python3 main.py cards 5 30

# 3) 起服务（HTTP 8090 + UDP 9000）
export ZF_ADMIN_KEY='换成你自己的管理密钥'
export ZF_PUBLIC_RELAY='你的公网IP:9000'
python3 main.py
```

启动后打印管理台地址，浏览器打开即可发卡 / 封禁 / 看审计。

### 3.2 客户端

```bash
# 电脑
./build.sh 你的服务器IP:8090

# 手机（无需电脑）
# 源码推 GitHub → Actions → build-zuofang-apk → Run workflow → 填地址 → 下 Artifacts
```

详见 §7。

---

## 4. 服务端

### 4.1 子命令

```
python3 main.py                    启动服务（HTTP + UDP + 维护线程）
python3 main.py cards <数量> <天数> [--batch 名称] [--note 备注]
python3 main.py init               只建库不启动
python3 main.py info               打印统计
python3 main.py secret             打印卡密密钥指纹（核对用了哪把密钥）
```

`cards` 示例：

```
$ python3 main.py cards 3 30 --batch vip
已生成 3 张，有效期 30 天，批次 vip
----------------------------------
ZF-K7M2-QX4N-8RTP-BHCV
ZF-WD9F-2LMP-T6XK-NQ8J
ZF-3JHV-R8NQ-PW4M-TZ6D
----------------------------------
数据库: /path/to/server/zuofang.db
```

### 4.2 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ZF_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `ZF_PORT` | `8090` | HTTP 端口 |
| `ZF_RELAY_LISTEN` | `0.0.0.0:9000` | UDP 中继监听 |
| `ZF_PUBLIC_RELAY` | 空 | 下发给客户端的 relay 地址；**跨机/公网部署必填** |
| `ZF_PUBLIC_BASE` | 空 | 反代后的外网地址，用于拼 WebSocket URL |
| `ZF_DB` | `server/zuofang.db` | SQLite 路径 |
| `ZF_SECRET` | 落盘 `server/.zf_secret` | 卡密校验码密钥（**上线后不可更换**） |
| `ZF_ADMIN_KEY` | 每次启动随机 | 管理台密钥 |
| `ZF_TOKEN_DAYS` | `30` | token 有效期 |
| `ZF_TICKET_TTL` | `120` | join 票据有效期（秒） |
| `ZF_SESSION_IDLE` | `120` | UDP 会话空闲回收（秒） |
| `ZF_SESSION_CHECK` | `15` | 会话状态复查周期（秒），封禁靠它踢人 |
| `ZF_MAX_DEVICES` | `1` | 单卡最多绑定设备数 |
| `ZF_NET_MODE` | `auto` | `auto` / `on` / `off`，见 §4.6 |
| `ZF_ALLOW_PRIVATE` | `0` | 是否放行内网目标 |
| `ZF_UDP_FLOW_MAX` | `256` | 用户态模式单会话并发流上限 |
| `ZF_MTU` / `ZF_DNS` | `1400` / `1.1.1.1` | 下发给客户端的参数 |
| `ZF_RATE_ACTIVATE` | `8` | 激活接口每 IP 每窗口次数 |
| `ZF_RATE_WINDOW` | `60` | 限流窗口（秒） |
| `ZF_BAD_FORMAT_LIMIT` | `20` | 格式错多少次拉黑 |
| `ZF_BAD_FORMAT_BAN` | `300` | 拉黑时长（秒） |
| `ZF_DEFAULT_ROOMS` | `3` | 首次启动自动建几条线路 |
| `ZF_VERBOSE` | `0` | 单行请求日志 |

### 4.3 客户端 API

鉴权头：`X-Card-Token: <token>` 与 `X-Device-Token: <设备指纹>`。

```
POST /api/card/activate   {"card_key","device"}   -> {token, card, relay}
GET  /api/card/me                                 -> 卡密视图
POST /api/card/heartbeat                          -> 卡密视图（顺带续 token 活跃时间）
POST /api/card/renew      {"card_key"}            -> {days, added_seconds, card}
POST /api/card/logout
GET  /api/rooms                                   -> {rooms:[{id,code,title,online,capacity}]}
POST /api/join            {"room_code"}           -> {session_id, relay, ticket, ticket_expires_at, room}
POST /api/rooms/<id>/join {}                      -> 同上，按 id
GET  /api/client-config                           -> {mtu, dns, mode, relay, protocol}
GET  /api/state                                   -> 在线数 / 中继统计
GET  /api/state_stream_url                        -> ws://.../api/state/stream?token=
WS   /api/state/stream?token=                     -> 5 秒一帧 tick
GET  /api/health | /api/docs | /
```

错误码（HTTP 状态 + `code` 字段）：

| HTTP | code | 含义 |
|---|---|---|
| 400 | `missing_card` / `bad_format` | 没填 / 格式或校验码错（**不查库**） |
| 403 | `card_banned` | 已封禁 |
| 404 | `invalid_card` / `room_not_found` | 卡不存在 / 线路不存在 |
| 409 | `device_mismatch` / `card_used` | 绑了别的设备 / 已用于续期 |
| 402 | `card_expired` / `subscription_expired` | 到期 |
| 401 | `unauthorized` | token 无效、过期或已吊销 |
| 409 | `room_full` | 线路满 |
| 429 | `rate_limited` / `ip_banned` | 太频繁 / 已临时拉黑 |

### 4.4 管理 API

```
GET  /admin                              单文件控制台（?admin_key= 或 X-Admin-Key）
GET  /admin/stats | /admin/cards | /admin/audit | /admin/rooms
POST /admin/cards/generate   {"count","days","batch","note"}
POST /admin/cards/ban | unban | unbind | reset | delete | extend | tokens   {"card_key"}
```

`ban` 会同时吊销该卡全部 token、踢掉全部在线 UDP 会话、推 WS `kick` 帧 —— 三处一起断。

### 4.5 表结构

```
cards         card_key PK, plan_days, created_at, activated_at, expires_at,
              status(0未激活/1正常/2封禁), batch, note, redeemed_by
card_devices  (card_key, device) PK, first_seen, last_seen, ua
tokens        token PK, card_key, device, created_at, last_seen, expires_at, revoked
tickets       ticket PK, card_key, device, room_id, created_at, expires_at, used_at
rooms         id PK, code UNIQUE, title, capacity, online, created_at
audit         id, ts, action, card_key, device, ip, detail
```

### 4.6 出网模式

`ZF_NET_MODE` 三档：

| 值 | 行为 |
|---|---|
| `auto`（默认） | 先试 TUN，起不来回落 userspace，日志留原因 |
| `on` | 强制 TUN，起不来直接报错退出（避免「以为在跑其实是半残」） |
| `off` | 只用 userspace |

- **tun**：需要 root / `CAP_NET_ADMIN` / `/dev/net/tun`。创建 `zftun0 (10.7.0.1/16)`，
  给每个会话分配虚拟 IP，包写进 TUN 由内核 NAT 出网。支持 TCP/UDP/ICMP，是完整隧道。
- **userspace**：免 root。每条会话一套 UDP 流表，用普通 socket 做 NAT。
  **只支持 UDP** —— TCP 要自己实现握手与拥塞控制，不在本协议范围内。
  用 TCP 的应用必须走 tun。

### 4.7 测试

```bash
cd server
python3 tests_align.py      # proto.py 与 Rust 侧逐字节对齐
python3 tests_netstack.py   # 用户态转发：本地 UDP 回声
python3 tests_e2e.py        # 全链路：发卡→激活→join→握手→真实出网→封禁踢人→限流
```

`tests_e2e.py` 起真进程、走真 TCP/UDP、打真 echo server，不 mock 任何东西。
当前 **70 项全过**。

---

## 5. 授权模型（为什么这样设计）

### 5.1 卡密

```
格式  ZF-XXXX-XXXX-XXXX-CCCC          （22 字符）
字符集 ABCDEFGHJKLMNPQRSTUVWXYZ23456789（去掉 0/O/1/I）
校验码 HMAC-SHA256(secret, body12) 取前 4 字节，各取低 5 bit 映射到字符集
随机体 12 字符 × 5 bit = 60 bit
```

校验码的意义：服务端先本地算校验码，**格式错直接 400，永不查库**。
这样即使接口被爆破，每次尝试的边际成本也不是一次 SQL。60 bit 随机体保证真卡猜不中。

### 5.2 票据（关键）

```
join  →  票据（一次性 / 120 s / 绑定 card_key+device+room_id）
握手  →  中继查库核销，UPDATE used_at，重放即拒
```

这是旧实现最大的漏洞所在：`--allow-token` 只校验 token 非空，填任意字符串就能白嫖。
现在握手必须出示服务端签发且未使用的票据，票据只能通过带 token 的 `join` 获取。

**注意**：票据一次性，所以客户端重连必须重新 `join`。
自动重连沿用旧票据会稳定收到 `HS_BAD_TICKET` —— 详见 [PROTOCOL.md](PROTOCOL.md) §9。

### 5.3 hello 重传缓存

UDP 丢包时客户端会原样重发 hello（同一 pub/nonce/ticket）。
中继对 `ticket → 应答字节` 做 30 秒缓存，命中就直接重发同一份应答，不重复核销票据。
没有这层缓存，第一个应答一丢，握手就永久失败。

---

## 6. 客户端

### 6.1 改造点

| 层 | 改动 |
|---|---|
| 包名 | `cn.eva.eva_client` → `com.zuofang.app` |
| 应用名 | `EVA Rust` → `ZUOFANG` |
| Kotlin | `EvaVpnService` → `ZfVpnService`；`NativeBridge` 库名 `zuofang_native` |
| JNI | `Java_com_zuofang_app_NativeBridge_{prepare,start,status,stop,captureStatus,listApps}` |
| Dart | `Eva*` → `Zf*`；`eva_widgets.dart` → `zf_widgets.dart` |
| 授权 | 账号/密码/验证码 → **卡密直登 + 卡密续期** |
| 存储 | `flutter_secure_storage`，键前缀 `zuofang.` |

后端地址**编译期注入**（`--dart-define=ZF_API=...`），App 内没有地址输入框。

### 6.2 Dart 文件职责

```
main.dart          入口 + 全局状态装配
app_shell.dart     ChangeNotifier：会话 / 引擎 / RTT 缓冲 / 自动重连 / 分流
api.dart           HTTP 客户端（含 JoinResult 冗余 roomId，供重连 re-join）
auth_page.dart     卡密登录
home_page.dart     组装层（139 行，只做刷新与分发）
session_card.dart  剩余天数 + 4 指标 / EnginePanel 明细
link_panel.dart    雷达 + RTT 曲线
room_panel.dart    线路选择
split_panel.dart   分流双模式 + 应用多选 + 自动重连开关
capture_panel.dart 抓包统计
renew_panel.dart   卡密续期
account_sheet.dart 账号信息底部弹层
rtt_chart.dart     60 秒 RTT 示波器（-1 表示丢包断口）
radar.dart         雷达图
native_engine.dart JNI 封装 + SplitMode + InstalledApp
session_store.dart secure storage（token / 卡密 / 设备 / 分流偏好）
format_utils.dart  字节/时间/短 id/卡密规范化
zf_widgets.dart    通用组件
app_theme.dart     配色
```

### 6.3 客户端独有能力

**分流双模式**

| 模式 | 行为 |
|---|---|
| `all` | 全部应用走隧道 |
| `apps` | 只让白名单走（`addAllowedApplication`），**强制包含自身包名**，否则 VPN 起不来 |

白名单存在 `flutter_secure_storage`，下次启动直接沿用。
白名单为空时 `startEngine()` 会直接拦下来报「请先挑选应用」，
而不是让隧道空跑一遍让人误判成网络问题。

**断线自动重连**

触发条件必须同时满足：曾经启动过（`_everStarted`）、不是用户主动停（`_userStopped`）、
开关打开（`autoReconnect`）、上一轮 `running` 而本轮 `terminated`。
最多 3 次，间隔 3 秒，每次**重新 `join` 换新票据**再握手。
失败不覆盖已有错误文案，避免「重连失败」把真正的失败原因冲掉。

**RTT 60 秒曲线**

每秒采样一次，容量 60，从 running 掉下来的那一秒推 `-1`（画成断口）。
三重颜色分级：正常 / 波动 / 异常。

**抓包**

`capture=true` 时把 TUN 读到的包镜像到中继（bank + ACK），纯观测，不影响转发。

---

## 7. 打包 APK

### 7.1 电脑（推荐）

```bash
cd zuofang
chmod +x build.sh
./build.sh 1.2.3.4:8090
```

流程：检查工具链 → `rustup target add aarch64-linux-android` → `cargo ndk build --release`
→ `flutter pub get` → `flutter build apk --release --dart-define=ZF_API=...`

产物：`app/build/app/outputs/flutter-apk/app-release.apk`

### 7.2 手机（GitHub Actions，零电脑）

1. 把整个 `zuofang/` 推到 GitHub 仓库。
2. 手机浏览器打开仓库 → `Actions` → 左侧 `build-zuofang-apk` → `Run workflow`。
3. `api_base` 填 `http://你的服务器IP:8090`。
4. 跑完点进 run，页面底部 `Artifacts` → 下载 `zuofang-apk.zip` → 解压安装。

workflow 固定 Flutter 3.24.5 / JDK 17 / NDK 27.0.12077973 / Rust stable，
与 `app/android/app/build.gradle` 的 `ndkVersion` 对齐。

### 7.3 手机（Termux，不推荐但可行）

至少 6 GB 存储、4 GB 内存，首次装 SDK 要很久。

```bash
pkg update && pkg install -y openjdk-17 rust python git wget unzip
cd ~ && wget https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.24.5-stable.tar.xz
tar xf flutter_linux_3.24.5-stable.tar.xz && export PATH="$HOME/flutter/bin:$PATH"
mkdir -p ~/android-sdk/cmdline-tools && cd ~/android-sdk/cmdline-tools
wget https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
unzip commandlinetools-linux-*.zip && mv cmdline-tools latest
export ANDROID_HOME="$HOME/android-sdk"
yes | $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager --licenses
$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager "platforms;android-35" "build-tools;35.0.0" "ndk;27.0.12077973"
rustup target add aarch64-linux-android && cargo install cargo-ndk --locked
cd ~/zuofang/app/rust && cargo ndk -t arm64-v8a -o ../android/app/src/main/jniLibs build --release
cd ~/zuofang/app && flutter pub get
flutter build apk --release --dart-define=ZF_API=http://你的服务器IP:8090
```

> Termux 里 Gradle 守护进程容易被杀，建议 `termux-wake-lock` 并关电池优化；
> 内存不足时在 `app/android/gradle.properties` 加 `org.gradle.jvmargs=-Xmx1536m`。

### 7.4 签名

release 默认用 debug 签名，方便直接装。要正式签名：

```bash
keytool -genkeypair -v -keystore zuofang.jks -alias zuofang \
        -keyalg RSA -keysize 2048 -validity 10000
```

把 `zuofang.jks` 放进 `app/android/app/`，在 `build.gradle` 加：

```gradle
signingConfigs {
    release {
        storeFile file("zuofang.jks")
        storePassword System.getenv("ZF_STORE_PASS")
        keyAlias "zuofang"
        keyPassword System.getenv("ZF_KEY_PASS")
    }
}
buildTypes { release { signingConfig signingConfigs.release } }
```

换签名后必须卸载旧包再装，否则 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`。

---

## 8. 验证记录

### 8.1 服务端（本容器实测）

| 测试 | 项数 | 结果 |
|---|---|---|
| `tests_align.py` | 密钥派生 / 加密 / 头 / 防重放，与 Rust 向量逐字节比对 | ALL PASS |
| `tests_netstack.py` | IPv4 解析 / UDP 解析建包 / 地址过滤 / 流回收 | ALL PASS |
| `tests_e2e.py` | 发卡→激活→鉴权边界→join→握手→票据重放→无票据白嫖→真实 UDP 出网→异常包→统计→封禁踢人→续期→限流→WS | **70/70 ALL PASS** |

`tests_e2e.py` 覆盖的三件旧版做错的事：

1. 隧道真的能出网（回程数据能回到客户端）
2. 没有票据的握手进不来（白嫖被堵）
3. 封禁能立刻踢掉在线会话

### 8.2 客户端

Dart 侧用 `gen_kernel_aot` 对 `lib/main.dart` 做全量 kernel 编译（等价完整类型检查），
零错误零警告。`flutter analyze` 在本容器不可用（沙箱禁写 `/root/.dartServer`，且 2 GB 内存下
分析服务器会崩），故改用该方式验证。

**未在本环境验证**：`cargo ndk` 交叉编译、`flutter build apk`（无 Flutter/Rust/Android SDK
完整工具链）、TUN 模式（无 `/dev/net/tun`）、真机运行。

---

## 9. 已知限制

- **userspace 模式只走 UDP**。TCP 应用必须用 tun 模式（需要 root）。
- 单条 UDP 载荷上限 1424 字节（MTU 1400），大包不分片，无 PMTU 探测。
- 卡密库是单机 SQLite。多实例部署要换 Postgres/MySQL 并加分布式锁；
  本架构的「同进程共享状态」前提也就不成立了。
- 自动重连上限 3 次。连续被服务端拒绝（如卡到期）时不会无限重试。
- `secrets.token_urlsafe` 生成的 token 存在 SQLite 明文，数据库文件权限要收紧。

---

## 10. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 登录报 `bad_format` | 卡密校验码错（可能是 `ZF_SECRET` 变了） | `python3 main.py secret` 核对指纹 |
| 握手一直转圈 | 中继没起 / 防火墙挡 UDP / `ZF_PUBLIC_RELAY` 填了 `127.0.0.1` | 看中继日志有没有 `hello` 行 |
| 握手成功但数据不通 | `ZF_NET_MODE=userspace` 且应用走 TCP | 换 tun 模式，或换成 UDP 应用 |
| 重连稳定失败 `HS_BAD_TICKET` | 沿用了已核销的旧票据 | 重连前必须重新 `join`（客户端已实现） |
| 客户端拿到 `relay=udp://127.0.0.1:9000` | 没设 `ZF_PUBLIC_RELAY` | 设成客户端可达的 IP:端口 |
| 封禁后客户端还能上网 | `ZF_SESSION_CHECK` 太大 | 默认 15 s；调小可更快踢人 |
| `TUN 不可用，回落用户态转发` | 容器/沙箱无 `/dev/net/tun` | 正常，userspace 模式可跑 UDP |
