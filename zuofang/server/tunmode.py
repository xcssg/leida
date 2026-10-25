"""TUN 模式 —— 全协议出网（需要 root）。

和 netstack.py 的分工：
  * netstack：免 root，只转 UDP，每个 flow 一个 socket，靠 socket 的端口做 NAT
  * tunmode ：要 root，内核转发，TCP/UDP/ICMP 全通，靠 iptables MASQUERADE 做 NAT

开启条件（缺一不可）：
  1. /dev/net/tun 可打开
  2. 能执行 ip / sysctl / iptables（root + CAP_NET_ADMIN）
  3. 容器里没被限制（多数云容器会禁 TUN，或者禁 net.ipv4.ip_forward 的写入）

任何一步失败都会抛 TunError，上层据此回落到用户态转发。
所以这里的写法是「能成就成，不成也好退」，不做细粒度降级。

数据流：
  客户端 TUN 包 → 隧道 → 服务端握手会话 → 分配到虚拟 IP 10.7.x.y
      → write_packet() 写进 zftun0
      → 内核路由 + MASQUERADE
      → 外网
  外网回包 → 内核按 10.7.x.y 路由回 zftun0
      → 读线程 read_packet()
      → 按目的 IP 查会话表 → 回隧道
"""

import fcntl
import os
import struct
import subprocess
import threading

import conf
import proto

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000

TUN_DEV = "/dev/net/tun"


class TunError(Exception):
    """TUN 不可用。上层捕获后回落用户态转发。"""


def _run(cmd):
    """跑一条系统命令。失败抛 TunError，输出带进异常里方便排查。"""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = p.communicate(timeout=10)
    except Exception as e:
        raise TunError("%s 执行失败: %s" % (" ".join(cmd), e))
    if p.returncode != 0:
        raise TunError("%s 返回 %d: %s"
                       % (" ".join(cmd), p.returncode,
                          out.decode("utf-8", "replace").strip()))
    return out.decode("utf-8", "replace")


# ---------------------------------------------------------------- 地址池

class AddressPool(object):
    """给会话分配虚拟 IP。10.7.0.2 起，跳过 .1（网关）。

    预分配整段，避免每次都算；回收后可以复用。
    """

    def __init__(self, cidr=None, start=2):
        self.cidr = cidr or conf.TUN_CIDR
        net = self.cidr.split("/")[0]
        p = net.split(".")
        self.prefix = ".".join(p[:3])
        self.start = start
        self.end = 254
        self.used = set()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            for i in range(self.start, self.end + 1):
                if i not in self.used:
                    self.used.add(i)
                    return "%s.%d" % (self.prefix, i)
        return None

    def release(self, ip):
        if not ip:
            return
        try:
            last = int(ip.rsplit(".", 1)[1])
        except Exception:
            return
        with self.lock:
            self.used.discard(last)

    def __len__(self):
        with self.lock:
            return len(self.used)


# ---------------------------------------------------------------- TUN 设备

class TunDevice(object):
    """zftun0 的打开、配置、读写、清理。"""

    def __init__(self, name, cidr, mtu):
        self.name = name
        self.cidr = cidr
        self.mtu = mtu
        self.fd = -1
        self.configured = False

    @classmethod
    def open(cls, name=None, cidr=None, mtu=None):
        dev = cls(name or conf.TUN_NAME, cidr or conf.TUN_CIDR, mtu or conf.TUN_MTU)
        dev._open()
        dev._configure()
        return dev

    def _open(self):
        if not os.path.exists(TUN_DEV):
            raise TunError("%s 不存在（容器可能未挂载 TUN）" % TUN_DEV)

        try:
            fd = os.open(TUN_DEV, os.O_RDWR)
        except OSError as e:
            raise TunError("打开 %s 失败: %s" % (TUN_DEV, e))

        # ifreq: name(16) + flags(2) + pad(22) = 40
        ifr = struct.pack("16sH", self.name.encode("utf-8"), IFF_TUN | IFF_NO_PI)
        ifr += b"\x00" * (40 - len(ifr))
        try:
            fcntl.ioctl(fd, TUNSETIFF, ifr)
        except OSError as e:
            os.close(fd)
            raise TunError("TUNSETIFF 失败（需要 CAP_NET_ADMIN）: %s" % e)

        self.fd = fd

    def _configure(self):
        """配地址、MTU、up，开转发和 NAT。任何一步失败都回滚。"""
        steps = [
            ["ip", "addr", "add", self.cidr, "dev", self.name],
            ["ip", "link", "set", "dev", self.name, "mtu", str(self.mtu)],
            ["ip", "link", "set", "dev", self.name, "up"],
            ["sysctl", "-w", "net.ipv4.ip_forward=1"],
            ["iptables", "-A", "FORWARD", "-i", self.name, "-j", "ACCEPT"],
            ["iptables", "-A", "FORWARD", "-o", self.name, "-j", "ACCEPT"],
            ["iptables", "-t", "nat", "-A", "POSTROUTING",
             "-s", self.cidr, "-o", "!%s" % self.name, "-j", "MASQUERADE"],
        ]
        done = []
        try:
            for cmd in steps:
                # 地址已存在时会报 File exists，这不算错
                try:
                    _run(cmd)
                except TunError as e:
                    msg = str(e)
                    if "File exists" in msg or "already exists" in msg:
                        done.append(cmd)
                        continue
                    raise
                done.append(cmd)
                if cmd[0] == "iptables" and "-s" in cmd:
                    done[-1] = cmd
        except TunError:
            self._rollback(done)
            self.close()
            raise
        self.configured = True

    def _rollback(self, done):
        for cmd in reversed(done):
            undo = list(cmd)
            if undo[0] == "iptables":
                undo[2] = "-D"
                try:
                    _run(undo)
                except TunError:
                    pass
            elif undo[0] == "ip" and "addr" in undo:
                try:
                    _run(["ip", "addr", "del", self.cidr, "dev", self.name])
                except TunError:
                    pass
            elif undo[0] == "ip" and "link" in undo:
                try:
                    _run(["ip", "link", "del", self.name])
                except TunError:
                    pass

    # ------------------------------------------------------------ 读写

    def read_packet(self):
        """阻塞读一个 IP 包。设备关闭时返回 None。

        IFF_NO_PI 下读出来的就是裸 IP 包，不用剥 4 字节元数据。
        """
        if self.fd < 0:
            return None
        try:
            return os.read(self.fd, self.mtu + 128)
        except OSError:
            return None

    def write_packet(self, pkt):
        if self.fd < 0 or not pkt:
            return False
        try:
            os.write(self.fd, pkt)
            return True
        except OSError:
            return False

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        if self.configured:
            for cmd in (["iptables", "-t", "nat", "-D", "POSTROUTING",
                         "-s", self.cidr, "-o", "!%s" % self.name, "-j", "MASQUERADE"],
                        ["iptables", "-D", "FORWARD", "-i", self.name, "-j", "ACCEPT"],
                        ["iptables", "-D", "FORWARD", "-o", self.name, "-j", "ACCEPT"]):
                try:
                    _run(cmd)
                except TunError:
                    pass
            try:
                _run(["ip", "link", "del", self.name])
            except TunError:
                pass
            self.configured = False


# ---------------------------------------------------------------- 分发器

class TunRouter(object):
    """TUN 模式的会话分发。

    两个方向：
      * 入向（客户端 → 外网）：hold 住会话表，packet_from_client() 写进 TUN
      * 出向（外网 → 客户端）：read_loop() 读 TUN，按目的 IP 找会话回调
    """

    def __init__(self, pool=None):
        self.dev = None
        self.pool = pool or AddressPool()
        self.sessions = {}          # 虚拟 IP -> 回调对象（需有 send_ip_pkt(bytes)）
        self.lock = threading.Lock()
        self.closed = False
        self.stat_in = 0
        self.stat_out = 0
        self._thread = None
        self.error = ""

    # ------------------------------------------------------------ 生命周期

    def start(self, name=None, cidr=None, mtu=None):
        """打开 TUN 并起读线程。失败抛 TunError。"""
        self.dev = TunDevice.open(name, cidr, mtu)
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="tun-read")
        self._thread.start()
        return self

    def close(self):
        self.closed = True
        if self.dev is not None:
            self.dev.close()
        with self.lock:
            self.sessions.clear()

    # ------------------------------------------------------------ 会话

    def attach(self, session):
        """给会话分配虚拟 IP 并登记。返回 ip，池满返回 None。"""
        ip = self.pool.acquire()
        if ip is None:
            return None
        with self.lock:
            self.sessions[ip] = session
        return ip

    def detach(self, session):
        with self.lock:
            for ip, s in list(self.sessions.items()):
                if s is session:
                    self.sessions.pop(ip, None)
                    self.pool.release(ip)

    def ip_of(self, session):
        with self.lock:
            for ip, s in self.sessions.items():
                if s is session:
                    return ip
        return None

    # ------------------------------------------------------------ 入向

    def packet_from_client(self, pkt):
        """客户端来的包直接灌进内核。返回 True 表示已写。"""
        if self.dev is None or self.closed:
            return False
        ip = proto.parse_ipv4_packet(pkt)
        if ip is None:
            return False
        ok = self.dev.write_packet(pkt)
        if ok:
            self.stat_in += 1
        return ok

    # ------------------------------------------------------------ 出向

    def _read_loop(self):
        while not self.closed:
            pkt = self.dev.read_packet()
            if pkt is None:
                if self.closed:
                    return
                # fd 被外部关掉的情况，直接退出避免空转
                self.error = "TUN 读失败或已关闭"
                return
            if len(pkt) < 20:
                continue
            ip = proto.parse_ipv4_packet(pkt)
            if ip is None:
                continue
            with self.lock:
                target = self.sessions.get(ip["dst"])
            if target is None:
                continue
            try:
                target.send_ip_pkt(pkt)
                self.stat_out += 1
            except Exception:
                pass

    # ------------------------------------------------------------ 统计

    def stats(self):
        with self.lock:
            n = len(self.sessions)
        return {"mode": "tun", "device": self.dev.name if self.dev else "",
                "cidr": self.pool.cidr, "sessions": n,
                "in": self.stat_in, "out": self.stat_out, "error": self.error}
