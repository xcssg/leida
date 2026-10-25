#!/usr/bin/env python3
"""ZUOFANG 统一服务入口。

一个进程同时干三件事：
  1. HTTP  —— 卡密 API、管理台、WebSocket 状态推送
  2. UDP   —— TGCP 隧道中继（握手 + 加解密 + 出网）
  3. 维护  —— 会话回收、卡状态复查、限流记录清理

为什么合成一个进程：
  封禁必须立刻生效。两个进程只能靠轮询或额外消息通道，慢且容易漂；
  同进程共享 SQLite 和内存里的会话表，一次开火就能把人踢干净。

用法：
  python3 main.py                       启动服务
  python3 main.py cards 20 30           发 20 张 30 天的卡
  python3 main.py cards 5 7 --batch t1  指定批次名
  python3 main.py init                  只建库不启动
  python3 main.py info                  打印统计
  python3 main.py secret                打印当前卡密密钥指纹

环境变量见 conf.py，常用的几个：
  ZF_PORT            HTTP 端口，默认 8090
  ZF_RELAY_LISTEN    UDP 监听，默认 0.0.0.0:9000
  ZF_PUBLIC_RELAY    下发给客户端的 relay 地址（跨机部署必填）
  ZF_ADMIN_KEY       管理台密钥，不设则每次启动随机
  ZF_NET_MODE        auto / on / off，出网模式
  ZF_DB              SQLite 路径
"""

import os
import signal
import sys
import time

# 允许从任意目录用 `python3 /path/to/server/main.py` 启动
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cards as C
import conf
import relayd
import store
import webapi as httpd


BANNER = r"""
  ______   __  __  ____  _____    _    _   _  ____
 |___  /  |  |/  |/ __ \|  ___|  / \  | \ | |/ ___|
    / /   |  |   | |  | | |_    / _ \ |  \| | |  _
   / /__  |  |   | |__| |  _|  / ___ \| |\  | |_| |
  /_____| |__|__|\____/|_|   /_/   \_\_| \_|\____|
"""


# ---------------------------------------------------------------- 初始化

def bootstrap(with_rooms=True):
    """建库 + 建表 + 补默认线路。可重复调用。"""
    store.init()
    if with_rooms:
        store.ensure_rooms()
    return store


# ---------------------------------------------------------------- 子命令

def cmd_cards(argv):
    if not argv:
        print("用法: main.py cards <数量> <天数> [--batch 名称] [--note 备注]")
        return 2
    try:
        count = int(argv[0])
        days = int(argv[1]) if len(argv) > 1 else 30
    except ValueError:
        print("数量和天数必须是整数")
        return 2

    batch = "cli"
    note = None
    i = 2
    while i < len(argv):
        a = argv[i]
        if a == "--batch" and i + 1 < len(argv):
            batch = argv[i + 1]
            i += 2
        elif a == "--note" and i + 1 < len(argv):
            note = argv[i + 1]
            i += 2
        else:
            i += 1

    bootstrap()
    try:
        keys = store.create_cards(count, days, batch=batch, note=note)
    except ValueError as e:
        print("生成失败: %s" % e)
        return 2

    print("已生成 %d 张，有效期 %d 天，批次 %s" % (len(keys), days, batch))
    print("-" * 34)
    for k in keys:
        print(k)
    print("-" * 34)
    print("数据库: %s" % conf.DB_PATH)
    return 0


def cmd_init(argv):
    bootstrap()
    print("已初始化 %s" % conf.DB_PATH)
    print("线路数: %d" % len(store.list_rooms()))
    return 0


def cmd_info(argv):
    bootstrap()
    st = store.stats()
    print("卡片总数    %d" % st["cards_total"])
    print("未激活      %d" % st["cards_unused"])
    print("生效中      %d" % st["cards_active"])
    print("已封禁      %d" % st["cards_banned"])
    print("已过期      %d" % st["cards_expired"])
    print("已续期消耗  %d" % st["cards_redeemed"])
    print("在线 token  %d" % st["tokens_live"])
    print("绑定设备    %d" % st["devices"])
    print("线路数      %d" % st["rooms"])
    print("数据库      %s" % conf.DB_PATH)
    return 0


def cmd_secret(argv):
    import hashlib
    fp = hashlib.sha256(conf.SECRET).hexdigest()[:16]
    print("卡密密钥指纹 %s" % fp)
    print("密钥来源     %s" % ("ZF_SECRET 环境变量"
                               if os.environ.get("ZF_SECRET") else conf.SECRET_FILE))
    print("换掉密钥会让历史卡密全部校验失败，谨慎操作")
    return 0


# ---------------------------------------------------------------- 服务

def serve():
    bootstrap()

    relay = relayd.Relay()
    relay.start()

    hub = httpd.ws.Hub(relay=relay).start()

    srv = httpd.make_server(relay=relay, hub=hub)
    httpd.start_sweeper()

    print(BANNER)
    print("  ZUOFANG %s" % conf.VERSION)
    print("  HTTP      http://%s:%d/" % (conf.HOST, conf.PORT))
    print("  管理台    http://%s:%d/admin?admin_key=%s"
          % (conf.HOST, conf.PORT, conf.ADMIN_KEY))
    print("  UDP 中继  %s（模式 %s）" % (conf.RELAY_LISTEN, relay.mode))
    print("  数据库    %s" % conf.DB_PATH)
    print("  线路      %d 条" % len(store.list_rooms()))
    if not conf.PUBLIC_RELAY:
        print("  提示      未设 ZF_PUBLIC_RELAY，客户端会用 %s"
              % conf.RELAY_LISTEN)
    print("")

    def shutdown(signum, frame):
        print("\n收到信号 %d，正在退出..." % signum)
        try:
            srv.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
        hub.close()
        relay.close()
        print("已停止")
    return 0


# ---------------------------------------------------------------- 入口

COMMANDS = {
    "cards": cmd_cards,
    "init": cmd_init,
    "info": cmd_info,
    "secret": cmd_secret,
}


def main(argv):
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]](argv[1:])
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
