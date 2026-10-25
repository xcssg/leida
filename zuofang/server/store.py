"""SQLite 存储层。

一个进程一个连接 + 一把锁。服务端是单进程多线程模型，
SQLite 的写并发本来就不高，串行化反而省心；
真要多实例部署，把这里换成 Postgres 即可（接口不变）。

表：
  cards         卡密主表
  card_devices  卡密绑定的设备（受 MAX_DEVICES 限制）
  tokens        登录令牌
  tickets       relay 握手票据（一次性）
  rooms         线路
  audit         操作审计
"""

import secrets
import sqlite3
import threading
import time

import cards as C
import conf

_LOCK = threading.RLock()
_CONN = None


# ---------------------------------------------------------------- 连接

def now():
    return int(time.time())


def conn():
    global _CONN
    if _CONN is None:
        # isolation_level=None：自己显式 BEGIN，避免隐式事务和 DDL 打架
        _CONN = sqlite3.connect(conf.DB_PATH, check_same_thread=False,
                                isolation_level=None, timeout=15.0)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA synchronous=NORMAL")
        _CONN.execute("PRAGMA foreign_keys=ON")
        _CONN.execute("PRAGMA busy_timeout=15000")
    return _CONN


SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_key     TEXT PRIMARY KEY,
    plan_days    INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    activated_at INTEGER,
    expires_at   INTEGER,
    status       INTEGER NOT NULL DEFAULT 0,   -- 0 未激活 1 正常 2 封禁
    batch        TEXT,
    note         TEXT,
    redeemed_by  TEXT                          -- 被哪张卡拿去续期了
);

CREATE INDEX IF NOT EXISTS idx_cards_status  ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cards_expires ON cards(expires_at);

CREATE TABLE IF NOT EXISTS card_devices (
    card_key   TEXT NOT NULL,
    device     TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    ua         TEXT,
    PRIMARY KEY (card_key, device)
);

CREATE TABLE IF NOT EXISTS tokens (
    token      TEXT PRIMARY KEY,
    card_key   TEXT NOT NULL,
    device     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tokens_card ON tokens(card_key);

CREATE TABLE IF NOT EXISTS tickets (
    ticket     TEXT PRIMARY KEY,
    card_key   TEXT NOT NULL,
    device     TEXT NOT NULL,
    room_id    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at    INTEGER
);

CREATE TABLE IF NOT EXISTS rooms (
    id         TEXT PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    capacity   INTEGER NOT NULL,
    online     INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    action   TEXT NOT NULL,
    card_key TEXT,
    device   TEXT,
    ip       TEXT,
    detail   TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


def init():
    with _LOCK:
        c = conn()
        c.executescript(SCHEMA)


def audit(action, card_key=None, device=None, ip=None, detail=None):
    with _LOCK:
        conn().execute(
            "INSERT INTO audit(ts,action,card_key,device,ip,detail) VALUES(?,?,?,?,?,?)",
            (now(), action, card_key, device, ip, detail),
        )


# ---------------------------------------------------------------- 卡密

def create_cards(count, days, batch=None, note=None):
    """生成并用一个事务写库，返回卡密列表。"""
    days = int(days)
    if days <= 0 or days > 3650:
        raise ValueError("时长必须在 1..3650 天之间")
    count = int(count)
    if count <= 0 or count > 5000:
        raise ValueError("单次生成数量必须在 1..5000 之间")

    keys = C.make_many(conf.SECRET, count)
    ts = now()
    with _LOCK:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.executemany(
                "INSERT INTO cards(card_key,plan_days,created_at,status,batch,note)"
                " VALUES(?,?,?,0,?,?)",
                [(k, days, ts, batch, note) for k in keys],
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    audit("generate", None, None, None, "count=%d days=%d batch=%s" % (count, days, batch))
    return keys


def get_card(key):
    k = C.normalize(key)
    if not k:
        return None
    with _LOCK:
        return conn().execute("SELECT * FROM cards WHERE card_key=?", (k,)).fetchone()


def card_view(row):
    """给客户端看的卡密视图。不含任何内部字段。"""
    if row is None:
        return None
    exp = int(row["expires_at"] or 0)
    t = now()
    return {
        "card_key": row["card_key"],
        "plan_days": int(row["plan_days"]),
        "status": int(row["status"]),
        "activated_at": row["activated_at"],
        "expires_at": exp,
        "remaining_seconds": max(0, exp - t) if exp else 0,
        "expired": bool(exp) and exp <= t,
        "note": row["note"] or "",
        "batch": row["batch"] or "",
    }


def card_is_usable(row):
    """能否用来登录/续票。返回 (ok, err)。"""
    if row is None:
        return False, "invalid_card"
    if int(row["status"]) == 2:
        return False, "card_banned"
    if row["redeemed_by"]:
        return False, "card_used"
    if row["activated_at"] and int(row["expires_at"] or 0) <= now():
        return False, "card_expired"
    return True, None


def activate(key, device, ip=None, ua=None):
    """首次激活：写 activated_at / expires_at / 绑定设备。

    返回 (row, err)。
    """
    k = C.normalize(key)
    ts = now()
    with _LOCK:
        c = conn()
        row = c.execute("SELECT * FROM cards WHERE card_key=?", (k,)).fetchone()
        if row is None:
            return None, "invalid_card"
        if int(row["status"]) == 2:
            return None, "card_banned"
        if row["redeemed_by"]:
            return None, "card_used"

        if row["activated_at"]:
            # 已激活：检查设备额度，然后放行（同设备重登是正常场景）
            n = c.execute("SELECT COUNT(*) AS n FROM card_devices WHERE card_key=?",
                          (k,)).fetchone()["n"]
            bound = c.execute("SELECT 1 FROM card_devices WHERE card_key=? AND device=?",
                              (k, device)).fetchone()
            if not bound and n >= conf.MAX_DEVICES:
                return None, "device_limit"
            if int(row["expires_at"] or 0) <= ts:
                return None, "card_expired"
        else:
            exp = ts + int(row["plan_days"]) * 86400
            c.execute(
                "UPDATE cards SET activated_at=?, expires_at=?, status=1 WHERE card_key=?",
                (ts, exp, k),
            )
            audit("activate", k, device, ip, "days=%s" % row["plan_days"])
            row = c.execute("SELECT * FROM cards WHERE card_key=?", (k,)).fetchone()

        c.execute(
            "INSERT INTO card_devices(card_key,device,first_seen,last_seen,ua)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(card_key,device) DO UPDATE SET last_seen=excluded.last_seen,"
            " ua=COALESCE(excluded.ua, card_devices.ua)",
            (k, device, ts, ts, ua),
        )
        return row, None


def extend(target_key, extra_key, device, ip=None):
    """用 extra_key 给 target_key 加时长。

    返回 (dict, err)。语义：
      - extra_key 必须未使用过（activated_at 为空）
      - 叠加基准取 max(now, target.expires_at)，不吞掉剩余时间
      - extra_key 被标记为已用（activated_at + redeemed_by 指向 target）
    """
    t = C.normalize(target_key)
    e = C.normalize(extra_key)
    ts = now()

    if t == e:
        return None, "self_redeem"

    with _LOCK:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            tgt = c.execute("SELECT * FROM cards WHERE card_key=?", (t,)).fetchone()
            if tgt is None:
                c.execute("ROLLBACK")
                return None, "invalid_target"
            if int(tgt["status"]) == 2:
                c.execute("ROLLBACK")
                return None, "card_banned"

            ext = c.execute("SELECT * FROM cards WHERE card_key=?", (e,)).fetchone()
            if ext is None:
                c.execute("ROLLBACK")
                return None, "invalid_card"
            if int(ext["status"]) == 2:
                c.execute("ROLLBACK")
                return None, "card_banned"
            if ext["activated_at"]:
                c.execute("ROLLBACK")
                return None, "card_used"

            base = max(ts, int(tgt["expires_at"] or 0))
            add = int(ext["plan_days"]) * 86400
            exp = base + add

            # 目标卡：延长到期时间
            c.execute(
                "UPDATE cards SET activated_at=COALESCE(activated_at,?), expires_at=?,"
                " status=1 WHERE card_key=?",
                (ts, exp, t),
            )
            # 消耗卡：标记已用，并记下续给了谁
            c.execute(
                "UPDATE cards SET activated_at=?, expires_at=?, status=1, redeemed_by=?"
                " WHERE card_key=?",
                (ts, exp, t, e),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    audit("extend", t, device, ip, "by=%s add=%dd" % (e, int(ext["plan_days"])))
    return {
        "days": int(ext["plan_days"]),
        "added_seconds": add,
        "expires_at": exp,
    }, None


def set_status(key, status):
    k = C.normalize(key)
    with _LOCK:
        conn().execute("UPDATE cards SET status=? WHERE card_key=?", (int(status), k))
    audit("status", k, None, None, "->%d" % int(status))
    return True


def unbind(key):
    k = C.normalize(key)
    with _LOCK:
        conn().execute("DELETE FROM card_devices WHERE card_key=?", (k,))
    audit("unbind", k)
    return True


def reset_expiry(key, days=None):
    """把到期时间重设为 now + days（默认沿用套餐天数）。"""
    k = C.normalize(key)
    with _LOCK:
        c = conn()
        row = c.execute("SELECT * FROM cards WHERE card_key=?", (k,)).fetchone()
        if row is None:
            return False
        d = int(days) if days else int(row["plan_days"])
        c.execute("UPDATE cards SET activated_at=?, expires_at=?, status=1 WHERE card_key=?",
                  (now(), now() + d * 86400, k))
    audit("reset", k, None, None, "days=%s" % days)
    return True


def delete_card(key):
    k = C.normalize(key)
    with _LOCK:
        c = conn()
        c.execute("DELETE FROM tokens WHERE card_key=?", (k,))
        c.execute("DELETE FROM card_devices WHERE card_key=?", (k,))
        c.execute("DELETE FROM tickets WHERE card_key=?", (k,))
        n = c.execute("DELETE FROM cards WHERE card_key=?", (k,)).rowcount
    audit("delete", k)
    return n > 0


def list_cards(status=None, batch=None, limit=200, offset=0, keyword=None):
    sql = "SELECT * FROM cards WHERE 1=1"
    args = []
    if status is not None:
        sql += " AND status=?"
        args.append(int(status))
    if batch:
        sql += " AND batch=?"
        args.append(batch)
    if keyword:
        sql += " AND card_key LIKE ?"
        args.append("%" + C.normalize(keyword) + "%")
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    args.extend([int(limit), int(offset)])
    with _LOCK:
        rows = conn().execute(sql, args).fetchall()
    out = []
    for r in rows:
        v = card_view(r)
        v["redeemed_by"] = r["redeemed_by"] or ""
        v["devices"] = devices_of(r["card_key"])
        out.append(v)
    return out


def devices_of(key):
    k = C.normalize(key)
    with _LOCK:
        rows = conn().execute(
            "SELECT device,first_seen,last_seen,ua FROM card_devices WHERE card_key=?"
            " ORDER BY last_seen DESC", (k,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- token

def issue_token(card_key, device):
    tok = secrets.token_urlsafe(32)
    ts = now()
    exp = ts + conf.TOKEN_DAYS * 86400
    with _LOCK:
        c = conn()
        # 同一设备旧 token 直接作废，避免 token 越积越多
        c.execute("UPDATE tokens SET revoked=1 WHERE card_key=? AND device=? AND revoked=0",
                  (card_key, device))
        c.execute(
            "INSERT INTO tokens(token,card_key,device,created_at,last_seen,expires_at,revoked)"
            " VALUES(?,?,?,?,?,?,0)",
            (tok, card_key, device, ts, ts, exp),
        )
    return tok, exp


def resolve_token(token):
    """校验 token 并返回 (card_row, token_row, err)。

    err 取值：missing_token / bad_token / token_expired / card_banned / card_expired

    先看卡状态，再看 token 是否被吊销：封禁卡时会顺手 revoke 该卡全部 token，
    若先判 revoked，客户端只会得到「未登录」，无法提示「已被封禁」。
    """
    if not token:
        return None, None, "missing_token"
    with _LOCK:
        c = conn()
        t = c.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()
        if t is None:
            return None, None, "bad_token"
        if int(t["expires_at"]) <= now():
            return None, None, "token_expired"
        row = c.execute("SELECT * FROM cards WHERE card_key=?", (t["card_key"],)).fetchone()
        if row is None:
            return None, None, "bad_token"
        if int(row["status"]) == 2:
            return None, None, "card_banned"
        if int(row["expires_at"] or 0) <= now():
            return None, None, "card_expired"
        if int(t["revoked"]) == 1:
            return None, None, "bad_token"
        return row, t, None


def touch_token(token, ua=None):
    ts = now()
    with _LOCK:
        c = conn()
        t = c.execute("SELECT card_key,device FROM tokens WHERE token=?", (token,)).fetchone()
        if t is None:
            return
        c.execute("UPDATE tokens SET last_seen=? WHERE token=?", (ts, token))
        c.execute(
            "INSERT INTO card_devices(card_key,device,first_seen,last_seen,ua)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(card_key,device) DO UPDATE SET last_seen=excluded.last_seen,"
            " ua=COALESCE(excluded.ua, card_devices.ua)",
            (t["card_key"], t["device"], ts, ts, ua),
        )


def revoke_token(token):
    with _LOCK:
        conn().execute("UPDATE tokens SET revoked=1 WHERE token=?", (token,))


def revoke_card_tokens(card_key):
    with _LOCK:
        n = conn().execute("UPDATE tokens SET revoked=1 WHERE card_key=? AND revoked=0",
                           (card_key,)).rowcount
    return n


def live_tokens(card_key):
    with _LOCK:
        rows = conn().execute(
            "SELECT device,last_seen FROM tokens WHERE card_key=? AND revoked=0"
            " AND expires_at>?", (card_key, now())).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 票据

def issue_ticket(card_key, device, room_id):
    """签发 relay 握手票据。一次性、短时、绑定设备和线路。"""
    tk = secrets.token_urlsafe(24)
    ts = now()
    with _LOCK:
        c = conn()
        # 清理过期票据，顺手控表大小
        c.execute("DELETE FROM tickets WHERE expires_at<?", (ts - 3600,))
        c.execute(
            "INSERT INTO tickets(ticket,card_key,device,room_id,created_at,expires_at)"
            " VALUES(?,?,?,?,?,?)",
            (tk, card_key, device, room_id, ts, ts + conf.TICKET_TTL),
        )
    return tk, ts + conf.TICKET_TTL


def redeem_ticket(ticket, device=None):
    """relay 握手时调用。返回 (info, err)。

    info = {card_key, device, room_id, expires_at}
    消费即作废（used_at 落库），重放会被拒。
    """
    if not ticket:
        return None, "missing_ticket"
    ts = now()
    with _LOCK:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            t = c.execute("SELECT * FROM tickets WHERE ticket=?", (ticket,)).fetchone()
            if t is None:
                c.execute("ROLLBACK")
                return None, "bad_ticket"
            if t["used_at"]:
                c.execute("ROLLBACK")
                return None, "ticket_used"
            if int(t["expires_at"]) <= ts:
                c.execute("ROLLBACK")
                return None, "ticket_expired"
            if device is not None and device != t["device"]:
                c.execute("ROLLBACK")
                return None, "ticket_device"
            row = c.execute("SELECT * FROM cards WHERE card_key=?", (t["card_key"],)).fetchone()
            if row is None:
                c.execute("ROLLBACK")
                return None, "bad_ticket"
            if int(row["status"]) == 2:
                c.execute("ROLLBACK")
                return None, "card_banned"
            if int(row["expires_at"] or 0) <= ts:
                c.execute("ROLLBACK")
                return None, "card_expired"
            c.execute("UPDATE tickets SET used_at=? WHERE ticket=?", (ts, ticket))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return {
        "card_key": t["card_key"],
        "device": t["device"],
        "room_id": t["room_id"],
        "expires_at": int(t["expires_at"]),
    }, None


# ---------------------------------------------------------------- 线路

def ensure_rooms(count=None):
    """首次启动建几条线路。已存在则不动。"""
    n = conf.DEFAULT_ROOMS if count is None else int(count)
    if n <= 0:
        return
    ts = now()
    with _LOCK:
        c = conn()
        have = c.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()["n"]
        if have:
            return
        for i in range(n):
            rid = secrets.token_hex(8)
            code = "".join(secrets.choice(C.ALPHABET) for _ in range(6))
            c.execute(
                "INSERT INTO rooms(id,code,title,capacity,online,created_at)"
                " VALUES(?,?,?,?,0,?)",
                (rid, code, "线路 %d" % (i + 1), conf.ROOM_CAPACITY, ts),
            )


def list_rooms():
    with _LOCK:
        rows = conn().execute("SELECT * FROM rooms ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


def find_room(room_id=None, code=None):
    with _LOCK:
        c = conn()
        if room_id:
            return c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if code:
            return c.execute("SELECT * FROM rooms WHERE code=?",
                             (str(code).strip().upper(),)).fetchone()
    return None


def bump_online(room_id, delta):
    with _LOCK:
        conn().execute(
            "UPDATE rooms SET online=MAX(0, online+?) WHERE id=?", (int(delta), room_id))


# ---------------------------------------------------------------- 统计

def stats():
    ts = now()
    with _LOCK:
        c = conn()
        q = lambda sql, a=(): c.execute(sql, a).fetchone()[0]
        return {
            "cards_total": q("SELECT COUNT(*) FROM cards"),
            "cards_unused": q("SELECT COUNT(*) FROM cards WHERE activated_at IS NULL"
                              " AND status!=2 AND redeemed_by IS NULL"),
            "cards_active": q("SELECT COUNT(*) FROM cards WHERE status=1 AND expires_at>?"
                              " AND redeemed_by IS NULL", (ts,)),
            "cards_banned": q("SELECT COUNT(*) FROM cards WHERE status=2"),
            "cards_expired": q("SELECT COUNT(*) FROM cards WHERE status!=2 AND expires_at<=?"
                               " AND redeemed_by IS NULL", (ts,)),
            "cards_redeemed": q("SELECT COUNT(*) FROM cards WHERE redeemed_by IS NOT NULL"),
            "tokens_live": q("SELECT COUNT(*) FROM tokens WHERE revoked=0 AND expires_at>?", (ts,)),
            "devices": q("SELECT COUNT(*) FROM card_devices"),
            "rooms": q("SELECT COUNT(*) FROM rooms"),
            "revenue_days": q("SELECT COALESCE(SUM(plan_days),0) FROM cards WHERE activated_at IS NOT NULL"),
            "server_time": ts,
        }


def audit_list(limit=200):
    with _LOCK:
        rows = conn().execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?",
                              (int(limit),)).fetchall()
    return [dict(r) for r in rows]
