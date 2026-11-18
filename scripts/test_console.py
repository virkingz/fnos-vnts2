#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台联动测试：用桩服务模拟 vnts2 控制台，验证
  1) 核心未运行时 /console-ready 返回 ready=false 并附日志尾部
  2) 核心就绪后 /console-ready 返回 ready=true
  3) /console/ 反代能取到页面，且 HTML 被正确改写（资源前缀 + 令牌注入）
  4) /api/* 反代透传到桩服务
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "shell"))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


# ---- 桩控制台（模拟 vnts2：JS 里硬编码 /assets 与 /api）----
CONSOLE_PORT = 29099
STUB_HTML = (
    '<!doctype html><html><head><title>vnt 控制中心</title>'
    '<script type="module" crossorigin src="/assets/index-abc.js"></script>'
    '<link rel="stylesheet" href="/assets/index-abc.css">'
    '</head><body><div id="app"></div></body></html>'
)
# 模拟压缩后的 JS：API 基址与动态 chunk 都是硬编码绝对路径
STUB_JS = (
    'const API_BASE="/api";'
    'function doFetch(p){return fetch(API_BASE+p,{headers:{"Authorization":"Bearer x"}});}'
    'const C=()=>import("/assets/chunk-xyz.js");'
    'const url="/assets/img/logo.png";'
    # Vite 的预加载分块表：路径**不带前导斜杠**，由 <link> 动态插入，
    # 不经过 fetch/XHR 拦截器 —— 必须在文本层改写，否则路由切换时视图空白。
    'const __vite__mapDeps=(i,m=__vite__mapDeps,'
    'd=(m.f||(m.f=["assets/SettingsView-DqxKWd6F.js","assets/NetworksView-Ce1TIOqS.js"])))'
    '=>i.map(i=>d[i]);'
)
STUB_CSS = 'body{background:url("/assets/bg.png")}'


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        # 故意用小写 header 名，与真实 axum/rust 服务一致。
        # 这能抓到「dict(headers) 后按 'Content-Type' 取值取不到」的 bug——
        # 该 bug 会让 HTML 被当作 application/octet-stream，浏览器变成下载。
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._send(200, json.dumps({"code": 200, "msg": "ok",
                                               "data": {"pong": True, "path": self.path}}),
                              "application/json; charset=utf-8")
        if self.path.endswith(".js"):
            return self._send(200, STUB_JS, "application/javascript; charset=utf-8")
        if self.path.endswith(".css"):
            return self._send(200, STUB_CSS, "text/css; charset=utf-8")
        return self._send(200, STUB_HTML)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path == "/api/login":
            return self._send(200, json.dumps({"code": 200, "msg": "ok",
                                               "data": {"token": "STUB-JWT-TOKEN"}}),
                              "application/json; charset=utf-8")
        return self._send(200, json.dumps({"code": 200, "msg": "ok", "data": None}),
                          "application/json; charset=utf-8")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _node_check_scripts(html):
    """用 node --check 校验 HTML 内联脚本语法。node 缺失时视为通过（跳过）。"""
    scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
    for i, s in enumerate(scripts):
        p = os.path.join(tempfile.gettempdir(), "inj_%d.js" % i)
        with open(p, "w", encoding="utf-8") as f:
            f.write(s)
        try:
            r = subprocess.run(["node", "--check", p], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return True  # node 不可用，跳过
        if r.returncode != 0:
            print("     注入脚本[%d] 语法错误: %s" % (i, r.stderr.decode()[:180]))
            return False
    return True


def get(url, headers=None):
    """返回 (状态码, body, 响应头小写字典)。"""
    req = urllib.request.Request(url, headers=headers or {})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        r = op.open(req, timeout=8)
        return r.getcode(), r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in (e.headers or {}).items()}
    except Exception as e:  # noqa: BLE001
        return None, str(e).encode(), {}


def main():
    base = tempfile.mkdtemp(prefix="vnts-console-")
    var = os.path.join(base, "var")
    os.makedirs(os.path.join(var, "server", "logs"), exist_ok=True)
    os.makedirs(os.path.join(base, "log"), exist_ok=True)

    # 让壳把 CONSOLE_PORT 指向桩服务
    with open(os.path.join(var, "ports.env"), "w") as f:
        f.write("PANEL_PORT=%d\nSERVICE_PORT=29872\nCONSOLE_PORT=%d\n"
                % (free_port(), CONSOLE_PORT))

    os.environ.update({
        "VNT_PKGVAR": var, "VNT_APPDEST": base, "VNT_APPNAME": "vnts-nas",
    })
    import shell_server as S
    import shell_http as H

    S.ensure_dirs()
    S.render_config()
    # 桩服务不运行时不提，先测「未就绪」分支
    panel_port = free_port()
    ports = S.load_ports()
    ports["PANEL_PORT"] = str(panel_port)
    S.save_ports(ports)

    srv = ThreadingHTTPServer(("127.0.0.1", panel_port), H.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)
    url = "http://127.0.0.1:%d" % panel_port
    token = S.get_panel_token()
    HDR = {"X-Panel-Token": token}

    print("[1] 核心未运行时：控制台应判为未就绪")
    c, b, hd = get(url + "/panel-api/console-ready", HDR)
    d = json.loads(b.decode())["data"]
    check("ready=false", d["ready"] is False, d)
    check("core_running=false", d["core_running"] is False)
    check("给出绑定的控制台地址", d["bind"].startswith("127.0.0.1:"), d["bind"])

    print("\n[2] 未就绪时 /console/ 给可读提示（而非空页）")
    c, b, hd = get(url + "/console/")
    check("返回 502 且含提示", c == 502 and "控制台未运行" in b.decode("utf-8", "replace"), c)

    print("\n[3] 启动桩控制台后：应判为就绪")
    stub = ThreadingHTTPServer(("127.0.0.1", CONSOLE_PORT), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    time.sleep(0.6)
    c, b, hd = get(url + "/panel-api/console-ready", HDR)
    d = json.loads(b.decode())["data"]
    check("ready=true", d["ready"] is True, d)
    check("detail 含字节数", "字节" in (d.get("detail") or ""), d.get("detail"))

    print("\n[4] 反代控制台首页：改写资源路径 + 注入令牌")
    c, b, hd = get(url + "/console/")
    html = b.decode("utf-8", "replace")
    check("首页 200", c == 200, c)
    # ⭐ 关键：Content-Type 必须是 text/html。
    # 上游发的是小写 content-type，若取值失败会退化成 application/octet-stream
    # → 浏览器不渲染而是弹「下载」。这正是「控制台空白 + 出现下载」的根因。
    check("Content-Type 是 text/html（否则浏览器会下载）",
          "text/html" in (hd.get("content-type") or ""), hd.get("content-type"))
    check("Content-Type 不是 octet-stream",
          "octet-stream" not in (hd.get("content-type") or ""), hd.get("content-type"))
    check("HTML 资源路径加 /console 前缀",
          'src="/console/assets/index-abc.js"' in html, html[:260])
    check("CSS 链接也改写", 'href="/console/assets/index-abc.css"' in html)
    check("注入 <base>", '<base href="/console/">' in html)
    check("不注入令牌（身份交给控制台登录页）",
          "jwt_token" not in html and "localStorage.setItem" not in html)

    print("\n[4a] 各类响应 Content-Type 正确（防止误下载）")
    c, js, hd = get(url + "/console/assets/index-abc.js")
    check("JS 是 javascript", "javascript" in (hd.get("content-type") or ""), hd.get("content-type"))
    c, css, hd = get(url + "/console/assets/index-abc.css")
    check("CSS 是 text/css", "css" in (hd.get("content-type") or ""), hd.get("content-type"))

    print("\n[4b] 反代 JS：改写资源路径，但**不动 /api 文本**（关键设计）")
    c, js, hd = get(url + "/console/assets/index-abc.js")
    js = js.decode("utf-8", "replace")
    check("JS 可获取", c == 200, c)
    check("动态 import 资源路径已改写",
          'import("/console/assets/chunk-xyz.js")' in js, js[:260])
    check("JS 内不留裸 /assets", '"/assets/' not in js, js[:260])
    # 关键：API 拼接片段必须保持原样，否则会拼出 /console/api/console/networks
    check("API 基址保持 /api 不被文本改写", 'API_BASE="/api"' in js, js[:120])
    check("拼接片段 /networks 未被误改", '"/networks"' not in js or True)
    check("未产生错误的双层前缀", "/console/api/console" not in js, js[:200])
    # ⭐ 关键：Vite mapDeps 条目必须**保持无前导斜杠**并只补「去斜杠前缀」。
    # Vite 运行时经 assetsURL(dd = e => '/'+e) 处理，若我们写成 "/console/assets/x.js"，
    # dd() 会产出 "//console/assets/x.js" → 浏览器按协议相对 URL 解析 → 丢域名 → 分块全 failed。
    check("mapDeps 已加前缀", "console/assets/SettingsView" in js, js[-260:])
    check("mapDeps 前缀不带前导斜杠（否则 dd() 造出 //）",
          '"console/assets/SettingsView' in js, js[-260:])
    check("mapDeps 未引入双斜杠", "//console" not in js, js[-260:])
    check("mapDeps 无残留裸 assets/", '"assets/' not in js, js[-260:])

    print("\n[4c] 反代 CSS")
    c, css, hd = get(url + "/console/assets/index-abc.css")
    css = css.decode("utf-8", "replace")
    check("CSS 内资源路径已改写", '/console/assets/bg.png' in css, css)

    print("\n[4d] HTML 注入运行时拦截器（API 的唯一处理点）")
    c, b, hd = get(url + "/console/")
    html = b.decode("utf-8", "replace")
    check("注入 fetch 包装", "window.fetch=function" in html)
    check("注入 XHR 包装", "XMLHttpRequest.prototype.open" in html)
    check("拦截器指向 /console/api", "/console" in html and "'/api/'" in html)
    check("拦截器处理 /assets", "'/assets/'" in html)
    # 回归防护：注入脚本必须语法正确，否则整段被浏览器丢弃、API 前缀失效
    check("注入脚本花括号正常（无 {{ 重复）", "{{" not in html and "}}" not in html)
    check("JS 语法可用 node 校验", _node_check_scripts(html))

    print("\n[5] 带网关前缀时资源路径也正确")
    c, b, hd = get(url + "/app/vnts-nas/console/")
    html = b.decode("utf-8", "replace")
    check("网关下 HTML 资源前缀正确",
          'src="/app/vnts-nas/console/assets/index-abc.js"' in html, html[:280])
    check("网关下 base 正确", '<base href="/app/vnts-nas/console/">' in html)
    c, js, hd = get(url + "/app/vnts-nas/console/assets/index-abc.js")
    js = js.decode("utf-8", "replace")
    check("网关下 JS 资源路径改写",
          'import("/app/vnts-nas/console/assets/chunk-xyz.js")' in js, js[:220])
    check("网关下 API 文本保持原样", 'API_BASE="/api"' in js, js[:120])

    print("\n[6] API 透传（/console/api/* 与 /api/*）")
    c, b, hd = get(url + "/console/api/networks", HDR)
    j = json.loads(b.decode())
    check("/console/api 透传成功", c == 200 and j.get("code") == 200, j)
    check("上游收到 /api/networks", j["data"]["path"] == "/api/networks", j["data"])
    check("API 响应 Content-Type 是 json",
          "json" in (hd.get("content-type") or ""), hd.get("content-type"))
    c, b, hd = get(url + "/api/networks", HDR)
    j = json.loads(b.decode())
    check("/api 兼容路径可用", c == 200 and j["data"]["path"] == "/api/networks", j)

    print("\n[7] 静态资源（/console/assets）")
    c, b, hd = get(url + "/console/assets/index-abc.js")
    check("资源可获取且是 JS", c == 200 and b"API_BASE" in b, (c, b[:40]))

    srv.shutdown()
    stub.shutdown()
    shutil.rmtree(base, ignore_errors=True)
    print("\n===== 结果：%d 通过 / %d 失败 =====" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
