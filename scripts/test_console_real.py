#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实前端结构验证：用从 vnts2 二进制提取的真实 index.html 与 mapDeps JS，
经反代后检查每个分块资源是否都能取到（模拟浏览器加载全链路）。

不依赖真实 vnts2 二进制运行（Linux 二进制无法在 Windows 执行），
但素材取自真实二进制，因此能复现「侧栏可见、内容区空白」这类问题。
"""
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "shell"))
PASS = FAIL = 0

# —— 真实 vnts2 前端素材（从 v2.0.9 二进制提取）——
REAL_INDEX = '''<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>vnt 控制中心</title>
    <script type="module" crossorigin src="/assets/index-WuWzUqOK.js"></script>
    <link rel="stylesheet" crossorigin href="/assets/index-x93Q7xIw.css">
  </head>
  <body>
    <div id="app"></div>
  </body>
</html>'''

# 真实 mapDeps（含各视图分块，路径不带前导斜杠）
REAL_MAPDEPS_JS = (
    'const API_BASE="/api";'
    'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=['
    '"assets/LoginView-BWeAJnqu.js","assets/eye-DqSxivB3.js",'
    '"assets/modules-BlkSxExa.js","assets/NetworksView-Ce1TIOqS.js",'
    '"assets/SettingsView-DqxKWd6F.js","assets/SettingsView-D19zM2Nj.css",'
    '"assets/ServersView-C6jfXhnb.js","assets/DevicesView-DIjKRxmy.js"'
    '])))=>i.map(i=>d[i]);'
    'function load(v){return import(v);}'
)
REAL_CSS = '.app{display:flex}.sidebar{width:200px}'

CHUNKS = [
    "assets/LoginView-BWeAJnqu.js", "assets/eye-DqSxivB3.js",
    "assets/modules-BlkSxExa.js", "assets/NetworksView-Ce1TIOqS.js",
    "assets/SettingsView-DqxKWd6F.js", "assets/ServersView-C6jfXhnb.js",
    "assets/DevicesView-DIjKRxmy.js", "assets/SettingsView-D19zM2Nj.css",
]


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


CONSOLE_PORT = 0


class Stub(BaseHTTPRequestHandler):
    """模拟真实 vnts2：content-type 用小写，与 axum 一致。"""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p.startswith("/api/"):
            return self._send(200, json.dumps({"code": 200, "msg": "ok", "data": {"path": p}}),
                              "application/json; charset=utf-8")
        if p.endswith(".js"):
            return self._send(200, REAL_MAPDEPS_JS, "application/javascript; charset=utf-8")
        if p.endswith(".css"):
            return self._send(200, REAL_CSS, "text/css; charset=utf-8")
        if p == "/favicon.svg":
            return self._send(200, "<svg/>", "image/svg+xml")
        return self._send(200, REAL_INDEX)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        return self._send(200, json.dumps({"code": 200, "msg": "ok", "data": None}),
                          "application/json; charset=utf-8")


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        r = op.open(req, timeout=8)
        return r.getcode(), r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in (e.headers or {}).items()}
    except Exception as e:  # noqa: BLE001
        return None, str(e).encode(), {}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    base = tempfile.mkdtemp(prefix="vnts-real-")
    var = os.path.join(base, "var")
    os.makedirs(os.path.join(var, "server", "logs"), exist_ok=True)
    os.makedirs(os.path.join(base, "log"), exist_ok=True)

    cport = free_port()
    pport = free_port()
    with open(os.path.join(var, "ports.env"), "w") as f:
        f.write("PANEL_PORT=%d\nSERVICE_PORT=29872\nCONSOLE_PORT=%d\n" % (pport, cport))

    os.environ.update({"VNT_PKGVAR": var, "VNT_APPDEST": base, "VNT_APPNAME": "vnts-nas"})
    import shell_server as S
    import shell_http as H

    S.ensure_dirs(); S.render_config()
    ports = S.load_ports(); ports["PANEL_PORT"] = str(pport); S.save_ports(ports)

    srv = ThreadingHTTPServer(("127.0.0.1", pport), H.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stub = ThreadingHTTPServer(("127.0.0.1", cport), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    time.sleep(0.8)

    url = "http://127.0.0.1:%d" % pport
    HDR = {"X-Panel-Token": S.get_panel_token()}
    pre = ""  # 直连

    print("[1] 控制台首页可渲染（Content-Type 正确）")
    c, b, hd = get(url + "/console/")
    html = b.decode("utf-8", "replace")
    check("200", c == 200, c)
    check("text/html", "text/html" in (hd.get("content-type") or ""), hd.get("content-type"))

    print("\n[2] 主入口资源可加载（否则整页白）")
    m = re.search(r'src="([^"]+index-[^"]+\.js)"', html)
    check("找得到主 JS 引用", bool(m), html[:200])
    if m:
        jsurl = m.group(1)
        c, jsb, hd = get(url + jsurl)
        check("主 JS 200（路径 %s）" % jsurl, c == 200, c)
        check("主 JS 是 javascript", "javascript" in (hd.get("content-type") or ""))

    m = re.search(r'href="([^"]+index-[^"]+\.css)"', html)
    if m:
        c, _cb, hd = get(url + m.group(1))
        check("主 CSS 200（%s）" % m.group(1), c == 200, c)

    print("\n[3] mapDeps 改写形态必须让 Vite 的 dd() 解析出正确 URL")
    c, jsb, _ = get(url + "/console/assets/index-WuWzUqOK.js")
    js = jsb.decode("utf-8", "replace")

    # 先取出 mapDeps 数组里的**全部**条目（容错两种前缀形态，避免"取不到就空转"的假阳性）
    m = re.search(r'__vite__mapDeps[^;]*?m\.f=\[(.*?)\]', js, re.S)
    entries = []
    if m:
        entries = [x for x in re.findall(r'"([^"]+)"', m.group(1))]
    check("取到 mapDeps 条目（非空）", len(entries) >= len(CHUNKS),
          "%d 条: %s" % (len(entries), entries[:3]))
    check("mapDeps 前缀不带前导斜杠（否则 dd() 造出 //）",
          all(not e.startswith("/") for e in entries), entries[:2])
    check("mapDeps 无残留裸 assets/",
          all(not e.startswith("assets/") for e in entries), entries[:2])

    # 用 Vite 的真实算法判定：dd(e) = '/' + e，再按 module URL 解析。
    # 旧写法（带前导斜杠）会得到 '//console/...'，解析后 origin 变成 http://console。
    module_url = "https://nas.local/console/assets/index-WuWzUqOK.js"
    origin_ok = "https://nas.local"
    bad = []
    for e in entries:
        after_dd = "/" + e                      # Vite: dd = function(e){return '/'+e}
        final = urljoin(module_url, after_dd)
        if not final.startswith(origin_ok + "/console/assets/"):
            bad.append((e, final))
    check("经 dd() 解析后 origin 未丢失（无协议相对 URL）", not bad, bad[:2])

    print("\n[4] 全部分块实际可加载（否则路由切换后内容区空白）")
    for e in entries:
        c, _b, hd = get(url + "/" + e.lstrip("/"))
        check("分块可加载 %s" % os.path.basename(e), c == 200, c)

    print("\n[5] 入口资源与 API")
    m = re.search(r'src="(/console/assets/index-[^"]+\.js)"', html)
    check("HTML 里主 JS 带前缀（绝对路径形态）", bool(m), html[:200])
    check("JS 里 API_BASE 未被误改", 'API_BASE="/api"' in js)
    c, ab, hd = get(url + "/console/api/networks", HDR)
    check("API 200", c == 200, c)
    check("API 是 json", "json" in (hd.get("content-type") or ""))
    check("上游收到正确路径", json.loads(ab.decode())["data"]["path"] == "/api/networks")

    print("\n[6] favicon 与静态小资源")
    c, _b, _hd = get(url + "/console/favicon.svg")
    check("favicon 200", c == 200, c)

    srv.shutdown(); stub.shutdown()
    shutil.rmtree(base, ignore_errors=True)
    print("\n===== 结果：%d 通过 / %d 失败 =====" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
