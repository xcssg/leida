"""用户态转发（netstack.py）的本地回声测试。

不依赖 /dev/net/tun，也不需要真出网 —— 起一个本地 UDP echo server，
把构造好的 IPv4+UDP 包喂给 UDPForwarder，断言 emit 收到可解析的回程包，
且回程的源/目的地址、端口、载荷全部正确。

跑法：cd server && python3 tests_netstack.py
"""

import socket
import sys
import threading
import time

import netstack
import proto

FAIL = 0


def check(name, got, want):
    global FAIL
    if got != want:
        FAIL += 1
        print("FAIL %s\n  got  %r\n  want %r" % (name, got, want))
    else:
        print("ok   %s" % name)


def echo_server():
    """起一个本地 UDP echo server，返回 (ip, port, stop_fn)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.settimeout(0.2)
    port = s.getsockname()[1]
    stop = [False]

    def run():
        while not stop[0]:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                s.sendto(b"echo:" + data, addr)
            except OSError:
                break

    t = threading.Thread(target=run, daemon=True)
    t.start()

    def fin():
        stop[0] = True
        try:
            s.close()
        except OSError:
            pass

    return "127.0.0.1", port, fin


def main():
    srv_ip, srv_port, srv_stop = echo_server()

    got = []
    got_lock = threading.Lock()

    def emit(pkt):
        with got_lock:
            got.append(pkt)

    fwd = netstack.UDPForwarder(session=None, emit=emit, allow_private=True)

    client_ip = "10.7.0.42"
    client_port = 40001
    payload = b"ping-from-tunnel"

    pkt = proto.build_udp(src_ip=client_ip, dst_ip=srv_ip,
                          sport=client_port, dport=srv_port, data=payload)
    r = fwd.handle_packet(pkt)
    check("forward.result", r, "ok")

    # 等回程
    deadline = time.time() + 3.0
    while time.time() < deadline and not got:
        time.sleep(0.02)

    check("reply.count", len(got), 1)
    if not got:
        return finish(fwd, srv_stop)

    back = proto.parse_ipv4_packet(got[0])
    check("reply.src", back["src"], srv_ip)
    check("reply.dst", back["dst"], client_ip)
    check("reply.proto", back["proto"], 17)
    check("reply.checksum", proto.ip_checksum(got[0][:20]), 0)

    u = proto.parse_udp(back["payload"])
    check("reply.sport", u["sport"], srv_port)
    check("reply.dport", u["dport"], client_port)
    check("reply.data", u["data"], b"echo:" + payload)

    # ---- 同一条流复用一个 socket
    pkt2 = proto.build_udp(src_ip=client_ip, dst_ip=srv_ip,
                           sport=client_port, dport=srv_port, data=b"second")
    check("reuse.result", fwd.handle_packet(pkt2), "ok")
    deadline = time.time() + 3.0
    while time.time() < deadline and len(got) < 2:
        time.sleep(0.02)
    check("reuse.flow-count", fwd.totals()[2], 1)
    check("reuse.reply", len(got) >= 2, True)

    # ---- 分片包必须丢
    frag = bytearray(pkt)
    frag[6] = 0x20  # MF 置位
    check("frag.drop", fwd.handle_packet(bytes(frag)), "drop")

    # ---- 非 UDP 必须走 not_udp
    tcp = bytearray(pkt)
    tcp[9] = 6
    check("tcp.not_udp", fwd.handle_packet(bytes(tcp)), "not_udp")

    # ---- 垃圾包
    check("garbage.drop", fwd.handle_packet(b"\x00" * 10), "drop")

    # ---- 默认不允许内网
    strict = netstack.UDPForwarder(session=None, emit=lambda p: None,
                                  allow_private=False)
    check("private.blocked", strict.handle_packet(pkt), "drop")
    strict.close()

    # ---- 地址过滤表
    blocked = ["0.0.0.0", "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255",
               "192.168.1.1", "169.254.1.1", "224.0.0.1", "255.255.255.255",
               "100.64.0.1", "100.127.255.255"]
    for ip in blocked:
        check("filter.blocked.%s" % ip, netstack._blocked(ip), True)
    allowed = ["1.1.1.1", "8.8.8.8", "104.16.0.1", "172.15.0.1", "172.32.0.1",
               "100.63.255.255", "100.128.0.1", "192.169.0.1"]
    for ip in allowed:
        check("filter.allowed.%s" % ip, netstack._blocked(ip), False)

    # ---- 关闭后不再接新流
    fwd.close()
    check("closed.drop", fwd.handle_packet(pkt), "drop")

    return finish(fwd, srv_stop)


def finish(fwd, srv_stop):
    try:
        fwd.close()
    except Exception:
        pass
    srv_stop()
    print("")
    if FAIL:
        print("FAILED %d check(s)" % FAIL)
        sys.exit(1)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
