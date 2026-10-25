# TGCP v1 —— 隧道协议规格

> 用于 `app/rust/`（客户端引擎）与 `server/`（Python 中继）之间的 UDP 传输。
> 一次握手换一对会话密钥，之后每个包独立加密 + 独立认证，无重传、无连接状态机。
>
> 原 APK 的 TGCP 只从字符串池还原出语义骨架（hello / direction / size limit / bank / heartbeat），
> 二进制布局、DH 参数与 AEAD 算法均不可考。本协议是**功能等价的自研重写**，
> 目标是与自己的中继互通，不承诺与原版互通。
>
> 三方实现必须逐字节一致：
> - `server/proto.py`（Python 中继与测试）
> - `app/rust/src/proto.rs`（客户端）
> - `server/tests_align.py`（用 Rust 侧 `cargo run --example vect` 生成的向量做断言）

---

## 1. 数据包

所有整数**大端**。

```
 0                1                2                3
+--------+--------+--------+--------+--------+--------+--------+--------+
| 'T'    | 'G'    | 'C'    | 'P'    |  ver   |  dir   |    flags(U16)   |
+--------+--------+--------+--------+--------+--------+--------+--------+
|                        session_id (U64)                               |
+-----------------------------------------------------------------------+
|                             seq (U64)                                 |
+-----------------------------------------------------------------------+
|           len (U16)           |                                       |
+-------------------------------+---------------------------------------+
|                            ciphertext ...                             |
+-----------------------------------------------------------------------+
```

| 字段 | 长度 | 说明 |
|---|---|---|
| `magic` | 4 | 固定 `TGCP` |
| `ver` | 1 | 固定 `1` |
| `dir` | 1 | `0x01` = C→S，`0x02` = S→C |
| `flags` | 2 | bit0 = 控制帧，bit1 = 抓包帧，其余保留为 0 |
| `session_id` | 8 | 由 `join` 下发的十进制串转成 u64 |
| `seq` | 8 | 单向单调递增，从 1 开始，每条会话每方向独立计数 |
| `len` | 2 | 密文长度 = 明文长度 + 16（tag） |

明文头固定 **26 字节**，`MAX_PAYLOAD = 1424`（对应 TUN MTU 1400）。超长包直接丢弃，不分片。

## 2. 握手

握手包**不带** TGCP 头，靠长度区分：首 4 字节不是 `TGCP` 且长度 ≥ 73 的一律当 hello。

```
C -> S  HELLO  73 + tlen 字节
        client_pub(32) || client_nonce(32) || session_id(8) || tlen(1) || ticket(tlen UTF-8)

S -> C  HELLO  固定 81 字节
        status(1) || server_pub(32) || server_nonce(32) || session_id(8) || expires_at(8)
```

- 曲线：X25519（RFC 7748），公钥 32 字节**小端**
- `tlen` 上限 120（`MAX_TICKET_LEN`），票据来自 `POST /api/join`
- `expires_at` 是卡密到期时间（Unix 秒），失败应答里为 0
- 握手超时 5 秒；`X25519` 结果全零视为无效（低阶点攻击）直接拒绝

### 2.1 状态码

| 值 | 名称 | 含义 |
|---|---|---|
| 0 | `HS_OK` | 成功，后续可发数据包 |
| 1 | `HS_REJECTED` | 结构错 / DH 失败 / TUN 地址池耗尽 |
| 2 | `HS_EXPIRED` | 票据过期 或 卡密到期 |
| 3 | `HS_BANNED` | 卡密被封禁 |
| 4 | `HS_BAD_TICKET` | 票据不存在 或 已被使用 |

失败也必须回满 81 字节，否则客户端会把应答当坏包静默丢掉，
表现为「一直转圈」而不是「明确的错误码」。

### 2.2 密钥派生

```
shared = X25519(server_sk, client_pub)          // == X25519(client_sk, server_pub)
prk    = HMAC-SHA256(key = client_nonce || server_nonce, msg = shared)
k_c2s  = HMAC-SHA256(key = prk, msg = b"tgcp c2s" || session_id_be8)
k_s2c  = HMAC-SHA256(key = prk, msg = b"tgcp s2c" || session_id_be8)
```

服务端每次 hello 都生成新的临时密钥对（`server_sk = os.urandom(32)`），
不复用、不落盘，所以没有前向安全棘轮也能保证单次会话密钥不复发。

## 3. 数据面加密

不做完整 AEAD，用标准原语组合成 Encrypt-then-MAC：

```
nonce = seq_be8 || dir(1B) || 0x00 0x00 0x00              // 12 字节，固定
ks    = HMAC(k, nonce || LE32(0)) || HMAC(k, nonce || LE32(1)) || ...
                                                          // 拼接后取前 pt_len 字节
ct    = pt XOR ks
tag   = HMAC(k, nonce || ct)[0:16]
wire  = ct || tag
```

解密顺序：**先验 tag（`hmac.compare_digest`），再解 ks**。校验失败直接丢包，
不回任何错误包 —— 不给探测者任何「这个端口活着」的信号。

> nonce 必须是正好 12 字节。早期写成 9 字节会算出完全不同的 keystream，
> 两端表现为「握手成功但数据全是乱码」。

## 4. 重放防护

`seq` 用 RFC 6479 风格滑动窗口，位图实现，窗口 256（必须是 2 的幂）。

- `seq` 落在窗口外且 ≤ 已见最大值 → 判定重放，丢弃
- 窗口内且对应位已置 → 判定重放，丢弃
- 窗口整体右移时，旧位清零

窗口 256 是 UDP 乱序容忍度与内存的折中：太小会把正常乱序杀掉，太大会让攻击者
有更大的重放空间。单条会话 RTT 通常 < 100 ms、速率 < 1000 pkt/s，256 足够。

## 5. 控制帧（flags bit0）

`payload` 为 UTF-8 JSON，无长度前缀：

```json
{"t":"ping","ts":1700000000000}
{"t":"pong","ts":1700000000000}
{"t":"config","mtu":1400,"dns":"1.1.1.1","mode":"userspace"}
{"t":"bye","reason":"user"}
{"t":"kick","reason":"card_banned"}
{"t":"ack","bank":7}
```

- 客户端每 10 s 发一次 `ping`，中继回 `pong`，客户端据此算 RTT
- 中继 30 s 收不到任何包判 `idle timeout`，回收会话
- 中继收到未知 `t` 一律回 `config`，相当于「能力探测」的应答
- `kick` 是服务端主动下发：封禁、到期、管理员踢人都会先发它再断会话

## 6. 数据帧（flags 为 0）

`payload` 直接是完整的 IPv4 包（从版本/IHL 字节开始），没有额外封装头。

- 客户端从 TUN 读出的包原样塞进来
- 中继回程用 `proto.build_udp()` 重新构包，源 IP 是虚拟地址，目的 IP 是客户端虚拟地址
- 只解析 `proto` / `src` / `dst` / 分片位，其余字段透传

## 7. 抓包帧（flags bit1）

`payload = JSON 头长度(2B) || JSON 头 || 原始 IP 包`

```json
{"bank":7,"last":1,"ts":1700000000000,"proto":"ip"}
```

- 每个 bank 最多 64 帧，`last=1` 表示该 bank 结束
- 中继收到 `last=1` 后回 `{"t":"ack","bank":N}`
- 未 ACK 的 bank 超过 8 个时丢弃最旧的
- 抓包只在客户端 `capture=true` 时启用，纯观测用途，不影响转发路径

## 8. 出网双模式

中继把数据帧送出公网有两条路径，由 `ZF_NET_MODE` 决定：

### 8.1 `tun`（需要 root / CAP_NET_ADMIN）

```
TunRouter 创建 zftun0 (10.7.0.1/16)
  ├── hello 成功时从地址池分配虚拟 IP（10.7.x.y），attach 到会话
  ├── 客户端来的 IPv4 包直接 write 进 TUN，由内核做 NAT 出网
  └── TUN 读线程读回程包，按目的虚拟 IP 找回会话，加密后发回
```

支持 TCP / UDP / ICMP，是完整隧道。沙箱与容器通常没有 `/dev/net/tun`，起不来就回落。

### 8.2 `userspace`（免 root）

```
每个会话一个 UDPForwarder
  ├── 收到 IPv4+UDP → 解析 (dst_ip, dst_port)，查/建 flow
  ├── flow = 一条普通 UDP socket，绑随机本地端口，connect 到目标
  ├── 每个 flow 一个读线程，回包按虚拟地址封 IPv4+UDP 回隧道
  └── 流空闲 60 s 回收，单会话上限 ZF_UDP_FLOW_MAX（默认 256）
```

**只支持 UDP**。TCP 需要自己实现三次握手与拥塞控制，不在本协议范围内 ——
用 TCP 的游戏或应用必须走 `tun` 模式。

默认挡掉内网/回环/保留地址（`netstack._blocked`），避免卡密用户拿隧道扫服务器所在内网；
需要放行（比如游戏服务器在内网）就设 `ZF_ALLOW_PRIVATE=1`。

## 9. 票据生命周期

这是整套授权最关键的环节，也是旧实现漏掉的部分。

```
1. 客户端 POST /api/join
      ↓  服务端签发票据（TICKET_TTL 默认 120 s）
   tickets 表插入一行 (ticket, card_key, device, room_id, expires_at, used_at NULL)

2. 客户端把 ticket 塞进 HELLO 的尾部
      ↓  中继查库核销
   BEGIN IMMEDIATE
   查不到            → HS_BAD_TICKET
   used_at 非空      → HS_BAD_TICKET（重放）
   expires_at <= now → HS_EXPIRED
   卡封禁            → HS_BANNED
   卡到期            → HS_EXPIRED
   否则 UPDATE used_at = now 并 COMMIT

3. 票据一次性，握手成功即作废
```

**重连必须重新 `join`**。客户端自动重连如果沿用旧票据，会稳定收到 `HS_BAD_TICKET`，
被自己的会话防重放挡住 —— 这是调试时最容易踩的坑。

### 9.1 hello 重传缓存

UDP 会丢包，客户端重发 hello 时 pub / nonce / ticket 完全不变。
中继用 `ticket -> (addr, client_pub, client_nonce, 应答字节, 时间戳)` 做 30 秒缓存：

- 命中（pub、nonce 都相同且 30 s 内）→ 原样回同一份 81 字节应答，**不重复核销票据**
- 未命中 → 正常走核销流程

没有这层缓存的话，第一个 hello 的应答丢了，客户端重发就会被判「票据已用」，
握手永远失败。

## 10. 会话 id

`session_id` 必须两端一致且跨重连稳定，否则中继无法把新会话替换掉旧会话。

```
session_id = FNV-1a64("card_key|device|room_id")
下发形式   = 十进制字符串
```

为什么是十进制：Rust 侧 `Config::session_u64()` 先 `parse::<u64>()`，成功就直接采用；
下发十六进制会解析失败再哈希一遍，服务端日志里的 id 与客户端实际使用的就对不上了。

## 11. 与 HTTP 层的接口

```
POST /api/join               {"room_code":"ABC234"}  -> {session_id, relay, ticket, ticket_expires_at, room}
POST /api/rooms/<id>/join    {}                      -> 同上，按 id 而不是 code
GET  /api/client-config                              -> {mtu, dns, mode, relay, protocol}
```

`relay` 是 `udp://host:port` 形式。`mode` 告诉客户端服务端当前是 `tun` 还是 `userspace`，
客户端可据此提示用户「当前模式只支持 UDP 应用」。
