"""TGCP v1 协议实现（服务端侧）。

设计取舍：
  * 密钥交换用 X25519 一次性临时密钥，不做前向安全棘轮 —— 服务端要支撑
    大量短连接会话，棘轮带来的状态开销不划算。
  * 数据加密用 HMAC-SHA256 计数器 keystream + 16 字节 HMAC tag。
    这不是 AEAD（没有专门认证的附加数据），但 Encrypt-then-MAC 结构下是安全的。
    keystream 每次会话密钥唯一、seq 单调，不会复用。
  * seq 用滑动窗口防重放，窗口 256。UDP 会乱序，窗口太小会误杀。

与客户端 `app/rust/src/proto.rs` 必须逐字节一致。
"""

import hashlib
import hmac
import struct

MAGIC = b"TGCP"
VER = 1

DIR_C2S = 0x01
DIR_S2C = 0x02

FLAG_CONTROL = 1 << 0
FLAG_CAPTURE = 1 << 1

HEADER_LEN = 26
TAG_LEN = 16
MAX_PAYLOAD = 1424
MAX_PACKET = HEADER_LEN + MAX_PAYLOAD

# hello 包布局
#   C2S: pub(32) nonce(32) session_id(8) ticket_len(1) ticket(ticket_len)
#   S2C: status(1) pub(32) nonce(32) session_id(8) expires_at(8)
HELLO_C2S_FIXED = 32 + 32 + 8 + 1
HELLO_S2C_LEN = 1 + 32 + 32 + 8 + 8
MAX_TICKET_LEN = 120

HS_OK = 0
HS_REJECTED = 1
HS_EXPIRED = 2
HS_BANNED = 3
HS_BAD_TICKET = 4


# ---------------------------------------------------------------- X25519

_P = 2 ** 255 - 19
_A24 = 121665


def _clamp(k):
    b = bytearray(k)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(bytes(b), "little")


def x25519(scalar, u):
    k = _clamp(scalar)
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = pow((da + cb) % _P, 2, _P)
        z3 = x1 * pow((da - cb) % _P, 2, _P) % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + _A24 * e) % _P) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def x25519_base(scalar):
    return x25519(scalar, (9).to_bytes(32, "little"))


# ---------------------------------------------------------------- 密钥派生

def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def derive_keys(shared, client_nonce, server_nonce, session_id):
    """HKDF-Extract 再 Expand 出双向密钥。返回 (c2s, s2c)。"""
    prk = _hmac(client_nonce + server_nonce, shared)
    sid = struct.pack("!Q", session_id)
    return _hmac(prk, b"tgcp c2s" + sid), _hmac(prk, b"tgcp s2c" + sid)


# ---------------------------------------------------------------- 头

def build_header(session_id, direction, flags, seq, ct_len):
    return (MAGIC + bytes([VER, direction]) + struct.pack("!H", flags)
            + struct.pack("!Q", session_id) + struct.pack("!Q", seq)
            + struct.pack("!H", ct_len))


def parse_header(data):
    """返回 dict 或 None。只做结构校验，密码学校验在 open() 里。"""
    if len(data) < HEADER_LEN or data[0:4] != MAGIC or data[4] != VER:
        return None
    ln = struct.unpack_from("!H", data, 24)[0]
    if ln > MAX_PAYLOAD or HEADER_LEN + ln > len(data):
        return None
    return {
        "dir": data[5],
        "flags": struct.unpack_from("!H", data, 6)[0],
        "session_id": struct.unpack_from("!Q", data, 8)[0],
        "seq": struct.unpack_from("!Q", data, 16)[0],
        "len": ln,
        "ct": data[HEADER_LEN:HEADER_LEN + ln],
    }


# ---------------------------------------------------------------- 加密

def _nonce(seq, direction):
    """12 字节 nonce：seq 大端 8 字节 + dir 1 字节 + 3 字节零。

    与 Rust `make_nonce()` 一致 —— 那里是 `[u8; 12]`，`n[8] = dir`，其余补零。
    早先写成 9 字节会算出完全不同的 keystream，两端口径必须严格相同。
    """
    return struct.pack("!Q", seq) + bytes([direction]) + b"\x00\x00\x00"


def _keystream(key, nonce, n):
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += _hmac(key, nonce + struct.pack("<I", counter))
        counter = (counter + 1) & 0xFFFFFFFF
    return bytes(out[:n])


def seal(key, seq, direction, pt):
    nonce = _nonce(seq, direction)
    ks = _keystream(key, nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    tag = _hmac(key, nonce + ct)[:TAG_LEN]
    return ct + tag


def open_(key, seq, direction, ct):
    """解密；失败返回 None（不区分原因，避免给探测者信号）。"""
    if len(ct) < TAG_LEN:
        return None
    body, tag = ct[:-TAG_LEN], ct[-TAG_LEN:]
    nonce = _nonce(seq, direction)
    if not hmac.compare_digest(_hmac(key, nonce + body)[:TAG_LEN], tag):
        return None
    ks = _keystream(key, nonce, len(body))
    return bytes(a ^ b for a, b in zip(body, ks))


# ---------------------------------------------------------------- 重放窗口

class ReplayWindow(object):
    """RFC 6479 风格滑动窗口，位图实现。窗口大小必须是 2 的幂。"""

    __slots__ = ("size", "mask", "last", "bitmap")

    def __init__(self, size=256):
        self.size = size
        self.mask = size - 1
        self.last = 0
        self.bitmap = [0] * (size // 32)

    def _bit(self, seq):
        return (seq - 1) & self.mask

    def check_and_set(self, seq):
        """返回 True 表示新包；False 表示重放或太旧。"""
        if seq == 0:
            return False

        if seq > self.last:
            gap = seq - self.last
            if gap >= self.size:
                self.bitmap = [0] * (self.size // 32)
            else:
                # 清掉窗口内被覆盖的位置
                for s in range(self.last + 1, seq):
                    b = self._bit(s)
                    self.bitmap[b >> 5] &= ~(1 << (b & 31))
            self.last = seq
            b = self._bit(seq)
            self.bitmap[b >> 5] |= 1 << (b & 31)
            return True

        # 窗口内或太旧
        if self.last - seq >= self.size:
            return False
        b = self._bit(seq)
        if self.bitmap[b >> 5] & (1 << (b & 31)):
            return False
        self.bitmap[b >> 5] |= 1 << (b & 31)
        return True


# ---------------------------------------------------------------- 控制帧

def encode_control(obj, json_mod):
    return json_mod.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def pack(session_id, direction, flags, seq, key, pt):
    ct = seal(key, seq, direction, pt)
    return build_header(session_id, direction, flags, seq, len(ct)) + ct


# ---------------------------------------------------------------- IPv4 工具

def ip_parse(b, off=0):
    return "%d.%d.%d.%d" % (b[off], b[off + 1], b[off + 2], b[off + 3])


def ip_build(s):
    p = s.split(".")
    return bytes([int(p[0]), int(p[1]), int(p[2]), int(p[3])])


def ip_checksum(data):
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def parse_ipv4_packet(pkt):
    """解析 IPv4 头。返回 dict 或 None。

    只解析转发需要的字段：协议、源/目的 IP、分片信息。
    """
    if len(pkt) < 20:
        return None
    vihl = pkt[0]
    if (vihl >> 4) != 4:
        return None
    ihl = (vihl & 0x0F) * 4
    if ihl < 20 or len(pkt) < ihl:
        return None
    total = struct.unpack_from("!H", pkt, 2)[0]
    if total < ihl or total > len(pkt):
        total = len(pkt)
    flags_frag = struct.unpack_from("!H", pkt, 6)[0]
    return {
        "ihl": ihl,
        "total": total,
        "proto": pkt[9],
        "src": ip_parse(pkt, 12),
        "dst": ip_parse(pkt, 16),
        "ttl": pkt[8],
        "mf": bool(flags_frag & 0x2000),
        "frag_off": (flags_frag & 0x1FFF) * 8,
        "payload": pkt[ihl:total],
    }


def parse_udp(payload):
    if len(payload) < 8:
        return None
    sport, dport, ln, csum = struct.unpack_from("!HHHH", payload, 0)
    if ln < 8:
        return None
    end = min(ln, len(payload))
    return {"sport": sport, "dport": dport, "data": payload[8:end]}


def build_udp(src_ip, dst_ip, sport, dport, data, src_port_override=None):
    """构造完整的 IPv4+UDP 包（S2C 方向用）。

    src_ip/dst_ip 是字符串。校验和按 IPv4 规则算（UDP 校验和可为 0）。
    """
    sp = src_port_override if src_port_override is not None else sport
    ulen = 8 + len(data)
    udp = struct.pack("!HHHH", sp, dport, ulen, 0) + data

    s_ip = ip_build(src_ip)
    d_ip = ip_build(dst_ip)

    # UDP 伪头校验和
    pseudo = s_ip + d_ip + struct.pack("!BBH", 0, 17, ulen)
    csum = ip_checksum(pseudo + udp)
    if csum == 0:
        csum = 0xFFFF
    udp = udp[:6] + struct.pack("!H", csum) + udp[8:]

    total = 20 + ulen
    hdr = (bytes([0x45, 0]) + struct.pack("!HHHBBH", total, 0, 0, 64, 17, 0)
           + s_ip + d_ip)
    c = ip_checksum(hdr)
    hdr = hdr[:10] + struct.pack("!H", c) + hdr[12:]
    return hdr + udp
