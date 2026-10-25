"""HTTP 层：卡密 API + 管理台 + WebSocket 升级。

路由必须和客户端 `app/lib/api.dart` 严格对齐 —— 全部挂在 `/api/card/*`
和 `/api/*` 下。改动前先确认 Dart 侧的路径，别一厢情愿换名字。

鉴权模型：
  * 卡密即账号，没有注册
  * `activate` 校验卡密格式 + 状态，绑设备，发 token
  * 之后所有请求带 `X-Card-Token` 与 `X-Device-Token`
  * `join` 签发一次性 relay 票据，票据才是真的入网凭证

限流：
  * 激活接口按 IP 限速，防撞库
  * 连续格式错误拉黑，防爆破

管理台：
  * `GET /admin` 返回单文件控制台
  * 其余 `/admin/*` 要 `X-Admin-Key` 或 `?admin_key=`
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

import cards as C
import conf
import store
import ws


# ---------------------------------------------------------------- 限流

class RateLimiter(object):
    """按 IP 计数的滑动窗口 + 拉黑名单。"""

    def __init__(self, limit, window, bad_limit, bad_ban):
        self.limit = limit
        self.window = window
        self.bad_limit = bad_limit
        self.bad_ban = bad_ban
        self.hits = {}          # ip -> [ts, ...]
        self.bad = {}           # ip -> (count, since)
        self.ban = {}           # ip -> until
        self.lock = threading.Lock()

    def _banned(self, ip, now):
        until = self.ban.get(ip)
        if until is None:
            return False
        if now >= until:
            self.ban.pop(ip, None)
            return False
        return True

    def allow(self, ip):
        now = time.time()
        with self.lock:
            if self._banned(ip, now):
                return False
            arr = self.hits.get(ip)
            if arr is None:
                arr = []
                self.hits[ip] = arr
            # 丢掉窗口外的
            cut = now - self.window
            while arr and arr[0] < cut:
                arr.pop(0)
            if len(arr) >= self.limit:
                return False
            arr.append(now)
            return True

    def mark_bad(self, ip):
        """记一次格式错误。超阈值直接拉黑。返回是否已拉黑。"""
        now = time.time()
        with self.lock:
            cnt, since = self.bad.get(ip, (0, now))
            if now - since > self.window * 5:
                cnt, since = 0, now
            cnt += 1
            self.bad[ip] = (cnt, since)
            if cnt >= self.bad_limit:
                self.ban[ip] = now + self.bad_ban
                self.bad[ip] = (0, now)
                return True
            return False

    def reset_bad(self, ip):
        with self.lock:
            self.bad.pop(ip, None)

    def sweep(self):
        """清掉很久没动的记录，避免无界增长。"""
        now = time.time()
        cut = now - max(self.window, 3600)
        with self.lock:
            for ip in [k for k, v in self.hits.items() if not v or v[-1] < cut]:
                self.hits.pop(ip, None)
            for ip in [k for k, v in self.bad.items() if v[1] < cut]:
                self.bad.pop(ip, None)
            for ip in [k for k, v in self.ban.items() if v < now]:
                self.ban.pop(ip, None)


ACTIVATE_LIMIT = RateLimiter(conf.RATE_ACTIVATE, conf.RATE_WINDOW,
                             conf.BAD_FORMAT_LIMIT, conf.BAD_FORMAT_BAN)


# ---------------------------------------------------------------- 错误码文案

ERRORS = {
    "missing_card": (400, "请填写卡密"),
    "bad_format": (400, "卡密格式错误，应为 ZF-XXXX-XXXX-XXXX-XXXX"),
    "invalid_card": (404, "卡密不存在"),
    "card_banned": (403, "卡密已被封禁"),
    "card_used": (409, "这张卡密已被使用（已用于续期）"),
    "card_expired": (402, "卡密已到期，请使用新卡续期"),
    "device_limit": (409, "该卡密已绑定其它设备，请联系管理员解绑"),
    "device_mismatch": (409, "设备与卡密绑定不一致"),
    "self_redeem": (400, "不能用自己的卡密给自己续期"),
    "invalid_target": (401, "当前卡密无效"),
    "unauthorized": (401, "卡密未登录或已过期"),
    "subscription_expired": (402, "卡密已到期，请先续期"),
    "room_not_found": (404, "线路不存在或线路码无效"),
    "room_full": (409, "线路已满，请换一条"),
    "rate_limited": (429, "操作太频繁，请稍后再试"),
    "ip_banned": (429, "尝试次数过多，已临时封禁"),
    "too_many": (400, "参数超出范围"),
    "not_found": (404, "接口不存在"),
}


def err(code_name, extra=None):
    code, msg = ERRORS.get(code_name, (400, code_name))
    o = {"code": code_name, "message": msg}
    if extra:
        o.update(extra)
    return code, o


# ---------------------------------------------------------------- 业务

def _card_payload(row, device=""):
    """客户端 `CardInfo.fromJson` 需要的字段，缺一个就显示成空。"""
    if row is None:
        return None
    v = store.card_view(row)
    if v is None:
        return None
    devs = store.devices_of(row["card_key"])
    bound = devs[0]["device"] if devs else ""
    v["bound_device"] = bound
    v["device_ok"] = (not bound) or (not device) or bound == device
    v["server_time"] = store.now()
    return v


def _relay_url(handler):
    """下发给客户端的 relay 地址。"""
    if conf.PUBLIC_RELAY:
        addr = conf.PUBLIC_RELAY
    else:
        addr = conf.RELAY_LISTEN
    if "://" in addr:
        return addr
    return "udp://" + addr


def _base_url(handler):
    if conf.PUBLIC_BASE:
        return conf.PUBLIC_BASE.rstrip("/")
    host = handler.headers.get("host") or ("%s:%d" % (conf.HOST, conf.PORT))
    return "http://" + host


def _join(handler, ident, room_id=None):
    """签发 relay 票据。这是白嫖的唯一入口，所以校验要严。"""
    card_key = ident["card_key"]
    device = ident["device"]

    if room_id is None:
        body = handler.body()
        code = body.get("room_code") or body.get("code") or ""
        room = store.find_room(code=code)
    else:
        room = store.find_room(room_id=room_id)

    if room is None:
        return handler.json(*err("room_not_found"))
    if int(room["online"]) >= int(room["capacity"]):
        return handler.json(*err("room_full"))

    ticket, texp = store.issue_ticket(card_key, device, room["id"])
    sid = _session_id(card_key, device, room["id"])

    store.audit("join", card_key, device, handler.ip(), "room=%s" % room["code"])

    return handler.json(200, {
        "code": "ok",
        "session_id": sid,
        "relay": _relay_url(handler),
        "token": ticket,
        "ticket": ticket,
        "ticket_expires_at": texp,
        "expires_at": int(handler.card_row["expires_at"] or 0),
        "room": {
            "id": room["id"], "code": room["code"], "title": room["title"],
            "online": int(room["online"]), "capacity": int(room["capacity"]),
        },
        "server_time": store.now(),
        "protocol": "zuofang.client.session.v1",
    })


def _session_id(card_key, device, room_id):
    """会话 id 必须稳定 —— 客户端重连时沿用同一个，中继才能替换旧会话。

    用 FNV-1a 64 把三元组压成一个 u64，再转成**十进制字符串**下发。

    为什么是十进制而不是十六进制：
      Rust 侧 `Config::session_u64()` 先尝试 `parse::<u64>()`，解析成功就
      直接采用，不再做 FNV。下发十进制能让两边拿到完全相同的数值；
      下发 hex 会被当成十进制解析（或解析失败后再哈希一遍），
      服务端日志里的会话 id 就对不上客户端实际用的那个了。
    """
    raw = ("%s|%s|%s" % (card_key, device, room_id)).encode("utf-8")
    h = 0xCBF29CE484222325
    for b in raw:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return str(h)


# ---------------------------------------------------------------- Handler

class Handler(BaseHTTPRequestHandler):
    server_version = "ZuoFang/" + conf.VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------

    def log_message(self, fmt, *args):
        if conf.VERBOSE:
            print("[http] %s %s" % (self.address_string(), fmt % args))

    def ip(self):
        fwd = self.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def body(self):
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 1 << 20:
            return {}
        try:
            v = json.loads(self.rfile.read(n).decode("utf-8"))
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    def query(self):
        return parse_qs(urlparse(self.path).query)

    def json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers",
                         "content-type,X-Card-Token,X-Device-Token,X-Admin-Key")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
        self.send_header("cache-control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def raw(self, code, ctype, data):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    # ---------- 鉴权 ----------

    def token(self):
        t = self.headers.get("X-Card-Token")
        if t:
            return t.strip()
        q = self.query().get("token")
        return (q or [None])[0]

    def device(self):
        return (self.headers.get("X-Device-Token") or "").strip()

    def auth(self):
        """校验 token。成功返回 (card_row, token_row, ident)，失败已回响应返回 None。"""
        tok = self.token()
        row, trow, e = store.resolve_token(tok)
        if e is not None:
            name = "unauthorized"
            if e == "card_banned":
                name = "card_banned"
            elif e == "card_expired":
                name = "card_expired"
            self.json(*err(name))
            return None

        dev = self.device()
        bound = trow["device"]
        if dev and bound and dev != bound:
            self.json(*err("device_mismatch"))
            return None

        self.card_row = row
        self.token_row = trow
        return {"card_key": row["card_key"], "device": bound or dev, "token": tok}

    def admin_ok(self):
        key = self.headers.get("X-Admin-Key") or (self.query().get("admin_key") or [""])[0]
        return bool(key) and key == conf.ADMIN_KEY

    # ---------- 路由 ----------

    def do_OPTIONS(self):
        self.json(204, {})

    def do_GET(self):
        p = urlparse(self.path).path

        if p in ("/", "/api"):
            return self.json(200, {
                "name": "ZUOFANG",
                "version": conf.VERSION,
                "docs": "/api/docs",
                "admin": "/admin",
            })

        if p == "/api/docs":
            return self.json(200, DOCS)

        if p == "/api/health":
            return self.json(200, {"ok": True, "time": store.now(),
                                   "relay": _relay_url(self),
                                   "mode": self.server.relay.mode if self.server.relay else ""})

        if p == "/api/card/me":
            ident = self.auth()
            if ident is None:
                return
            return self.json(200, _card_payload(self.card_row, ident["device"]))

        if p == "/api/rooms":
            if self.auth() is None:
                return
            return self.json(200, {"rooms": _rooms_payload(self)})

        if p.startswith("/api/rooms/"):
            if self.auth() is None:
                return
            rid = p[len("/api/rooms/"):]
            r = store.find_room(room_id=rid)
            if r is None:
                return self.json(*err("room_not_found"))
            return self.json(200, _room_payload(r))

        if p == "/api/client-config":
            return self.json(200, {
                "mtu": conf.MTU,
                "dns": conf.DNS,
                "capture_enabled": True,
                "notice": "",
                "relay": _relay_url(self),
                "mode": self.server.relay.mode if self.server.relay else "userspace",
                "protocol": "zuofang.client.session.v1",
                "server_time": store.now(),
            })

        if p == "/api/state":
            if self.auth() is None:
                return
            return self.json(200, self._state_payload())

        if p == "/api/state_stream_url":
            if self.auth() is None:
                return
            base = _base_url(self)
            base = base.replace("https://", "wss://").replace("http://", "ws://")
            return self.json(200, {
                "url": "%s/api/state/stream?token=%s" % (base, self.token() or ""),
                "protocol": "zuofang.client.session.v1",
            })

        if p == "/api/state/stream":
            return self._ws()

        # ---------- 管理 ----------
        if p == "/admin":
            return self._admin_page()

        if p.startswith("/admin/"):
            if not self.admin_ok():
                return self.json(*err("unauthorized"))
            return self._admin_get(p)

        return self.json(*err("not_found"))

    def do_POST(self):
        p = urlparse(self.path).path

        if p == "/api/card/activate":
            return self._activate()
        if p == "/api/card/heartbeat":
            return self._heartbeat()
        if p == "/api/card/renew":
            return self._renew()
        if p == "/api/card/logout":
            return self._logout()
        if p == "/api/join":
            ident = self.auth()
            if ident is None:
                return
            return _join(self, ident)
        if p.startswith("/api/rooms/") and p.endswith("/join"):
            ident = self.auth()
            if ident is None:
                return
            return _join(self, ident, room_id=p[len("/api/rooms/"):-len("/join")])

        if p.startswith("/admin/"):
            if not self.admin_ok():
                return self.json(*err("unauthorized"))
            return self._admin_post(p, self.body())

        return self.json(*err("not_found"))

    # ---------- 卡密业务 ----------

    def _activate(self):
        ip = self.ip()
        if not ACTIVATE_LIMIT.allow(ip):
            return self.json(*err("rate_limited"))

        b = self.body()
        raw = b.get("card_key") or ""
        device = (b.get("device") or self.device() or "").strip()

        key = C.normalize(raw)
        if not key:
            ACTIVATE_LIMIT.mark_bad(ip)
            return self.json(*err("missing_card"))
        if not C.check_format(key, conf.SECRET):
            if ACTIVATE_LIMIT.mark_bad(ip):
                return self.json(*err("ip_banned"))
            return self.json(*err("bad_format"))
        ACTIVATE_LIMIT.reset_bad(ip)

        row, e = store.activate(key, device, ip, self.headers.get("user-agent"))
        if e is not None:
            name = e
            if e == "device_limit":
                name = "device_mismatch"
            return self.json(*err(name, {"bound_device": _bound_device(key)}))

        tok, texp = store.issue_token(key, device)
        store.audit("login", key, device, ip, "ua=%s" % (self.headers.get("user-agent") or "")[:80])

        return self.json(200, {
            "code": "ok",
            "token": tok,
            "card_token": tok,
            "token_expires_at": texp,
            "card": _card_payload(row, device),
            "relay": _relay_url(self),
            "server_time": store.now(),
        })

    def _heartbeat(self):
        ident = self.auth()
        if ident is None:
            return
        # touch_token 内部已经顺带更新了 card_devices 的 last_seen / ua
        store.touch_token(ident["token"], self.headers.get("user-agent"))
        return self.json(200, _card_payload(self.card_row, ident["device"]))

    def _renew(self):
        ident = self.auth()
        if ident is None:
            return
        b = self.body()
        raw = b.get("card_key") or ""
        key = C.normalize(raw)
        if not key:
            return self.json(*err("missing_card"))
        if not C.check_format(key, conf.SECRET):
            return self.json(*err("bad_format"))

        res, e = store.extend(ident["card_key"], key, ident["device"], self.ip())
        if e is not None:
            return self.json(*err(e))

        fresh = store.get_card(ident["card_key"])
        return self.json(200, {
            "code": "ok",
            "days": res["days"],
            "added_seconds": res["added_seconds"],
            "expires_at": res["expires_at"],
            "server_time": store.now(),
            "card": _card_payload(fresh, ident["device"]),
        })

    def _logout(self):
        ident = self.auth()
        if ident is None:
            return
        store.revoke_token(ident["token"])
        store.audit("logout", ident["card_key"], ident["device"], self.ip())
        return self.json(200, {"ok": True})

    # ---------- 状态 ----------

    def _state_payload(self):
        relay = self.server.relay
        st = relay.stats() if relay is not None else {}
        exp = int(self.card_row["expires_at"] or 0)
        return {
            "server_time": store.now(),
            "online": st.get("sessions", 0),
            "ws_online": len(self.server.hub) if self.server.hub else 0,
            "remaining_seconds": max(0, exp - store.now()),
            "relay": _relay_url(self),
            "mode": st.get("mode", ""),
            "relay_stats": st,
        }

    def _ws(self):
        # 先鉴权再验升级头：没带 token 的普通 GET 应当被明确告知 401，
        # 而不是被当成「没有升级头 = 404」，否则客户端拿不到有效错误码。
        tok = (self.query().get("token") or [""])[0]
        row, trow, e = store.resolve_token(tok)
        if e is not None:
            return self.json(*err("card_banned" if e == "card_banned" else "unauthorized"))

        key = self.headers.get("sec-websocket-key")
        if not key:
            return self.json(400, {"code": "bad_request", "message": "缺少 Sec-WebSocket-Key"})

        if self.server.hub is None:
            return self.json(500, {"code": "no_hub", "message": "状态推送未启用"})

        accept = ws.accept_key(key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:
            return

        ws.serve_client(self, tok, row["card_key"])

    # ---------- 管理 ----------

    def _admin_page(self):
        p = os.path.join(conf.BASE_DIR, "admin.html")
        if not os.path.exists(p):
            return self.raw(404, "text/plain; charset=utf-8", b"admin.html missing")
        with open(p, "rb") as f:
            data = f.read()
        return self.raw(200, "text/html; charset=utf-8", data)

    def _admin_get(self, p):
        if p == "/admin/stats":
            st = store.stats()
            relay = self.server.relay
            st["relay"] = relay.stats() if relay is not None else {}
            st["ws_online"] = len(self.server.hub) if self.server.hub else 0
            st["sessions"] = relay.session_list() if relay is not None else []
            return self.json(200, st)

        if p == "/admin/cards":
            q = self.query()
            try:
                limit = min(2000, max(1, int((q.get("limit") or ["200"])[0])))
                offset = max(0, int((q.get("offset") or ["0"])[0]))
            except ValueError:
                limit, offset = 200, 0
            st = q.get("status")
            status = None
            if st and st[0] != "":
                try:
                    status = int(st[0])
                except ValueError:
                    status = None
            return self.json(200, {"cards": store.list_cards(
                status=status,
                batch=(q.get("batch") or [None])[0],
                keyword=(q.get("q") or [None])[0],
                limit=limit, offset=offset)})

        if p == "/admin/audit":
            q = self.query()
            try:
                limit = min(1000, max(1, int((q.get("limit") or ["200"])[0])))
            except ValueError:
                limit = 200
            return self.json(200, {"audit": store.audit_list(limit)})

        if p == "/admin/rooms":
            return self.json(200, {"rooms": store.list_rooms()})

        return self.json(*err("not_found"))

    def _admin_post(self, p, b):
        ip = self.ip()

        if p == "/admin/cards/generate":
            try:
                count = int(b.get("count") or 10)
                days = int(b.get("days") or 30)
            except ValueError:
                return self.json(*err("too_many"))
            count = max(1, min(2000, count))
            days = max(1, min(3650, days))
            keys = store.create_cards(count, days, batch=b.get("batch") or "api",
                                      note=b.get("note"))
            store.audit("generate", None, None, ip, "n=%d days=%d" % (count, days))
            return self.json(200, {"count": len(keys), "days": days, "cards": keys})

        key = C.normalize(b.get("card_key") or (self.query().get("card_key") or [""])[0])
        if not key:
            return self.json(*err("missing_card"))
        if not C.is_canonical(key):
            return self.json(*err("bad_format"))

        if p == "/admin/cards/ban":
            store.set_status(key, 2)
            store.revoke_card_tokens(key)
            n = self._kick(key)
            store.audit("ban", key, None, ip, "kicked=%d" % n)
            return self.json(200, {"ok": True, "kicked": n})

        if p == "/admin/cards/unban":
            store.set_status(key, 1)
            store.audit("unban", key, None, ip)
            return self.json(200, {"ok": True})

        if p == "/admin/cards/unbind":
            store.unbind(key)
            store.audit("unbind", key, None, ip)
            return self.json(200, {"ok": True})

        if p == "/admin/cards/reset":
            days = b.get("days")
            ok = store.reset_expiry(key, int(days) if days else None)
            store.audit("reset", key, None, ip, "days=%s" % days)
            return self.json(200, {"ok": ok})

        if p == "/admin/cards/delete":
            ok = store.delete_card(key)
            self._kick(key)
            store.audit("delete", key, None, ip)
            return self.json(200, {"ok": ok})

        if p == "/admin/cards/extend":
            by = C.normalize(b.get("by_card") or "")
            if not by:
                return self.json(*err("missing_card"))
            res, e = store.extend(key, by, "admin", ip)
            if e is not None:
                return self.json(*err(e))
            return self.json(200, {"ok": True, "days": res["days"],
                                   "expires_at": res["expires_at"]})

        if p == "/admin/cards/tokens":
            return self.json(200, {"tokens": store.live_tokens(key)})

        return self.json(*err("not_found"))

    def _kick(self, key):
        """封卡后立刻踢人：UDP 会话 + WS 连接都断。"""
        n = 0
        relay = self.server.relay
        if relay is not None:
            n += relay.kick_card(key)
        if self.server.hub is not None:
            self.server.hub.kick_card(key)
        return n


# ---------------------------------------------------------------- 辅助

def _bound_device(key):
    devs = store.devices_of(key)
    return devs[0]["device"] if devs else ""


def _room_payload(r):
    return {
        "id": r["id"], "code": r["code"], "title": r["title"],
        "online": int(r["online"]), "capacity": int(r["capacity"]),
        "expires_at": 0,
    }


def _rooms_payload(handler):
    relay = handler.server.relay
    live = {}
    if relay is not None:
        for s in relay.session_list():
            live[s["room_id"]] = live.get(s["room_id"], 0) + 1
    out = []
    for r in store.list_rooms():
        v = _room_payload(r)
        # 在线数用真实会话数，别用库里的累计值 —— 那个只在新旧版本混跑时才有偏差
        v["online"] = live.get(r["id"], 0)
        out.append(v)
    return out


DOCS = {
    "auth": "需要鉴权的接口带请求头 X-Card-Token: <token>",
    "device": "同时带 X-Device-Token: <设备指纹>，用于单设备绑定",
    "flow": [
        "1. POST /api/card/activate 拿 token",
        "2. GET  /api/rooms 选线路",
        "3. POST /api/join 拿一次性票据 ticket",
        "4. 用 ticket 做 TGCP 握手进 UDP 隧道",
    ],
    "endpoints": {
        "POST /api/card/activate": {"body": {"card_key": "ZF-...", "device": "..."}},
        "GET  /api/card/me": {},
        "POST /api/card/heartbeat": {},
        "POST /api/card/renew": {"body": {"card_key": "ZF-..."}},
        "POST /api/card/logout": {},
        "GET  /api/rooms": {},
        "POST /api/join": {"body": {"room_code": "ABC234"}},
        "POST /api/rooms/<id>/join": {},
        "GET  /api/client-config": {},
        "GET  /api/state": {},
        "GET  /api/state_stream_url": {},
        "WS  /api/state/stream?token=...": {},
    },
    "errors": sorted(ERRORS.keys()),
}


# ---------------------------------------------------------------- 服务器

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, relay=None, hub=None):
        HTTPServer.__init__(self, addr, handler)
        self.relay = relay
        self.hub = hub


def make_server(relay=None, hub=None, host=None, port=None):
    return ThreadingHTTPServer((host or conf.HOST, port or conf.PORT),
                               Handler, relay=relay, hub=hub)


def start_sweeper():
    """后台清限流记录，防止内存无界增长。"""
    def run():
        while True:
            time.sleep(600)
            ACTIVATE_LIMIT.sweep()

    t = threading.Thread(target=run, daemon=True, name="rate-sweep")
    t.start()
    return t
