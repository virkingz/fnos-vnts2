#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vnts-nas 管理页服务入口。

同时提供两个监听：
  1. Unix socket  $TRIM_APPDEST/app.sock  —— 供飞牛统一网关 /app/vnts-nas 转发
  2. TCP 0.0.0.0:<PANEL_PORT>             —— 管理页（用户配置的端口）

vnts2 控制台由本服务反向代理（挂在 /console/ 与 /api/ 下），
因此与管理页同源，可安全 iframe 内嵌，令牌由代理注入，页面内无需登录。
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import shell_http as H  # noqa: E402
import shell_server as S  # noqa: E402


def main():
    S.log("=== vnts-nas shell backend starting ===")
    S.ensure_dirs()
    S.heal_legacy_conf()
    S._migrate_legacy_peer_domains()
    S.trim_logs()
    threading.Thread(target=S.watcher, daemon=True).start()
    threading.Thread(target=S.dns_sync_loop, args=(S._dns_stop,), daemon=True).start()
    threading.Thread(target=H.serve_unix, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(2), S.bootstrap()), daemon=True).start()
    H.serve_tcp()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        S.log("interrupted, shutting down")
        S.stop_core()
