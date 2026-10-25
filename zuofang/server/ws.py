"""极简 WebSocket 服务端（RFC 6455 子集）。

只用标准库实现，够客户端（Dart `WebSocket`）用就行：
  * 只支持文本帧、ping/pong、close
  * 不做分片重组（客户端也不会发分片）
  * 不做扩展协商

为什么要自己写：
  Python 3.6 上没有 aiohttp/websockets 的稳定轮子，而服务端整体是
  标准库 + 线程模型，引一个异步框架会打乱结构。这个文件只做一件事 ——
  把事件推给客户端，顺便实现服务端主动踢人通知。

推送事件类型（客户端按 `t` 字段分派）：
  hello  连接建立
  tick   每 5 秒一次，带剩余时长、在线数、隧道状态
  join   有会话上线
  kick   卡密被封/到期，客户端应立即断开
"""

import base64
import hashlib
import json
import socket
import struct
import threading
import time

import store

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(key):
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("utf-8")).digest()).decode("ascii")


def frame(payload, opcode=0x1):
    """服务端发帧：不掩码。"""
    h = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        h.append(n)
    elif n < (1 << 16):
        h.append(126)
        h.extend(struct.pack("!H", n))
    else:
        h.append(127)
        h.extend(struct.pack("!Q", n))
    return bytes(h) + payload


class Client(object):
    """一条 WebSocket 连接。"""

    __slots__ = ("sock", "rf", "token", "card_key", "addr", "alive", "lock")

    def __init__(self, sock, rf, token, card_key, addr):
        self.sock = sock
        self.rf = rf
        self.token = token
        self.card_key = card_key
        self.addr = addr
        self.alive = True
        self.lock = threading.Lock()

    def send(self, obj):
        if not self.alive:
            return False
        try:
            data = frame(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            with self.lock:
                self.sock.sendall(data)
            return True
        except Exception:
            self.alive = False
            return False

    def close(self, code=1000):
        if not self.alive:
            return
        self.alive = False
        try:
            with self.lock:
                self.sock.sendall(frame(struct.pack("!H", code), opcode=0x8))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class Hub(object):
    """在线连接池 + 定时 tick。"""

    def __init__(self, relay=None):
        self.clients = []
        self.lock = threading.Lock()
        self.relay = relay
        self.closed = False
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._tick_loop, daemon=True,
                                        name="ws-tick")
        self._thread.start()
        return self

    def close(self):
        self.closed = True
        with self.lock:
            clients = list(self.clients)
            self.clients = []
        for c in clients:
            c.close()

    # ------------------------------------------------------------ 连接管理

    def add(self, client):
        with self.lock:
            self.clients.append(client)
        n = len(self)
        client.send({
            "t": "hello",
            "online": n,
            "server_time": store.now(),
            "remaining_seconds": self._remaining(client.card_key),
        })
        self.broadcast({"t": "presence", "online": n}, exclude=client)

    def remove(self, client):
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
        self.broadcast({"t": "presence", "online": len(self)}, exclude=client)

    def __len__(self):
        with self.lock:
            return len([c for c in self.clients if c.alive])

    # ------------------------------------------------------------ 推送

    def broadcast(self, obj, exclude=None):
        with self.lock:
            clients = [c for c in self.clients if c is not exclude]
        dead = []
        for c in clients:
            if not c.send(obj):
                dead.append(c)
        for c in dead:
            self.remove(c)

    def kick_card(self, card_key, reason="card_banned"):
        """把某张卡的连接全部推 kick 并断开。"""
        with self.lock:
            targets = [c for c in self.clients if c.card_key == card_key]
        for c in targets:
            c.send({"t": "kick", "reason": reason, "server_time": store.now()})
            c.close(1000)
            self.remove(c)
        return len(targets)

    # ------------------------------------------------------------ 内部

    def _remaining(self, card_key):
        row = store.get_card(card_key)
        if row is None:
            return 0
        return max(0, int(row["expires_at"] or 0) - store.now())

    def _tick_loop(self):
        """每 5 秒推一次状态；顺手清掉已死的连接。"""
        while not self.closed:
            time.sleep(5)
            if self.closed:
                return
            with self.lock:
                clients = list(self.clients)
            if not clients:
                continue

            tstats = self.relay.stats() if self.relay is not None else {}
            group = {}
            for c in clients:
                group.setdefault(c.card_key, []).append(c)

            for card_key, cs in group.items():
                row = store.get_card(card_key)
                if row is None:
                    for c in cs:
                        c.send({"t": "kick", "reason": "card_missing"})
                        c.close()
                        self.remove(c)
                    continue

                exp = int(row["expires_at"] or 0)
                remain = max(0, exp - store.now())
                status = int(row["status"])
                base = {
                    "t": "tick",
                    "online": len(self),
                    "remaining_seconds": remain,
                    "status": status,
                    "expires_at": exp,
                    "server_time": store.now(),
                }
                if tstats:
                    base["relay"] = {
                        "mode": tstats.get("mode", ""),
                        "sessions": tstats.get("sessions", 0),
                        "rx_bytes": tstats.get("rx_bytes", 0),
                        "tx_bytes": tstats.get("tx_bytes", 0),
                        "rx_pkts": tstats.get("rx_pkts", 0),
                        "tx_pkts": tstats.get("tx_pkts", 0),
                        "bad_pkts": tstats.get("bad_pkts", 0),
                        "uptime": tstats.get("uptime", 0),
                    }
                for c in cs:
                    if status == 2:
                        c.send({"t": "kick", "reason": "card_banned"})
                        c.close()
                        self.remove(c)
                    elif remain <= 0 and exp > 0:
                        c.send({"t": "kick", "reason": "card_expired"})
                        c.close()
                        self.remove(c)
                    else:
                        c.send(base)


# ---------------------------------------------------------------- 帧读取

def read_frame(rf):
    """返回 (opcode, payload)。读不到返回 (None, None)。"""
    b = rf.read(2)
    if not b or len(b) < 2:
        return None, None
    opcode = b[0] & 0x0F
    masked = b[1] & 0x80
    ln = b[1] & 0x7F
    if ln == 126:
        ext = rf.read(2)
        if len(ext) < 2:
            return None, None
        ln = struct.unpack("!H", ext)[0]
    elif ln == 127:
        ext = rf.read(8)
        if len(ext) < 8:
            return None, None
        ln = struct.unpack("!Q", ext)[0]
    if ln > 1 << 20:
        # 客户端不会发大包，超过 1MB 直接判坏
        return None, None
    mask = rf.read(4) if masked else b"\x00\x00\x00\x00"
    data = rf.read(ln) if ln else b""
    if len(data) < ln:
        return None, None
    if masked:
        data = bytes(data[i] ^ mask[i % 4] for i in range(len(data)))
    return opcode, data


def serve_client(handler, token, card_key):
    """把一条已升级的连接接进 Hub 并进入读循环（阻塞到断开）。"""
    sock = handler.connection
    hub = handler.server.hub
    rf = handler.rfile

    try:
        sock.settimeout(1.0)
    except OSError:
        pass

    client = Client(sock, rf, token, card_key, handler.client_address)
    hub.add(client)

    last_rx = time.time()
    try:
        while client.alive:
            try:
                op, data = read_frame(rf)
            except socket.timeout:
                op, data = None, None
            except Exception:
                break

            if op == 0x8:
                break
            if op == 0x9:
                try:
                    with client.lock:
                        sock.sendall(frame(data or b"", opcode=0xA))
                except Exception:
                    break
                continue
            if op == 0x1 and data:
                # 客户端可发 {"t":"ping"}，回 pong 顺带带服务器时间
                try:
                    msg = json.loads(data.decode("utf-8"))
                except Exception:
                    msg = {}
                if msg.get("t") == "ping":
                    client.send({"t": "pong", "ts": msg.get("ts", 0),
                                 "server_time": store.now()})
            last_rx = time.time()

            # 卡被封了立刻断，不等下一个 tick
            row = store.get_card(card_key)
            if row is None or int(row["status"]) == 2:
                client.send({"t": "kick", "reason": "card_banned"})
                break
    except Exception:
        pass
    finally:
        client.close()
        hub.remove(client)
        handler.close_connection = True
