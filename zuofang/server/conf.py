"""ZUOFANG 服务端配置。

所有可调项都从环境变量读，没设就用这里的默认值。
改配置不用改代码，重启进程即可（密钥除外，见 SECRET）。
"""

import os
import secrets

VERSION = "1.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

# ---------------------------------------------------------------- 基础

def _env(key, default=None):
    v = os.environ.get(key)
    if v is None or v.strip() == "":
        return default
    return v.strip()


def _env_int(key, default):
    v = _env(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_bool(key, default=False):
    v = _env(key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


HOST = _env("ZF_HOST", "0.0.0.0")
PORT = _env_int("ZF_PORT", 8090)

# UDP 中继监听地址。和 HTTP 同一个进程，只占另一个端口。
RELAY_LISTEN = _env("ZF_RELAY_LISTEN", "0.0.0.0:9000")

# 下发给客户端的 relay 地址。留空则按请求的 Host 推导（同机部署最省事）。
# 中继和 API 不在同一台机器时，这里必须写死成客户端可达的 IP:端口。
PUBLIC_RELAY = _env("ZF_PUBLIC_RELAY", "")

# 反向代理后的外网地址，用于拼 WebSocket URL，例如 https://api.example.com
PUBLIC_BASE = _env("ZF_PUBLIC_BASE", "")

# ---------------------------------------------------------------- 存储

DB_PATH = _env("ZF_DB", os.path.join(BASE_DIR, "zuofang.db"))
SECRET_FILE = _env("ZF_SECRET_FILE", os.path.join(BASE_DIR, ".zf_secret"))


def _load_secret():
    """卡密校验码密钥。

    环境变量 ZF_SECRET 优先；否则读 SECRET_FILE；都没有就生成并落盘。
    这个值换了等于换了一套卡密体系，历史卡密全部判废。
    """
    env = os.environ.get("ZF_SECRET")
    if env and env.strip():
        return env.strip().encode("utf-8")

    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "rb") as f:
            data = f.read().strip()
        if data:
            return data

    data = secrets.token_urlsafe(32).encode("utf-8")
    d = os.path.dirname(SECRET_FILE)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(SECRET_FILE, "wb") as f:
        f.write(data)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return data


SECRET = _load_secret()

ADMIN_KEY = _env("ZF_ADMIN_KEY")
if not ADMIN_KEY:
    ADMIN_KEY = "zf-" + secrets.token_hex(9)

# ---------------------------------------------------------------- 授权时长

# token 有效期（天）。客户端每次心跳会续命，所以实际远长于这个值也能活着。
TOKEN_DAYS = _env_int("ZF_TOKEN_DAYS", 30)

# 心跳超过这个秒数没上报，token 视为失效（防止一台设备无限挂着）。
HEARTBEAT_STALE = _env_int("ZF_HEARTBEAT_STALE", 600)

# join 票据有效期（秒）。票据只用于换一次握手资格，短一点更安全。
TICKET_TTL = _env_int("ZF_TICKET_TTL", 120)

# UDP 会话空闲多久回收（秒）。
SESSION_IDLE = _env_int("ZF_SESSION_IDLE", 120)

# 会话状态复查周期（秒）。封禁/到期靠它踢人，所以不能太长。
SESSION_CHECK = _env_int("ZF_SESSION_CHECK", 15)

# 单张卡最多绑定几台设备。1 = 严格单设备。
MAX_DEVICES = _env_int("ZF_MAX_DEVICES", 1)

# ---------------------------------------------------------------- 网络

DNS = _env("ZF_DNS", "1.1.1.1")
MTU = _env_int("ZF_MTU", 1400)

# 出网模式：auto 先试 TUN，失败回落到用户态转发；on 强制 TUN；off 只用用户态。
NET_MODE = _env("ZF_NET_MODE", "auto").lower()

TUN_NAME = _env("ZF_TUN_NAME", "zftun0")
TUN_CIDR = _env("ZF_TUN_CIDR", "10.7.0.1/16")
TUN_MTU = _env_int("ZF_TUN_MTU", MTU)

# 用户态转发时，单个会话允许的并发 UDP 流数量。
UDP_FLOW_MAX = _env_int("ZF_UDP_FLOW_MAX", 256)

# 是否放行内网/回环目标。默认关，避免卡密用户拿隧道扫服务器内网。
ALLOW_PRIVATE = _env_bool("ZF_ALLOW_PRIVATE", False)

# ---------------------------------------------------------------- 限流

# 激活接口：每个 IP 在 RATE_WINDOW 秒内最多 RATE_ACTIVATE 次。
RATE_ACTIVATE = _env_int("ZF_RATE_ACTIVATE", 8)
RATE_WINDOW = _env_int("ZF_RATE_WINDOW", 60)

# 连续格式错误达到阈值，直接拉黑 IP 一段时间。
BAD_FORMAT_LIMIT = _env_int("ZF_BAD_FORMAT_LIMIT", 20)
BAD_FORMAT_BAN = _env_int("ZF_BAD_FORMAT_BAN", 300)

# ---------------------------------------------------------------- 房间

# 首次启动自动建几条线路，省得开箱空列表。
DEFAULT_ROOMS = _env_int("ZF_DEFAULT_ROOMS", 3)
ROOM_CAPACITY = _env_int("ZF_ROOM_CAPACITY", 100)

# 单行日志开关。生产环境建议关掉。
VERBOSE = _env_bool("ZF_VERBOSE", False)


def relay_addr():
    """(host, port) 形式的本地 relay 监听地址。"""
    host, _, port = RELAY_LISTEN.rpartition(":")
    if not host:
        host = "0.0.0.0"
    try:
        return host, int(port)
    except ValueError:
        return host, 9000
