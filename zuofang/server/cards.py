"""卡密编解码。

格式：ZF-XXXX-XXXX-XXXX-CCCC
  ZF          固定前缀
  XXXX x3     随机体，字符集 32 个，共 60 bit
  CCCC        校验码，由 SECRET 做 HMAC-SHA256 后映射到同一字符集

为什么要带校验码：
  攻击者拿一个卡密去试，服务端先本地算校验码，不查库就知道格式真假。
  伪造的卡密在校验这一步就被挡掉，永远碰不到数据库，
  这样即使接口不限速，爆破的边际成本也不会变成 1 次 SQL。

注意：校验码只防伪造，不防穷举。真卡密被猜中的概率由 60 bit 随机体保证。
"""

import hmac
import hashlib
import secrets

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 0/O/1/I，避免手抄歧义
INDEX = {c: i for i, c in enumerate(ALPHABET)}

PREFIX = "ZF"
BODY_GROUPS = 3
GROUP_LEN = 4
CHECK_LEN = 4

BODY_LEN = BODY_GROUPS * GROUP_LEN  # 12
# ZF-XXXX-XXXX-XXXX-CCCC = 2 + 4*(4+1) = 22
FULL_LEN = len(PREFIX) + (BODY_GROUPS + 1) * (GROUP_LEN + 1)


# ---------------------------------------------------------------- 校验码

def _checksum(body, secret):
    """body 为 12 个字符（不含横线），返回 4 个字符。"""
    mac = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    out = []
    # 每字节取低 5 bit 映射到 32 字符集，取 4 个字节即 4 个字符。
    for i in range(CHECK_LEN):
        out.append(ALPHABET[mac[i] & 0x1F])
    return "".join(out)


# ---------------------------------------------------------------- 生成

def _random_body():
    return "".join(secrets.choice(ALPHABET) for _ in range(BODY_LEN))


def _group(body):
    return "-".join(body[i:i + GROUP_LEN] for i in range(0, len(body), GROUP_LEN))


def make(secret):
    """生成一张新卡密（带横线的可读形式）。"""
    body = _random_body()
    return "%s-%s-%s" % (PREFIX, _group(body), _checksum(body, secret))


def make_many(secret, count):
    """批量生成，去重（碰撞概率极低，但仍然防一手）。"""
    seen = set()
    out = []
    guard = 0
    while len(out) < count:
        key = make(secret)
        if key not in seen:
            seen.add(key)
            out.append(key)
        guard += 1
        if guard > count * 8 + 64:
            raise RuntimeError("随机数质量异常，生成卡密失败")
    return out


# ---------------------------------------------------------------- 解析

def normalize(raw):
    """把用户输入洗成标准形式 ZF-XXXX-XXXX-XXXX-CCCC。

    容错：小写、空格、少横线、多横线、前后空白、全角字符。
    非法字符直接丢弃；长度不对时尽量截断/补足到标准长度以便后续判错。
    """
    if not raw:
        return ""

    s = str(raw).upper()
    # 只保留字母数字
    buf = []
    for ch in s:
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            buf.append(ch)
    s = "".join(buf)

    # 去掉前缀（可能用户输入了 ZF 也可能没有）
    if s.startswith(PREFIX):
        s = s[len(PREFIX):]

    if not s:
        return ""

    # 这里不再补齐或截断：长度不对就是错卡密，交给 check_format 判。
    body = s[:BODY_LEN]
    check = s[BODY_LEN:BODY_LEN + CHECK_LEN]
    if not check:
        return "%s-%s" % (PREFIX, _group(body))
    return "%s-%s-%s" % (PREFIX, _group(body), check)


def split(key):
    """拆成 (body12, check4)。格式不合法返回 (None, None)。"""
    n = normalize(key)
    if not n:
        return None, None
    parts = n.split("-")
    if len(parts) != 1 + BODY_GROUPS + 1 or parts[0] != PREFIX:
        return None, None
    body = "".join(parts[1:1 + BODY_GROUPS])
    check = parts[-1]
    if len(body) != BODY_LEN or len(check) != CHECK_LEN:
        return None, None
    for ch in body + check:
        if ch not in INDEX:
            return None, None
    return body, check


def check_format(key, secret):
    """只校验格式与校验码，不查库。这是抗爆破的第一道闸。"""
    body, check = split(key)
    if body is None:
        return False
    # 用 compare_digest 避免按字符提前返回，虽然这里有 HMAC 保护，但保持习惯。
    return hmac.compare_digest(check, _checksum(body, secret))


def is_canonical(key):
    """判断用户输入是否已经是标准形式（用于给前端提示）。"""
    return bool(key) and normalize(key) == key.strip().upper()
