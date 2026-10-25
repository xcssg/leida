"""UDP 中继：TGCP 握手 + 会话管理 + 真出网。

这里是整个服务端的核心，解决旧版三个硬伤：
  1. 旧版 `on_ip()` 是空函数，隧道收包后什么也不做 —— 现在真正转发。
  2. 旧版 `--allow-token` 只判空，随便填字符串就能白嫖 —— 现在必须持有
     `join` 签发的一次性票据，握手时查库核销，用过即废。
  3. 旧版卡密服务与中继是两进程，封禁无法即时生效 —— 现在同进程共享 SQLite，
     定时复查卡状态，封禁立刻踢下线。

线程模型：
  recv 线程      —— 收 UDP，做握手/解密/分发
  status 线程    —— 每 SESSION_CHECK 秒复查卡状态，踢掉过期/封禁的会话
  tun 读线程     —— 仅在 TUN 模式存在，见 tunmode.py
  每 flow 读线程 —— 仅用户态转发存在，见 netstack.py

数据通路：
  客户端 TUN 包 --seal(c2s)--> UDP --> 本模块 open --> 
     TUN 模式   : write_packet 到 zftun0 → 内核 NAT 出网
     用户态模式 : 查 flow 表 → 真实 UDP socket 出网
  回程反向，重新 seal(s2c) 回客户端。
"""

import json
import os
import socket
import threading
import time

import conf
import netstack
import proto
import store
import tunmode


def _log(msg):
    if conf.VERBOSE:
        print("[relay] %s" % msg)


class Session(object):
    """一条已建立的隧道会话。"""

    __slots__ = ("relay", "addr", "session_id", "keys", "card_key", "device",
                 "room_id", "expires_at", "created", "last_seen", "mode",
                 "seq_out", "win", "rx_bytes", "tx_bytes", "rx_pkts", "tx_pkts",
                 "dropped", "alive", "lock", "fwd", "vip", "rtt_us")

    def __init__(self, relay, addr, session_id, keys, info, mode):
        self.relay = relay
        self.addr = addr
        self.session_id = session_id
        self.keys = keys
        self.card_key = info["card_key"]
        self.device = info["device"]
        self.room_id = info["room_id"]
        self.expires_at = info["expires_at"]
        self.created = time.time()
        self.last_seen = self.created
        self.mode = mode
        self.seq_out = 0
        self.win = proto.ReplayWindow()
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.rx_pkts = 0
        self.tx_pkts = 0
        self.dropped = 0
        self.alive = True
        self.lock = threading.Lock()
        self.fwd = None
        self.vip = None
        self.rtt_us = 0

    # ------------------------------------------------------------ 发送

    def send_raw(self, pkt):
        return self.relay.sock.sendto(pkt, self.addr) == len(pkt)

    def send_ip_pkt(self, ip_pkt):
        """把一个 IP 包加密后发回客户端。两个出网模式都走这里。"""
        if not self.alive:
            return False
        with self.lock:
            self.seq_out = (self.seq_out + 1) & 0xFFFFFFFFFFFFFFFF
            seq = self.seq_out
        try:
            pkt = proto.pack(self.session_id, proto.DIR_S2C, 0, seq,
                             self.keys[1], ip_pkt)
        except Exception:
            return False
        ok = self.send_raw(pkt)
        if ok:
            self.tx_bytes += len(ip_pkt)
            self.tx_pkts += 1
        else:
            self.dropped += 1
        return ok

    def send_control(self, obj):
        if not self.alive:
            return False
        with self.lock:
            self.seq_out = (self.seq_out + 1) & 0xFFFFFFFFFFFFFFFF
            seq = self.seq_out
        pt = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        pkt = proto.pack(self.session_id, proto.DIR_S2C, proto.FLAG_CONTROL,
                         seq, self.keys[1], pt)
        return self.send_raw(pkt)

    # ------------------------------------------------------------ 收

    def on_payload(self, flags, pt):
        self.last_seen = time.time()
        self.rx_bytes += len(pt)
        self.rx_pkts += 1

        if flags & proto.FLAG_CONTROL:
            self._on_control(pt)
            return

        self._on_ip(pt)

    def _on_control(self, pt):
        try:
            v = json.loads(pt.decode("utf-8"))
        except Exception:
            self.dropped += 1
            return
        t = v.get("t")
        if t == "ping":
            self.send_control({"t": "pong", "ts": v.get("ts", 0)})
        elif t == "bye":
            _log("session bye %s" % (self.addr,))
            self.close("client bye")
        elif t == "ack":
            pass
        else:
            self.send_control({"t": "config", "dns": conf.DNS, "mtu": conf.MTU,
                               "mode": self.mode})

    def _on_ip(self, pkt):
        if self.mode == "tun":
            if self.relay.tun is not None and self.relay.tun.packet_from_client(pkt):
                return
            self.dropped += 1
            return

        # 用户态转发
        if self.fwd is None:
            self.dropped += 1
            return
        r = self.fwd.handle_packet(pkt)
        if r != "ok":
            self.dropped += 1

    # ------------------------------------------------------------ 生命周期

    def close(self, reason=""):
        if not self.alive:
            return
        self.alive = False
        if self.fwd is not None:
            try:
                self.fwd.close()
            except Exception:
                pass
            self.fwd = None
        if self.relay.tun is not None:
            self.relay.tun.detach(self)
        if self.room_id:
            try:
                store.bump_online(self.room_id, -1)
            except Exception:
                pass
        _log("session closed %s reason=%s" % (self.addr, reason or "-"))

    def view(self):
        return {
            "addr": "%s:%d" % self.addr,
            "session_id": "%016x" % self.session_id,
            "card_key": self.card_key,
            "device": self.device,
            "room_id": self.room_id,
            "mode": self.mode,
            "vip": self.vip or "",
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
            "rx_pkts": self.rx_pkts,
            "tx_pkts": self.tx_pkts,
            "dropped": self.dropped,
            "rtt_ms": self.rtt_us // 1000,
            "age": int(time.time() - self.created),
            "idle": int(time.time() - self.last_seen),
        }


class Relay(object):
    """UDP 中继服务。与 HTTP 同进程，由 main.py 启动。"""

    def __init__(self, listen=None):
        host, port = listen or conf.relay_addr()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        self.sock.bind((host, port))
        self.sock.settimeout(0.5)

        self.sessions = {}          # (addr, session_id) -> Session
        # 握手结果缓存：ticket -> (addr, session_id, response, keys, info, exp, ts)
        # 客户端 hello 丢了会重发同一个 ticket，这里原样回同一份应答，
        # 既不用重复核销票据，也不会因为「票据已用」把正常重传送进死路。
        self.hs_cache = {}
        self.lock = threading.RLock()
        self.closed = False
        self.tun = None
        self.mode = "userspace"
        self.started = time.time()
        self.stat_rx_pkts = 0
        self.stat_tx_pkts = 0
        self.stat_bad = 0
        self.stat_hello = 0
        self.stat_reject = 0
        self._threads = []

    # ------------------------------------------------------------ 启动

    def start(self):
        self._init_tun()

        t = threading.Thread(target=self._recv_loop, daemon=True, name="relay-recv")
        t.start()
        self._threads.append(t)

        t = threading.Thread(target=self._status_loop, daemon=True, name="relay-status")
        t.start()
        self._threads.append(t)
        return self

    def _init_tun(self):
        """决定出网模式。auto 下 TUN 起不来就回落，日志里留原因。"""
        if conf.NET_MODE == "off":
            self.mode = "userspace"
            return
        try:
            self.tun = tunmode.TunRouter().start()
            self.mode = "tun"
            _log("出网模式: tun (%s)" % self.tun.pool.cidr)
        except Exception as e:
            self.tun = None
            self.mode = "userspace"
            if conf.NET_MODE == "on":
                raise
            _log("TUN 不可用，回落用户态转发: %s" % e)

    def close(self):
        self.closed = True
        with self.lock:
            for s in list(self.sessions.values()):
                s.close("server shutdown")
            self.sessions.clear()
        if self.tun is not None:
            self.tun.close()
            self.tun = None
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------ 收包主循环

    def _recv_loop(self):
        buf = bytearray(65535)
        while not self.closed:
            try:
                n, addr = self.sock.recvfrom_into(buf)
            except socket.timeout:
                self._sweep()
                continue
            except OSError:
                if self.closed:
                    return
                continue
            if n <= 0:
                continue
            data = bytes(buf[:n])

            # 没有 MAGIC 开头的都当 hello：hello 是明文定长结构
            if len(data) >= proto.HELLO_C2S_FIXED and data[0:4] != proto.MAGIC:
                self.stat_hello += 1
                self._on_hello(addr, data)
                continue

            self._on_packet(addr, data)

    def _on_hello(self, addr, data):
        """握手。票据在这里一次性核销 —— 这是白嫖的唯一入口。"""
        if len(data) < proto.HELLO_C2S_FIXED:
            self.stat_bad += 1
            return

        client_pub = data[0:32]
        client_nonce = data[32:64]
        session_id = int.from_bytes(data[64:72], "big")
        tlen = data[72]
        if tlen > proto.MAX_TICKET_LEN or len(data) < proto.HELLO_C2S_FIXED + tlen:
            self.stat_bad += 1
            self._send_hello(addr, proto.HS_REJECTED, session_id, b"", b"", 0, client_pub, client_nonce)
            return

        ticket = data[proto.HELLO_C2S_FIXED:proto.HELLO_C2S_FIXED + tlen].decode(
            "utf-8", "replace")

        # hello 丢了客户端会原样重发（同一个 pub / nonce / ticket）。
        # 命中缓存就直接回同一份应答，不重复核销票据。
        with self.lock:
            hit = self.hs_cache.get(ticket)
        if hit is not None:
            h_addr, h_pub, h_nonce, h_resp, h_ts = hit
            if (h_pub == client_pub and h_nonce == client_nonce
                    and time.time() - h_ts < 30):
                try:
                    self.sock.sendto(h_resp, addr)
                except OSError:
                    pass
                _log("hello replay %s（同一票据重传）" % (addr,))
                return

        info, err = store.redeem_ticket(ticket)
        if err is not None:
            self.stat_reject += 1
            status = proto.HS_BAD_TICKET
            if err == "card_banned":
                status = proto.HS_BANNED
            elif err == "card_expired" or err == "ticket_expired":
                status = proto.HS_EXPIRED
            _log("hello 拒绝 %s: %s" % (addr, err))
            self._send_hello(addr, status, session_id, b"", b"", 0,
                            client_pub, client_nonce)
            return

        # 服务端临时密钥
        server_sk = os.urandom(32)
        server_pub = proto.x25519_base(server_sk)
        server_nonce = os.urandom(32)

        shared = proto.x25519(server_sk, client_pub)
        if shared == b"\x00" * 32:
            self.stat_reject += 1
            self._send_hello(addr, proto.HS_REJECTED, session_id, b"", b"", 0,
                            client_pub, client_nonce)
            return

        keys = proto.derive_keys(shared, client_nonce, server_nonce, session_id)

        # 同地址同 session_id 的老会话直接替换（客户端重启场景）
        with self.lock:
            old = self.sessions.get((addr, session_id))
            if old is not None:
                old.close("re-handshake")
                self.sessions.pop((addr, session_id), None)

        sess = Session(self, addr, session_id, keys, info, self.mode)

        if self.mode == "tun" and self.tun is not None:
            sess.vip = self.tun.attach(sess)
            if sess.vip is None:
                sess.alive = False
                self.stat_reject += 1
                self._send_hello(addr, proto.HS_REJECTED, session_id, b"", b"", 0,
                                client_pub, client_nonce)
                return
        else:
            sess.fwd = netstack.UDPForwarder(sess, sess.send_ip_pkt)

        with self.lock:
            self.sessions[(addr, session_id)] = sess

        if info["room_id"]:
            try:
                store.bump_online(info["room_id"], 1)
            except Exception:
                pass

        resp = self._hello_body(proto.HS_OK, session_id, server_pub, server_nonce,
                                info["expires_at"])
        with self.lock:
            self._prune_hs_cache()
            self.hs_cache[ticket] = (addr, client_pub, client_nonce, resp, time.time())
        try:
            self.sock.sendto(resp, addr)
        except OSError:
            pass
        _log("hello ok %s card=%s mode=%s vip=%s"
             % (addr, info["card_key"], self.mode, sess.vip or "-"))

    def _prune_hs_cache(self):
        """缓存只留 60 秒，顺手清掉过期的。调用方需持锁。"""
        now = time.time()
        for k, v in list(self.hs_cache.items()):
            if now - v[4] > 60:
                self.hs_cache.pop(k, None)

    def _hello_body(self, status, session_id, pub, nonce, expires_at):
        return (bytes([status])
                + (pub if len(pub) == 32 else b"\x00" * 32)
                + (nonce if len(nonce) == 32 else b"\x00" * 32)
                + session_id.to_bytes(8, "big")
                + expires_at.to_bytes(8, "big"))

    def _send_hello(self, addr, status, session_id, pub, nonce, expires_at,
                    client_pub, client_nonce):
        """失败也要回满 81 字节，否则客户端会把包当坏包丢掉。"""
        try:
            self.sock.sendto(
                self._hello_body(status, session_id, pub, nonce, expires_at), addr)
        except OSError:
            pass

    def _on_packet(self, addr, data):
        h = proto.parse_header(data)
        if h is None:
            self.stat_bad += 1
            return
        key = (addr, h["session_id"])
        with self.lock:
            sess = self.sessions.get(key)
        if sess is None:
            self.stat_bad += 1
            return
        if h["dir"] != proto.DIR_C2S:
            sess.dropped += 1
            return
        if not sess.win.check_and_set(h["seq"]):
            sess.dropped += 1
            return

        pt = proto.open_(sess.keys[0], h["seq"], proto.DIR_C2S, h["ct"])
        if pt is None:
            sess.dropped += 1
            return

        self.stat_rx_pkts += 1
        sess.on_payload(h["flags"], pt)

    # ------------------------------------------------------------ 维护线程

    def _sweep(self):
        """回收空闲会话。"""
        if self.closed:
            return
        now = time.time()
        dead = []
        with self.lock:
            for k, s in self.sessions.items():
                if not s.alive or now - s.last_seen > conf.SESSION_IDLE:
                    dead.append((k, s))
        for k, s in dead:
            s.close("idle")
            with self.lock:
                self.sessions.pop(k, None)

    def _status_loop(self):
        """定期复查卡状态。封禁和到期靠这里踢人。"""
        while not self.closed:
            time.sleep(conf.SESSION_CHECK)
            if self.closed:
                return
            with self.lock:
                snapshot = list(self.sessions.items())
            for k, s in snapshot:
                row = store.get_card(s.card_key)
                reason = ""
                if row is None:
                    reason = "card_missing"
                elif int(row["status"]) == 2:
                    reason = "card_banned"
                elif int(row["expires_at"] or 0) <= int(time.time()):
                    reason = "card_expired"
                if reason:
                    try:
                        s.send_control({"t": "kick", "reason": reason})
                    except Exception:
                        pass
                    s.close(reason)
                    with self.lock:
                        self.sessions.pop(k, None)

    # ------------------------------------------------------------ 供 HTTP 调用

    def kick_card(self, card_key):
        """封卡后立刻踢掉该卡的所有会话。"""
        n = 0
        with self.lock:
            targets = [(k, s) for k, s in self.sessions.items()
                       if s.card_key == card_key]
        for k, s in targets:
            try:
                s.send_control({"t": "kick", "reason": "card_banned"})
            except Exception:
                pass
            s.close("admin kick")
            with self.lock:
                self.sessions.pop(k, None)
            n += 1
        return n

    def session_list(self):
        with self.lock:
            return [s.view() for s in self.sessions.values() if s.alive]

    def stats(self):
        with self.lock:
            n = len([s for s in self.sessions.values() if s.alive])
            rx = sum(s.rx_bytes for s in self.sessions.values())
            tx = sum(s.tx_bytes for s in self.sessions.values())
        out = {
            "mode": self.mode,
            "listen": conf.RELAY_LISTEN,
            "sessions": n,
            "rx_bytes": rx,
            "tx_bytes": tx,
            "rx_pkts": self.stat_rx_pkts,
            "tx_pkts": self.stat_tx_pkts,
            "bad_pkts": self.stat_bad,
            "hello": self.stat_hello,
            "rejected": self.stat_reject,
            "uptime": int(time.time() - self.started),
        }
        if self.tun is not None:
            out["tun"] = self.tun.stats()
        return out
