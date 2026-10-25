#!/usr/bin/env python3
"""端到端联调：发卡 → 激活 → join → 握手 → 真实 UDP 回声 → 封禁踢人 → 限流。

这个测试起真进程内的 HTTP + Relay，不 mock 任何东西：
  * HTTP 走真实 TCP 请求
  * 隧道包走真实 UDP 到 relay 端口
  * 出网走 netstack 的真实 socket，打一个本地 echo 服务器

所以它同时验证了三件旧版做错的事：
  1. 隧道真的能出网（回程数据能回到客户端）
  2. 没有票据的手握不进（白嫖被堵）
  3. 封禁能立刻踢掉在线的会话

运行：
  python3 tests_e2e.py
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))

# conf 在 import 时就固化了环境变量，所以必须先设再导入
_TMP = tempfile.mkdtemp(prefix="zf-e2e-")
os.environ["ZF_DB"] = os.path.join(_TMP, "e2e.db")
os.environ["ZF_SECRET_FILE"] = os.path.join(_TMP, "secret")
os.environ["ZF_NET_MODE"] = "off"            # 强制用户态，沙箱里没 /dev/net/tun
os.environ["ZF_ALLOW_PRIVATE"] = "1"         # 允许打 127.0.0.1
os.environ["ZF_VERBOSE"] = "0"
os.environ["ZF_ADMIN_KEY"] = "test-admin-key"
os.environ["ZF_RATE_ACTIVATE"] = "50"        # 限流单独做单元断言，别干扰主流程
os.environ["ZF_RATE_WINDOW"] = "60"
os.environ["ZF_SESSION_CHECK"] = "1"         # 让状态复查快一点

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cards as C
import conf
import proto
import relayd
import store
import webapi as httpd
import ws

PASS = []
FAIL = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  ok   %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL %s %s" % (name, extra))


# ---------------------------------------------------------------- echo 服务

class EchoServer(object):
    """本地 UDP 回声服务器，代表"外网目标"。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.3)
        self.port = self.sock.getsockname()[1]
        self.got = []
        self.alive = True
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while self.alive:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            self.got.append(data)
            # 原样回，并加个后缀好断言
            try:
                self.sock.sendto(data + b"|echo", addr)
            except OSError:
                return

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- HTTP 客户端

class Api(object):
    def __init__(self, base):
        self.base = base

    def call(self, path, body=None, headers=None, method=None):
        data = None
        hdrs = {"content-type": "application/json"}
        if headers:
            hdrs.update(headers)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs,
                                     method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw}


# ---------------------------------------------------------------- 隧道客户端

class Tunnel(object):
    """一个最小的 TGCP 客户端，只实现测试需要的那部分。"""

    def __init__(self, relay_host, relay_port, session_id, ticket, virtual_ip):
        self.relay = (relay_host, relay_port)
        self.session_id = int(session_id)
        self.ticket = ticket
        self.virtual_ip = virtual_ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(3.0)
        self.keys = None
        self.seq = 0
        self.win = proto.ReplayWindow()

    def handshake(self):
        sk = os.urandom(32)
        pub = proto.x25519_base(sk)
        nonce = os.urandom(32)
        tb = self.ticket.encode("utf-8")
        hello = (pub + nonce + self.session_id.to_bytes(8, "big")
                 + bytes([len(tb)]) + tb)
        self.sock.sendto(hello, self.relay)
        data, _ = self.sock.recvfrom(2048)
        if len(data) != proto.HELLO_S2C_LEN:
            return None, "hello 长度 %d" % len(data)
        status = data[0]
        if status != proto.HS_OK:
            return None, "status=%d" % status
        server_pub = data[1:33]
        server_nonce = data[33:65]
        sid = int.from_bytes(data[65:73], "big")
        if sid != self.session_id:
            return None, "session mismatch"
        shared = proto.x25519(sk, server_pub)
        self.keys = proto.derive_keys(shared, nonce, server_nonce, self.session_id)
        return data[73:81], None

    def send_ip(self, pkt):
        self.seq += 1
        p = proto.pack(self.session_id, proto.DIR_C2S, 0, self.seq, self.keys[0], pkt)
        self.sock.sendto(p, self.relay)

    def recv_ip(self, timeout=3.0):
        """返回 (flags, payload)；超时或坏包返回 (None, None)。"""
        old = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            data, _ = self.sock.recvfrom(65535)
        except socket.timeout:
            return None, None
        finally:
            self.sock.settimeout(old)
        h = proto.parse_header(data)
        if h is None or h["dir"] != proto.DIR_S2C:
            return None, None
        if not self.win.check_and_set(h["seq"]):
            return None, None
        pt = proto.open_(self.keys[1], h["seq"], proto.DIR_S2C, h["ct"])
        return h["flags"], pt

    def recv_data(self, timeout=3.0):
        """只取数据帧，跳过控制帧。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            flags, pt = self.recv_ip(max(0.1, deadline - time.time()))
            if flags is None:
                return None
            if flags & proto.FLAG_CONTROL:
                continue
            return pt
        return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def udp_probe(src_ip, dst_ip, dport, sport, payload):
    """构造一个客户端侧发出的 IPv4+UDP 包。"""
    return proto.build_udp(src_ip, dst_ip, sport, dport, payload)


# ---------------------------------------------------------------- 主流程

def main():
    print("准备环境 %s" % _TMP)
    store.init()
    store.ensure_rooms()

    # ---- 发卡 ----
    print("\n[1] 发卡")
    keys = store.create_cards(5, 30, batch="e2e")
    check("发卡 5 张", len(keys) == 5)
    check("卡密格式合法", all(C.check_format(k, conf.SECRET) for k in keys))
    check("卡密长度 22", all(len(k) == 22 for k in keys))
    check("卡密不重复", len(set(keys)) == 5)

    # ---- 起服务 ----
    print("\n[2] 启动中继 + HTTP")
    relay = relayd.Relay(listen=("127.0.0.1", 0))
    relay.start()
    relay_port = relay.sock.getsockname()[1]
    check("中继模式 userspace", relay.mode == "userspace", relay.mode)

    hub = ws.Hub(relay=relay).start()
    srv = httpd.make_server(relay=relay, hub=hub, host="127.0.0.1", port=0)
    http_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    api = Api("http://127.0.0.1:%d" % http_port)
    check("HTTP 就绪", http_port > 0)

    # ---- 健康检查 ----
    st, r = api.call("/api/health")
    check("GET /api/health 200", st == 200, str(st))
    check("健康检查带 mode", r.get("mode") == "userspace", str(r.get("mode")))

    # ---- 激活 ----
    print("\n[3] 激活卡密")
    dev = "device-aaa-111"
    st, r = api.call("/api/card/activate", {"card_key": keys[0], "device": dev})
    check("激活 200", st == 200, str(r))
    token = r.get("token") or ""
    check("拿到 token", bool(token))
    check("激活后状态生效", (r.get("card") or {}).get("status") == 1,
          str(r.get("card")))

    st, r2 = api.call("/api/card/activate", {"card_key": keys[0], "device": "other-dev"})
    check("换设备激活被拒", st == 409 and r2.get("code") == "device_mismatch",
          "%s %s" % (st, r2.get("code")))

    st, r2 = api.call("/api/card/activate", {"card_key": "ZF-AAAA-BBBB-CCCC-DDDD",
                                            "device": dev})
    check("伪造卡密被拒", st in (400, 404), "%s %s" % (st, r2.get("code")))
    check("伪造卡密错误码正确", r2.get("code") == "bad_format", str(r2.get("code")))

    # ---- 未授权访问 ----
    print("\n[4] 鉴权边界")
    st, r = api.call("/api/card/me")
    check("无 token 401", st == 401, str(st))
    st, r = api.call("/api/card/me", headers={"X-Card-Token": "bogus"})
    check("假 token 401", st == 401, str(st))

    hdr = {"X-Card-Token": token, "X-Device-Token": dev}
    st, r = api.call("/api/card/me", headers=hdr)
    check("正确 token 200", st == 200, str(st))
    check("卡密剩余时间 > 0", (r.get("remaining_seconds") or 0) > 0)

    st, r = api.call("/api/card/me", headers={"X-Card-Token": token,
                                             "X-Device-Token": "wrong"})
    check("设备不符 409", st == 409, str(st))

    # ---- 线路 ----
    print("\n[5] 线路与 join")
    st, r = api.call("/api/rooms", headers=hdr)
    rooms = r.get("rooms") or []
    check("线路列表非空", len(rooms) >= 1, str(len(rooms)))
    code = rooms[0]["code"]

    st, j = api.call("/api/join", {"room_code": code}, headers=hdr)
    check("join 200", st == 200, str(j))
    ticket = j.get("ticket") or ""
    sid = j.get("session_id") or ""
    check("拿到 ticket", bool(ticket))
    check("拿到 session_id", bool(sid))
    check("session_id 是纯数字", sid.isdigit(), sid)

    st, j2 = api.call("/api/join", {"room_code": "ZZZZZZ"}, headers=hdr)
    check("错线路码 404", st == 404, str(st))

    # ---- 握手 ----
    print("\n[6] TGCP 握手")
    tun = Tunnel("127.0.0.1", relay_port, sid, ticket, "10.7.0.9")
    exp, e = tun.handshake()
    check("握手成功", exp is not None, str(e))
    check("会话已登记", len(relay.session_list()) == 1,
          str(len(relay.session_list())))

    # 票据只能用一次：换个 key 重放同一 ticket
    print("\n[7] 票据重放防护")
    bad = Tunnel("127.0.0.1", relay_port, sid, ticket, "10.7.0.10")
    # 换个 session_id 才会走 redeem 分支（同 sid 会被会话替换逻辑吃掉）
    bad.session_id = (int(sid) + 1) & 0xFFFFFFFFFFFFFFFF
    exp2, e2 = bad.handshake()
    check("重放同票据被拒", exp2 is None, "居然成功了")
    bad.close()

    # 空白嫖：随便填个 token
    print("\n[8] 无票据白嫖")
    free = Tunnel("127.0.0.1", relay_port, sid, "freedom-token-123", "10.7.0.11")
    exp3, e3 = free.handshake()
    check("无票据握手被拒", exp3 is None, "居然成功了")
    free.close()

    # ---- 真实出网 ----
    print("\n[9] 真实 UDP 出网")
    echo = EchoServer()
    payload = b"zuofang-e2e-%d" % int(time.time())
    pkt = udp_probe(tun.virtual_ip, "127.0.0.1", echo.port, 40000, payload)
    tun.send_ip(pkt)

    back = tun.recv_data(3.0)
    check("收到回程 IP 包", back is not None)
    if back is not None:
        ip = proto.parse_ipv4_packet(back)
        check("回程是 IPv4", ip is not None)
        if ip:
            check("回程目的地址是虚拟 IP", ip["dst"] == tun.virtual_ip, ip["dst"])
            check("回程协议 UDP", ip["proto"] == 17)
            u = proto.parse_udp(ip["payload"])
            check("回程 UDP 可解析", u is not None)
            if u:
                check("回程源端口正确", u["sport"] == echo.port,
                      "%d vs %d" % (u["sport"], echo.port))
                check("回程目的端口正确", u["dport"] == 40000, str(u["dport"]))
                check("回程数据一致", u["data"] == payload + b"|echo", repr(u["data"]))
    check("echo 服务器确实收到", payload in echo.got, str(echo.got))

    # 第二条流复用同一个 forwarder
    payload2 = b"second-flow"
    tun.send_ip(udp_probe(tun.virtual_ip, "127.0.0.1", echo.port, 40001, payload2))
    back2 = tun.recv_data(3.0)
    check("第二条流也能通", back2 is not None)
    check("只有一条会话（流复用）", len(relay.session_list()) == 1)

    # ---- 分片 / 非 UDP ----
    print("\n[10] 异常包处理")
    frag = bytearray(udp_probe(tun.virtual_ip, "127.0.0.1", echo.port, 40002, b"x"))
    frag[6] |= 0x20  # MF 位
    tun.send_ip(bytes(frag))
    check("分片包被丢", tun.recv_data(0.8) is None)

    tcp = bytearray(udp_probe(tun.virtual_ip, "127.0.0.1", echo.port, 40003, b"y"))
    tcp[9] = 6  # 改成 TCP
    tun.send_ip(bytes(tcp))
    check("非 UDP 被丢", tun.recv_data(0.8) is None)

    # ---- 统计 ----
    print("\n[11] 统计接口")
    st, r = api.call("/admin/stats", headers={"X-Admin-Key": conf.ADMIN_KEY})
    check("管理统计 200", st == 200, str(st))
    check("统计有会话数", (r.get("relay") or {}).get("sessions") == 1,
          str((r.get("relay") or {}).get("sessions")))
    check("统计有收字节", (r.get("relay") or {}).get("rx_bytes", 0) > 0)

    st, r = api.call("/admin/stats")
    check("无管理密钥 401", st == 401, str(st))

    # ---- 封禁踢人 ----
    print("\n[12] 封禁即时踢人")
    st, r = api.call("/admin/cards/ban", {"card_key": keys[0]},
                     headers={"X-Admin-Key": conf.ADMIN_KEY})
    check("封禁 200", st == 200, str(st))
    check("踢掉 1 个会话", r.get("kicked") == 1, str(r.get("kicked")))

    time.sleep(0.3)
    check("会话表已清空", len(relay.session_list()) == 0,
          str(len(relay.session_list())))

    tun.send_ip(udp_probe(tun.virtual_ip, "127.0.0.1", echo.port, 40004, b"after-ban"))
    check("封禁后发包无回程", tun.recv_data(0.8) is None)

    st, r = api.call("/api/card/me", headers=hdr)
    check("封禁后 token 失效", st == 403, str(st))

    st, r = api.call("/api/card/activate", {"card_key": keys[0], "device": dev})
    check("封禁卡不能激活", st == 403, str(st))

    # 解封后能重新登录
    st, r = api.call("/admin/cards/unban", {"card_key": keys[0]},
                     headers={"X-Admin-Key": conf.ADMIN_KEY})
    check("解封 200", st == 200, str(st))
    st, r = api.call("/api/card/activate", {"card_key": keys[0], "device": dev})
    check("解封后可再激活", st == 200, str(st))

    # ---- 续期 ----
    print("\n[13] 续期")
    hdr = {"X-Card-Token": r.get("token"), "X-Device-Token": dev}
    before = (r.get("card") or {}).get("expires_at") or 0
    st, x = api.call("/api/card/renew", {"card_key": keys[1]}, headers=hdr)
    check("续期 200", st == 200, str(x))
    check("续期加了 30 天", x.get("days") == 30, str(x.get("days")))
    check("到期时间往后推", (x.get("expires_at") or 0) > before)
    check("续期卡被标记已用", (store.get_card(keys[1])["redeemed_by"] or "") == keys[0])

    st, x2 = api.call("/api/card/renew", {"card_key": keys[1]}, headers=hdr)
    check("同一张卡不能用两次", st in (400, 409), "%s %s" % (st, x2.get("code")))

    st, x3 = api.call("/api/card/renew", {"card_key": keys[0]}, headers=hdr)
    check("不能给自己续期", st in (400, 409), "%s %s" % (st, x3.get("code")))

    # ---- 限流 ----
    # 全局 ACTIVATE_LIMIT 调到 50 是为了不干扰主流程，这里直接对 RateLimiter
    # 做单元断言，验证「窗口内超额拒绝」和「坏格式累计拉黑」两条逻辑。
    print("\n[14] 限流")
    rl = httpd.RateLimiter(limit=2, window=60, bad_limit=3, bad_ban=600)
    r1 = rl.allow("1.1.1.1")
    r2 = rl.allow("1.1.1.1")
    r3 = rl.allow("1.1.1.1")
    check("前两次放行", r1 and r2, "%s %s" % (r1, r2))
    check("第三次超限拒绝", not r3, str(r3))
    check("其他 IP 不受影响", rl.allow("2.2.2.2"), "")

    banned = [rl.mark_bad("3.3.3.3") for _ in range(3)]
    check("坏格式未达阈值不拉黑", not any(banned[:2]), str(banned))
    check("坏格式达阈值拉黑", banned[-1], str(banned))
    check("被拉黑后一律拒绝", not rl.allow("3.3.3.3"), "")

    st, r = api.call("/api/card/activate",
                     {"card_key": "ZF-BAD1-BBBB-CCCC-DDDD", "device": "x"})
    check("坏卡密返回 400/404", st in (400, 404), str(st))

    # ---- WebSocket ----
    print("\n[15] WebSocket 升级")
    st, r = api.call("/api/state/stream?token=nope")
    check("WS 无 token 被拒", st == 401, str(st))

    st, r = api.call("/api/state", headers=hdr)
    check("状态接口 200", st == 200, str(st))
    check("状态含 relay 信息", "relay_stats" in r)

    # ---- 收尾 ----
    tun.close()
    echo.close()
    relay.close()
    hub.close()
    try:
        srv.shutdown()
    except Exception:
        pass
    try:
        srv.server_close()
    except Exception:
        pass

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAILED: %s" % f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
