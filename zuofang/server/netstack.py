"""用户态 IPv4/UDP 转发（不需要 root）。

为什么要有这条路：
  服务器常常跑在容器里，拿不到 /dev/net/tun，也开不了 ip_forward。
  这时候 TUN + NAT 方案直接不可用，但业务还是得能跑。
  用户态转发只用普通 UDP socket，任何环境都能起。

能力边界（先说清楚，免得后面踩坑）：
  * 只转发 UDP。TCP 走不了 —— 用户态实现 TCP 等于写半个协议栈，
    代价远大于收益。
  * 不做 IP 分片重组。分片包直接丢弃。
  * 客户端 TUN 里看到的源 IP 是服务端分配的虚拟地址（10.7.x.x），
    回包按这个虚拟地址投递，所以 NAT 语义是完整的。

工作方式：
  1. 客户端把 IP 包塞进隧道上来
  2. 解析出 (dst_ip, dst_port)，查 flow 表
  3. 没有就新建一个 UDP socket，connect 到目标，起一个读线程
  4. 读线程收到回包，封成 IPv4+UDP，交给上层回隧道
"""

import socket
import struct
import threading
import time

import conf
import proto


class Flow(object):
    """一条 UDP 流 = 一个到目标的本地 socket + 一个读线程。"""

    __slots__ = ("key", "sock", "client_ip", "client_port", "dst_ip", "dst_port",
                 "created", "last_seen", "sent", "recv", "alive", "session")

    def __init__(self, key, sock, client_ip, client_port, dst_ip, dst_port, session):
        self.key = key
        self.sock = sock
        self.client_ip = client_ip
        self.client_port = client_port
        self.dst_ip = dst_ip
        self.dst_port = dst_port
        self.created = time.time()
        self.last_seen = self.created
        self.sent = 0
        self.recv = 0
        self.alive = True
        self.session = session


class UDPForwarder(object):
    """把 IP 包转成真实 UDP 并回程。每个会话持有一个实例。"""

    def __init__(self, session, emit, allow_private=None):
        """emit(bytes) 用于把回程 IP 包交回会话。

        allow_private=True 时放行内网地址（游戏服务器在内网、或做本地联调）。
        """
        self.session = session
        self.emit = emit
        self.flows = {}
        self.lock = threading.Lock()
        self.closed = False
        self.max_flows = conf.UDP_FLOW_MAX
        if allow_private is None:
            allow_private = conf.ALLOW_PRIVATE
        self.allow_private = allow_private

    # ---------------------------------------------------------------- 入向

    def handle_packet(self, pkt):
        """返回 'ok' / 'drop' / 'not_udp'。"""
        ip = proto.parse_ipv4_packet(pkt)
        if ip is None:
            return "drop"

        # 分片包不处理：用户态没有重组能力，硬转会把包变成垃圾
        if ip["frag_off"] != 0 or ip["mf"]:
            return "drop"

        if ip["proto"] != 17:
            return "not_udp"

        udp = proto.parse_udp(ip["payload"])
        if udp is None:
            return "drop"

        dst_ip, dst_port = ip["dst"], udp["dport"]

        # 拒绝明显不该转的地址，避免被当成内网跳板
        if not self.allow_private and _blocked(dst_ip):
            return "drop"

        key = (dst_ip, dst_port, udp["sport"])
        flow = self._get_flow(key, ip["src"], udp["sport"], dst_ip, dst_port)
        if flow is None:
            return "drop"

        try:
            flow.sock.send(udp["data"])
            flow.sent += len(udp["data"])
            flow.last_seen = time.time()
        except Exception:
            self._close_flow(key)
            return "drop"
        return "ok"

    def _get_flow(self, key, client_ip, client_port, dst_ip, dst_port):
        with self.lock:
            if self.closed:
                return None
            f = self.flows.get(key)
            if f is not None:
                return f
            if len(self.flows) >= self.max_flows:
                self._evict_locked()
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(0.5)
                s.connect((dst_ip, dst_port))
            except Exception:
                return None
            f = Flow(key, s, client_ip, client_port, dst_ip, dst_port, self.session)
            self.flows[key] = f

        t = threading.Thread(target=self._reader, args=(f,), daemon=True,
                             name="flow-%s:%d" % (dst_ip, dst_port))
        t.start()
        return f

    def _evict_locked(self):
        """flow 满了就淘汰最久没动的那个。"""
        if not self.flows:
            return
        oldest = min(self.flows.items(), key=lambda kv: kv[1].last_seen)
        self._close_flow_locked(oldest[0])

    # ---------------------------------------------------------------- 出向

    def _reader(self, flow):
        while flow.alive and not self.closed:
            try:
                data, _ = flow.sock.recvfrom(65535)
            except socket.timeout:
                if time.time() - flow.last_seen > 60:
                    break
                continue
            except Exception:
                break
            if not data:
                continue
            flow.recv += len(data)
            flow.last_seen = time.time()
            try:
                # 回程：源是目标的真实地址，目的是客户端的虚拟地址
                pkt = proto.build_udp(
                    src_ip=flow.dst_ip, dst_ip=flow.client_ip,
                    sport=flow.dst_port, dport=flow.client_port, data=data,
                )
                self.emit(pkt)
            except Exception:
                break
        self._close_flow(flow.key)

    # ---------------------------------------------------------------- 清理

    def _close_flow(self, key):
        with self.lock:
            self._close_flow_locked(key)

    def _close_flow_locked(self, key):
        f = self.flows.pop(key, None)
        if f is None:
            return
        f.alive = False
        try:
            f.sock.close()
        except Exception:
            pass

    def close(self):
        self.closed = True
        with self.lock:
            for k in list(self.flows):
                self._close_flow_locked(k)

    # ---------------------------------------------------------------- 统计

    def snapshot(self):
        with self.lock:
            return [
                {"dst": "%s:%d" % (f.dst_ip, f.dst_port),
                 "sent": f.sent, "recv": f.recv, "age": int(time.time() - f.created)}
                for f in sorted(self.flows.values(), key=lambda x: -x.last_seen)[:20]
            ]

    def totals(self):
        with self.lock:
            return (sum(f.sent for f in self.flows.values()),
                    sum(f.recv for f in self.flows.values()),
                    len(self.flows))


# ---------------------------------------------------------------- 地址过滤

def _blocked(ip):
    """挡住明显的内网/保留地址。

    这不是安全边界（服务端本来就是出口），只是避免卡密用户
    拿隧道去扫服务器所在内网。
    """
    try:
        a, b, c, d = [int(x) for x in ip.split(".")]
    except Exception:
        return True

    if a == 0 or a == 127:
        return True
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 169 and b == 254:
        return True
    if a >= 224:
        return True
    if a == 100 and 64 <= b <= 127:
        return True
    return False


def allow_private(ip):
    """想放行内网时用（例如游戏服务器在内网）。"""
    return not _blocked(ip)
