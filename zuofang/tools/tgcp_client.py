"""TGCP v1 客户端参考实现（Python），用于在不起 App 的情况下验证中继。

它和 `server/proto.py` 共用同一份协议实现，所以这里能跑通，
说明 Python 侧自洽；与 Rust 客户端的逐字节一致性由 `server/tests_align.py` 保证。

用法：
    # 1) 先拿一张可用票据
    TOKEN=$(curl -s -X POST http://1.2.3.4:8090/api/card/activate \
              -H 'content-type: application/json' \
              -d '{"card_key":"ZF-....","device":"test"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
    SID=$(curl -s -X POST http://1.2.3.4:8090/api/join \
              -H "X-Card-Token: $TOKEN" -H 'X-Device-Token: test' \
              -H 'content-type: application/json' -d '{"room_code":"..."}' \
              | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
    TICKET=$(... 同上取 token 字段 ...)

    # 2) 握手 + 控制帧往返
    python3 tools/tgcp_client.py --relay 1.2.3.4:9000 --session $SID --token $TICKET

退出码 0 表示 pong 与 ack 数量都符合预期。
"""

import argparse
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "server"))

import proto  # noqa: E402


def session_u64(s):
    """与服务端 `webapi._session_id()` 一致：能解析就直用，否则 FNV-1a64。"""
    try:
        return int(s)
    except ValueError:
        pass
    h = 0xCBF29CE484222325
    for b in s.encode():
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


class Client(object):
    def __init__(self, relay, session_id, ticket=""):
        import socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)
        self.relay = relay
        self.sid = session_u64(session_id)
        self.ticket = ticket
        self.seq = 0
        self.win = proto.ReplayWindow()
        self.c2s = self.s2c = None
        self.expires_at = 0

    def handshake(self, timeout=5.0):
        secret = os.urandom(32)
        pub = proto.x25519_base(secret)
        nonce = os.urandom(32)
        tb = self.ticket.encode("utf-8")
        if len(tb) > proto.MAX_TICKET_LEN:
            raise RuntimeError("票据过长")
        hello = (pub + nonce + struct.pack("!Q", self.sid)
                 + bytes([len(tb)]) + tb)

        deadline = time.time() + timeout
        status = None
        while time.time() < deadline:
            self.sock.sendto(hello, self.relay)
            try:
                data, _ = self.sock.recvfrom(2048)
            except Exception:
                continue
            if len(data) != proto.HELLO_S2C_LEN:
                continue
            status = data[0]
            if status != proto.HS_OK:
                raise RuntimeError("handshake rejected: status=%d" % status)
            spub = data[1:33]
            snonce = data[33:65]
            sid = struct.unpack_from("!Q", data, 65)[0]
            self.expires_at = struct.unpack_from("!Q", data, 73)[0]
            if sid != self.sid:
                raise RuntimeError("session mismatch")
            shared = proto.x25519(secret, spub)
            if shared == b"\x00" * 32:
                raise RuntimeError("invalid DH secret")
            self.c2s, self.s2c = proto.derive_keys(shared, nonce, snonce, sid)
            return True
        raise RuntimeError("handshake timeout")

    def _send(self, flags, payload):
        self.seq += 1
        pkt = proto.pack(self.sid, proto.DIR_C2S, flags, self.seq,
                         self.c2s, payload)
        self.sock.sendto(pkt, self.relay)

    def ping(self):
        self._send(proto.FLAG_CONTROL,
                   json.dumps({"t": "ping", "ts": int(time.time() * 1000)}).encode())

    def capture(self, bank, payload=b"FAKE-IP-PACKET", last=True):
        h = json.dumps({"bank": bank, "last": 1 if last else 0,
                        "ts": int(time.time() * 1000), "proto": "ip"}).encode()
        self._send(proto.FLAG_CAPTURE, struct.pack("!H", len(h)) + h + payload)

    def recv(self):
        """返回 (flags, 明文)；坏包 / 重放 / 超时返回 None。"""
        data, _ = self.sock.recvfrom(proto.MAX_PACKET * 2)
        h = proto.parse_header(data)
        if h is None or h["dir"] != proto.DIR_S2C:
            return None
        if not self.win.check_and_set(h["seq"]):
            return None
        pt = proto.open_(self.s2c, h["seq"], proto.DIR_S2C, h["ct"])
        if pt is None:
            return None
        return h["flags"], pt


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default="127.0.0.1:9000")
    ap.add_argument("--session", default="demo-1",
                    help="join 返回的 session_id（十进制串）")
    ap.add_argument("--token", default="", help="join 返回的 ticket")
    ap.add_argument("--rounds", type=int, default=3)
    a = ap.parse_args(argv)

    host, _, port = a.relay.rpartition(":")
    c = Client((host, int(port)), a.session, a.token)
    c.handshake()
    print("handshake ok, session=%d expires_at=%d" % (c.sid, c.expires_at))

    got_pong = 0
    got_ack = 0
    for i in range(a.rounds):
        c.ping()
        c.capture(100 + i)
        deadline = time.time() + 2.0
        while time.time() < deadline and (got_pong < i + 1 or got_ack < i + 1):
            r = c.recv()
            if r is None:
                continue
            _flags, pt = r
            try:
                v = json.loads(pt.decode())
            except Exception:
                continue
            if v.get("t") == "pong":
                got_pong += 1
                print("pong ts=%s" % v.get("ts"))
            elif v.get("t") == "ack":
                got_ack += 1
                print("ack bank=%s" % v.get("bank"))

    print("summary: pong=%d ack=%d" % (got_pong, got_ack))
    return 0 if got_pong == a.rounds and got_ack == a.rounds else 1


if __name__ == "__main__":
    sys.exit(main())
