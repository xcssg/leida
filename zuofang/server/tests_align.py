"""proto.py 与 Rust 侧的逐字节对齐测试。

向量由 `cd app/rust && cargo run --release --example vect` 生成。
只要这边断言全绿，Python 中继和 Rust 客户端就能互通。

跑法：cd server && python3 tests_align.py
"""

import sys

import proto

FAIL = 0


def check(name, got, want):
    global FAIL
    if got != want:
        FAIL += 1
        print("FAIL %s\n  got  %s\n  want %s" % (name, got, want))
    else:
        print("ok   %s" % name)


def h(b):
    return b.hex()


def unhex(s):
    return bytes.fromhex(s)


# ---------------------------------------------------------------- 1. 密钥派生

shared = b"\x11" * 32
cn = b"\x22" * 32
sn = b"\x33" * 32
sid = 0x0102030405060708

c2s, s2c = proto.derive_keys(shared, cn, sn, sid)
check("derive.c2s", h(c2s),
      "a44aba222e0fc4e47e40e0cc56700dbebc503cc6313c2d66cdd8ba5b5420612e")
check("derive.s2c", h(s2c),
      "377e7aafb45afc8b51cfaef11d154e0baa56e3d73baffd45142fb1e6fb4c59ce")

# ---------------------------------------------------------------- 2. 加密

key = b"\x07" * 32

check("seal.short", h(proto.seal(key, 1, proto.DIR_C2S, b"hello")),
      "102b446ebc1980a2f015149943cc1ce6bbdf3dff70")

pt2 = bytes(range(40))
check("seal.long", h(proto.seal(key, 0x0102030405060708, proto.DIR_S2C, pt2)),
      "7775469543e493e135d5155a91d93b737d2205ca0f29fcaded7d8e25a20f8395"
      "eab476419487e390603b3116b59492e6c69a6623413e06f3")

check("seal.empty", h(proto.seal(key, 9, proto.DIR_C2S, b"")),
      "2e4f2379923a43d7cf1bad5759ecbe7c")

# ---------------------------------------------------------------- 3. 头

hdr = proto.build_header(0x1122334455667788, proto.DIR_C2S, proto.FLAG_CONTROL, 42, 100)
check("header.encode", h(hdr),
      "54474350010100011122334455667788000000000000002a0064")

back = proto.parse_header(hdr + b"\x00" * 100)
check("header.roundtrip.session_id", back["session_id"], 0x1122334455667788)
check("header.roundtrip.seq", back["seq"], 42)
check("header.roundtrip.len", back["len"], 100)
check("header.roundtrip.dir", back["dir"], proto.DIR_C2S)
check("header.roundtrip.flags", back["flags"], proto.FLAG_CONTROL)

# 垃圾头必须被拒
check("header.reject.magic", proto.parse_header(b"NOPE" + b"\x00" * 22), None)
check("header.reject.short", proto.parse_header(b"TGCP"), None)
bad_ver = bytearray(hdr + b"\x00" * 100)
bad_ver[4] = 2
check("header.reject.ver", proto.parse_header(bytes(bad_ver)), None)
over = bytearray(hdr + b"\x00" * 100)
over[24:26] = (2000).to_bytes(2, "big")
check("header.reject.overlong", proto.parse_header(bytes(over)), None)

# ---------------------------------------------------------------- 4. 加解密回环

for seq in (1, 2, 255, 256, 65535, 0xFFFFFFFFFFFF):
    for length in (0, 1, 31, 32, 33, 1000, 1424):
        pt = bytes((i * 7 + 3) & 0xFF for i in range(length))
        ct = proto.seal(key, seq, proto.DIR_C2S, pt)
        if len(ct) != length + proto.TAG_LEN:
            FAIL += 1
            print("FAIL seal.len seq=%d len=%d got=%d" % (seq, length, len(ct)))
            continue
        if proto.open_(key, seq, proto.DIR_C2S, ct) != pt:
            FAIL += 1
            print("FAIL open roundtrip seq=%d len=%d" % (seq, length))
        if proto.open_(key, seq, proto.DIR_S2C, ct) is not None:
            FAIL += 1
            print("FAIL open wrong-dir accepted seq=%d len=%d" % (seq, length))
print("ok   seal/open roundtrip matrix")

# 篡改必须失败
ct = proto.seal(key, 5, proto.DIR_C2S, b"payload-1234")
for pos in (0, len(ct) // 2, len(ct) - 1):
    bad = bytearray(ct)
    bad[pos] ^= 1
    if proto.open_(key, 5, proto.DIR_C2S, bytes(bad)) is not None:
        FAIL += 1
        print("FAIL tamper accepted at %d" % pos)
print("ok   tamper rejected")

# 截断必须失败
check("open.truncated", proto.open_(key, 5, proto.DIR_C2S, ct[:10]), None)

# ---------------------------------------------------------------- 5. X25519

sk = b"\x77" * 32
check("x25519.pub", h(proto.x25519_base(sk)),
      "1cf579aba45a10ba1d1ef06d91fca2aa9ed0a1150515653155405d0b18cb9a67")

# 双方算出的共享密钥必须相等
a_sk, b_sk = b"\x01" * 32, b"\x02" * 32
a_pub = proto.x25519_base(a_sk)
b_pub = proto.x25519_base(b_sk)
check("x25519.dh", h(proto.x25519(a_sk, b_pub)), h(proto.x25519(b_sk, a_pub)))

# RFC 7748 §6.1 官方向量（第一组）
rfc_scalar = unhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
rfc_point = unhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
check("x25519.rfc7748", h(proto.x25519(rfc_scalar, rfc_point)),
      "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")

# ---------------------------------------------------------------- 6. 重放窗口

w = proto.ReplayWindow()

check("replay.first", w.check_and_set(1), True)
check("replay.dup", w.check_and_set(1), False)
check("replay.zero", w.check_and_set(0), False)
check("replay.forward", w.check_and_set(5), True)
check("replay.fill-2", w.check_and_set(2), True)
check("replay.fill-2-dup", w.check_and_set(2), False)
check("replay.late-3", w.check_and_set(3), True)
check("replay.jump", w.check_and_set(1000), True)
check("replay.old-in-window", w.check_and_set(800), True)
check("replay.too-old", w.check_and_set(500), False)
check("replay.just-inside", w.check_and_set(1000 - 255), True)
check("replay.just-outside", w.check_and_set(1000 - 256), False)

# 乱序：分块洗牌，块跨度小于窗口，保证每个包都落在窗口内可被接受
import random
random.seed(7)
seqs = []
for start in range(1, 400, 100):
    chunk = list(range(start, min(start + 100, 400)))
    random.shuffle(chunk)
    seqs.extend(chunk)
w2 = proto.ReplayWindow()
accept = sum(1 for s in seqs if w2.check_and_set(s))
reject_dup = sum(1 for s in seqs if w2.check_and_set(s))
check("replay.shuffle.accepted", accept, len(seqs))
check("replay.shuffle.dups", reject_dup, 0)

# ---------------------------------------------------------------- 7. IPv4/UDP

pkt = proto.build_udp("1.2.3.4", "10.7.0.2", 5353, 12345, b"\xde\xad\xbe\xef")
ip = proto.parse_ipv4_packet(pkt)
check("ipv4.src", ip["src"], "1.2.3.4")
check("ipv4.dst", ip["dst"], "10.7.0.2")
check("ipv4.proto", ip["proto"], 17)
check("ipv4.ihl", ip["ihl"], 20)
check("ipv4.total", ip["total"], 20 + 8 + 4)
check("ipv4.frag_off", ip["frag_off"], 0)
check("ipv4.mf", ip["mf"], False)

# IP 头校验和必须为 0（校验通过）
check("ipv4.checksum", proto.ip_checksum(pkt[:20]), 0)

# UDP 校验和必须正确（含伪头）
udp_part = pkt[20:]
pseudo = (proto.ip_build("1.2.3.4") + proto.ip_build("10.7.0.2")
          + bytes([0, 17]) + (len(udp_part)).to_bytes(2, "big"))
check("udp.checksum", proto.ip_checksum(pseudo + udp_part), 0)

u = proto.parse_udp(ip["payload"])
check("udp.sport", u["sport"], 5353)
check("udp.dport", u["dport"], 12345)
check("udp.data", u["data"], b"\xde\xad\xbe\xef")

# 畸形包必须返回 None
check("ipv4.reject.short", proto.parse_ipv4_packet(b"\x45" + b"\x00" * 10), None)
check("ipv4.reject.v6", proto.parse_ipv4_packet(b"\x60" + b"\x00" * 40), None)

# ---------------------------------------------------------------- 汇总

print("")
if FAIL:
    print("FAILED %d check(s)" % FAIL)
    sys.exit(1)
print("ALL PASS")
