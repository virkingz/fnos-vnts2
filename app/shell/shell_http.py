#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vnts-nas 管理页 HTTP 层：路由、静态资源、vnts 控制台反代、导入导出下载。

路径分工（避免冲突，关键）：
  /panel-api/*   本管理页自身的 API
  /api/*         全部转发给 vnts2 控制台（其前端 API_BASE 就是 /api）
  /console/*     vnts2 控制台页面（改写资源路径 + 注入令牌，实现同源内嵌免登录）
  其余           管理页静态资源

内嵌控制台为何无需改 JS：
  vnts2 前端用 hash 路由（createWebHashHistory），资源为绝对路径 /assets/…
  我们只改写 HTML 里的 /assets/ -> /console/assets/，并注入 localStorage 令牌；
  其 fetch('/api/…') 与页面同源，正好命中本服务的 /api 反代。
"""

import errno
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shell_server as S  # noqa: E402

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
CONSOLE_PREFIX = "/console"
MAX_BODY = 300 * 1024 * 1024

# 静态资源根的固定前缀。这些是**完整**资源路径，从不被拼接，
# 因此可安全地在文本层改写（加代理前缀）。
#
# ⚠ 绝不能改写 /api 或其它任意 /xxx：
#   vnts2 前端有 `fetch('/api' + path)` 这类拼接，path 字面量形如 "/networks"。
#   若把字面量也改写，会得到 '/console/api/console/networks' 这类错误 URL。
#   API 改由「运行时 fetch/XHR 拦截」处理 —— 那里能拿到完整 URL，不误伤片段。
ASSET_ROOTS = ("/assets/", "/favicon", "/vite.svg", "/icons/", "/static/", "/img/")

# Vite 的 __vite__mapDeps 里，分块路径是**不带前导斜杠**的：
#   "assets/SettingsView-DqxKWd6F.js"
#
# 🔴 关键：这类条目**必须保持「无前导斜杠」**，只能补上「不含前导斜杠」的前缀。
#   因为 Vite 运行时会经 assetsURL 处理，其实现（v2.0.9 二进制实证）为：
#       dd = function(e){ return `/` + e }      // 无条件加一个前导斜杠
#       t = dd(t); t = new URL(t, import.meta.url).href
#   若我们写成 "/console/assets/x.js"，dd() 后变成 "//console/assets/x.js"，
#   浏览器按**协议相对 URL**解析，主机名成了 "console" → 丢域名 → DNS 失败。
#
# 而 HTML 里的 src="/assets/x.js" 是**绝对路径**，不经 dd()，
# 因此那类仍需补带前导斜杠的前缀。
#
# 同一份响应里两种形态并存（HTML 一个、mapDeps 一个），故需分别处理。
_MAPDEPS_RE = re.compile(r'(["\'])(assets/[A-Za-z0-9._-]+\.(?:js|css))(["\'])')


def rewrite_assets(text, prefix):
    """给文本里的资源路径加代理前缀（HTML/JS/CSS 通用，幂等）。

    分两类处理，**改写形态不同**：
      1. HTML 里的绝对路径  "/assets/x.js" → "<prefix>/assets/x.js"
      2. Vite mapDeps 条目  "assets/x.js"  → "<prefix 去斜杠>/assets/x.js"
         （保持无前导斜杠，交给 Vite 的 dd() 补斜杠，见上方注释）

    幂等：改写后 mapDeps 条目形如 "<pre>/assets/x.js"（引号后非 "assets/"），
    HTML 条目形如 "<prefix>/assets/x.js"（引号后是 prefix 而非 "/assets/"），
    两种模式都不再匹配原始形态。
    """
    if not text or not prefix:
        return text
    pre = prefix.rstrip("/")
    pre_noslash = pre.lstrip("/")

    # 1) Vite mapDeps：保持无前导斜杠
    text = _MAPDEPS_RE.sub(
        lambda m: m.group(1) + pre_noslash + "/" + m.group(2) + m.group(3), text)

    # 2) HTML/其它绝对路径（此时 mapDeps 已改完，不会二次命中）
    for root in ASSET_ROOTS:
        for q in ('"', "'", "`"):
            text = text.replace(q + root, q + pre + root)
    return text


def is_text_type(ctype):
    """只有 HTML / JS / CSS 才做资源路径改写。

    刻意排除 JSON：API 响应属于数据，改写其中内容可能破坏业务数据。
    （vnts2 的 /api 走独立代理分支，不经此处；此处再收一道防线。）
    """
    c = (ctype or "").lower()
    return "text/html" in c or "javascript" in c or "text/css" in c


def ci_headers(msg):
    """大小写不敏感的 header 视图。

    上游（axum/rust 服务）常发小写 header 名（如 `content-type`），
    而 dict(msg) 会原样保留大小写，导致 hdrs.get("Content-Type") 取不到值，
    进而把 HTML 误判为 application/octet-stream —— 浏览器就会「下载」而不是渲染。
    这里统一转成小写键，读取时也转小写，彻底避免该问题。
    """
    class CI(dict):
        def get(self, k, default=None):
            return super().get(k.lower(), default)
    d = CI()
    for k, v in (msg.items() if hasattr(msg, "items") else msg or []):
        d[k.lower()] = v
    return d


def mime_of(path):
    return MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "vnts-nas/1.0"
    protocol_version = "HTTP/1.1"

    # -- 读请求阶段的连接异常 ----------------------------------------------
    def handle_one_request(self):
        """读请求行/请求头时的连接异常一律静默。

        BaseHTTPRequestHandler.handle_one_request 里 `rfile.readline()` 抛出的
        ConnectionResetError / BrokenPipeError / TimeoutError 会冒到
        socketserver 的 handle_error，把整段 traceback 打到 stderr
        （进程 stderr 又进了日志），日志被这类噪声刷满：

            ConnectionResetError: [Errno 104] Connection reset by peer
            File ".../http/server.py", line 408, in handle_one_request
                self.raw_requestline = self.rfile.readline(65537)

        这些都是**客户端侧的正常行为**，不是服务端故障：
          - 浏览器切换路由/刷新时取消在途请求；
          - 反代（飞牛网关、nginx）探测端口连通性后立刻断开；
          - 长连接空闲超时后被对端关闭。

        这里直接吞掉并标记关闭连接。真正需要排查的异常由 _route 的
        except 分支记录（那才带请求路径等有用信息）。
        """
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
                TimeoutError, OSError):
            # OSError 覆盖各平台不同的 errno（EPIPE/ECONNRESET/ETIMEDOUT…）
            self.close_connection = True

    # -- 基础输出 ----------------------------------------------------------
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        """发送响应。

        ⚠ HEAD 请求的 Content-Length 必须为 0：
        HEAD 会返回与 GET 相同的响应头、但**不带 body**。
        若按 GET 的长度声明 Content-Length（如 11790）却一个字节都不写，
        客户端会一直等这 11790 字节 → 连接失步/超时 → 浏览器报 net::ERR_*，
        随后重试才成功（Network 面板即出现「(failed) + 200」成对记录）。
        """
        if isinstance(body, str):
            body = body.encode("utf-8")
        is_head = (self.command == "HEAD")
        declared = 0 if is_head else len(body)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(declared))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not is_head and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # 客户端提前断开（如切换路由取消请求），属正常，静默处理
                self.close_connection = True

    def ok(self, data=None, msg="ok"):
        self._send(200, json_bytes({"code": 0, "msg": msg, "data": data}))

    def err(self, msg, code=400, data=None):
        self._send(code, json_bytes({"code": code, "msg": msg, "data": data}))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        return self.rfile.read(n)

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def log_message(self, fmt, *args):
        pass

    # -- 路由 --------------------------------------------------------------
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def do_HEAD(self):
        self._route("HEAD")

    def _route(self, method):
        try:
            u = urlparse(self.path)
            raw = unquote(u.path)
            # 剥离飞牛统一网关前缀（直连时前缀为空，no-op）
            p = S.remove_gateway_prefix(raw)
            q = parse_qs(u.query)
            self._prefix = S.detect_path_prefix(raw)

            if p.startswith("/panel-api/"):
                return self._panel_api(method, p[len("/panel-api"):], q)
            # 控制台 API：同时挂 /console/api/*（挂载前缀下唯一可达的路径）
            # 与 /api/*（直连根路径时的兼容），两者走同一代理。
            if p.startswith(CONSOLE_PREFIX + "/api/"):
                return self._proxy_console_api(method, p[len(CONSOLE_PREFIX):], q)
            if p == CONSOLE_PREFIX + "/api":
                return self._proxy_console_api(method, "/api", q)
            if p == "/api" or p.startswith("/api/"):
                return self._proxy_console_api(method, p, q)
            if p == CONSOLE_PREFIX or p.startswith(CONSOLE_PREFIX + "/"):
                # 无尾斜杠的 /console 需重定向到 /console/，
                # 否则浏览器的相对路径基准是站点根，资源会解析到错误位置。
                if p == CONSOLE_PREFIX:
                    return self._send(301, b"", "text/plain; charset=utf-8", extra={
                        "Location": (getattr(self, "_prefix", "") or "") + CONSOLE_PREFIX + "/",
                    })
                return self._console(method, p)
            return self._static(p)
        except ValueError as e:
            self.err(str(e))
        except Exception as e:  # noqa: BLE001
            S.log("HTTP 异常 %s: %s\n%s" % (self.path, e, traceback.format_exc()))
            self.err("内部错误: %s" % e, 500)

    def _token_ok(self):
        """校验管理页令牌（请求头 X-Panel-Token 或查询参数 token）。"""
        want = S.get_panel_token()
        if not want:
            return True
        got = (self.headers.get("X-Panel-Token") or "").strip()
        if not got:
            try:
                q = parse_qs(urlparse(self.path).query)
                got = (q.get("token") or [""])[0].strip()
            except Exception:  # noqa: BLE001
                got = ""
        return got == want

    # -- 管理页 API --------------------------------------------------------
    def _panel_api(self, method, path, q):
        P = S

        if path == "/health":
            return self.ok({"ok": True, "t": time.time()})

        # 令牌校验：health 之外的管理页接口都需带正确令牌。
        # 令牌由管理页首页自动注入，用户无需手工填写。
        if not self._token_ok():
            return self.err("令牌无效，请重新从管理页入口访问", 401)

        if path == "/state":
            return self.ok(P.status())

        if path == "/settings":
            if method == "GET":
                return self.ok(P.get_settings())
            if method == "PUT":
                res = P.update_settings(self._json_body())
                if not res.get("ok"):
                    return self.err("；".join(res.get("errors", [])) or "保存失败")
                # 不自动重启核心：只把 config.toml 写盘，何时生效由用户决定
                return self.ok({"saved": True})
            return self.err("方法不允许", 405)

        if path == "/ports":
            if method == "GET":
                return self.ok(P.load_ports())
            if method == "PUT":
                body = self._json_body()
                ports = P.load_ports()
                for k in ("PANEL_PORT", "SERVICE_PORT", "CONSOLE_PORT"):
                    if k in body and str(body[k]).strip():
                        try:
                            v = int(body[k])
                        except (TypeError, ValueError):
                            return self.err("%s 必须是数字" % k)
                        if not (1 <= v <= 65535):
                            return self.err("%s 范围 1-65535" % k)
                        ports[k] = str(v)
                if ports["PANEL_PORT"] == ports["SERVICE_PORT"]:
                    return self.err("管理页端口与组网服务端口不能相同")
                P.save_ports(ports)
                P.render_config()
                need = ports["PANEL_PORT"] != str(self.server.server_address[1])
                if P.core_running():
                    P.restart_core()
                return self.ok({"restart_needed": need})
            return self.err("方法不允许", 405)

        if path == "/token":
            if method == "GET":
                t = P.get_panel_token()
                return self.ok({"hint": ("已设置(%d位)" % len(t)) if t else "未设置"})
            if method == "PUT":
                try:
                    P.set_panel_token(self._json_body().get("token", ""))
                except ValueError as e:
                    return self.err(str(e))
                return self.ok({"msg": "令牌已更新"})
            return self.err("方法不允许", 405)

        if path == "/token/gen":
            import secrets
            return self.ok({"token": secrets.token_hex(16)})

        if path == "/logdir":
            if method == "GET":
                return self.ok({
                    "log_dir": P.log_dir(),
                    "default": os.path.join(P.PKGVAR, "log"),
                    "from_env": bool(os.environ.get("LOG_DIR")),
                })
            if method == "PUT":
                d = (self._json_body().get("log_dir") or "").strip()
                if os.environ.get("LOG_DIR"):
                    return self.err("日志目录由环境变量 LOG_DIR 指定，无法在此修改")
                if d:
                    try:
                        os.makedirs(d, exist_ok=True)
                    except OSError as e:
                        return self.err("无法创建目录: %s" % e)
                P.save_log_dir(d)
                return self.ok({"log_dir": P.log_dir()})
            return self.err("方法不允许", 405)

        if path == "/logconf":
            if method == "GET":
                return self.ok(P.log_conf_view())
            if method == "PUT":
                b = self._json_body()
                # 兼容两种入参：max_kb（精确）优先，其次 max_mb（换算）
                kb = b.get("max_kb")
                if kb is None and b.get("max_mb") is not None:
                    try:
                        kb = float(b.get("max_mb")) * 1024
                    except (TypeError, ValueError):
                        kb = b.get("max_mb")     # 交给校验报错
                conf, errs = P.normalize_log_conf(b.get("level"), kb, b.get("keep"))
                if errs:
                    return self.err("；".join(errs))
                P.save_log_conf(conf)
                return self.ok({"saved": True, "conf": P.log_conf_view()})
            return self.err("方法不允许", 405)

        if path == "/mirror":
            if method == "GET":
                return self.ok({"mirror": P.get_mirror()})
            if method == "PUT":
                res = P.set_mirror(self._json_body().get("mirror", ""))
                if not res.get("ok"):
                    return self.err(res.get("error", "保存失败"))
                out = {"mirror": res.get("mirror", "")}
                if res.get("warning"):
                    out["warning"] = res["warning"]
                return self.ok(out)
            return self.err("方法不允许", 405)

        if path == "/proxy":
            if method == "GET":
                return self.ok({"proxy": P.get_proxy()})
            if method == "PUT":
                res = P.set_proxy(self._json_body().get("proxy", ""))
                if not res.get("ok"):
                    return self.err(res.get("error", "保存失败"))
                return self.ok({"proxy": res.get("proxy", "")})
            return self.err("方法不允许", 405)

        if path == "/test-connection":
            return self._test_connection()

        if path == "/dns-peers":
            if method == "GET":
                return self.ok(P.dns_peers_status())
            if method == "PUT":
                body = self._json_body()
                res = P.set_dns_peers(body.get("peers", ""), body.get("interval"))
                if not res.get("ok"):
                    return self.err("；".join(res.get("errors", [])) or "保存失败")
                return self.ok({"saved": True, "status": res.get("status"),
                                "sync": res.get("sync", ""),
                                "actions": res.get("actions", [])})
            return self.err("方法不允许", 405)

        if path == "/dns-peers/sync":
            if method != "POST":
                return self.err("方法不允许", 405)
            res = P.sync_dns_peers(log_prefix="域名同步（手动）")
            if res.get("skipped"):
                return self.ok({"ok": True, "msg": "清单为空，无需同步",
                                "status": P.dns_peers_status()})
            return self.ok({"ok": res.get("ok"), "msg": res.get("error") or "已同步",
                            "actions": res.get("actions", []),
                            "status": P.dns_peers_status()})

        if path == "/core/check":
            if not P.arch_triple():
                return self.err("当前架构 %s 无官方预编译包" % S.machine())
            try:
                latest = P.latest_version()
            except Exception as e:  # noqa: BLE001
                return self.err("查询失败: %s" % e)
            if not latest:
                return self.err("无法获取最新版本（网络受限？可先配置镜像或代理）")
            cur = P.installed_version()
            state = "latest" if cur == latest else ("update" if cur else "missing")
            return self.ok({"current": cur, "latest": latest, "state": state})

        if path == "/core/download":
            ver = (self._json_body().get("version") or "").strip()
            if not ver:
                ver = P.latest_version()
            if not ver:
                return self.err("未指定版本且无法查询最新版本")
            res = P.download_core(ver)
            if not res.get("ok"):
                return self.err(res.get("error", "下载失败"))
            if P.core_running():
                P.restart_core()
            return self.ok(res)

        if path == "/core/upload":
            raw = self._body()
            ver = (self.headers.get("X-Core-Version") or "").strip()
            if not raw:
                return self.err("未收到文件内容")
            res = P.install_core_from_bytes(raw, ver)
            if not res.get("ok"):
                return self.err(res.get("error", "安装失败"))
            if P.core_running():
                P.restart_core()
            return self.ok(res)

        if path in ("/core/start", "/core/stop", "/core/restart"):
            if path == "/core/stop":
                P.mark_autostart(False)
                ok = P.stop_core()
                return self.ok({"stopped": ok}) if ok else self.err("停止失败")
            if path == "/core/restart":
                ok, msg = P.restart_core()
            else:
                ok, msg = P.start_core()
            if not ok:
                return self.err(msg)
            P.mark_autostart(True)
            return self.ok({"msg": msg})

        if path == "/logs":
            name = (q.get("name") or ["vnts2.log"])[0]
            return self.ok({"name": name, "text": P.read_log(name), "sizes": P.log_sizes()})

        if path == "/logs/clear":
            P.clear_log()
            return self.ok({"msg": "已清空"})

        if path == "/config/export":
            data = P.export_bundle()
            fn = "fnos-vnts2-config-%s.zip" % time.strftime("%Y%m%d-%H%M%S")
            return self._send(200, data, "application/zip", extra={
                "Content-Disposition": 'attachment; filename="%s"' % fn,
            })

        if path == "/config/import":
            raw = self._body()
            res = P.import_bundle(raw)
            if not res.get("ok"):
                return self.err(res.get("error", "导入失败"))
            # 核心在跑 → 重启它，让导入的配置（含出站互联）真正生效。
            # restart_core 内部会在启动后再补一次域名同步 —— 同步要经控制台
            # API 写进核心数据库，必须在核心就绪之后做。
            # 核心没跑 → 不主动拉起（保持用户原有的运行状态），但要如实告诉
            # 用户「互联要等核心启动后才会同步」，免得他以为导入没生效。
            if P.core_running():
                P.restart_core()
                res["core"] = "restarted"
            else:
                res["core"] = "stopped"
                res["notice"] = ("核心当前未运行。导入的出站互联域名会在核心"
                                 "启动后自动同步（约 30 秒内），无需手工操作。")
            return self.ok(res)

        if path == "/console-info":
            ports = P.load_ports()
            return self.ok({
                "url": CONSOLE_PREFIX + "/",
                "bind": "127.0.0.1:%s" % ports["CONSOLE_PORT"],
                "username": P.get_console_username(),
            })

        if path == "/console-ready":
            # 控制台就绪探测：供前端在核心启动后自动加载 iframe
            ports = P.load_ports()
            ready, detail = False, ""
            try:
                code, body, _ = self._console_fetch("/", timeout=4)
                if code == 200:
                    ready = True
                    detail = "%d 字节" % len(body)
                elif code is None:
                    detail = "控制台端口无响应"
                else:
                    detail = "HTTP %s" % code
            except Exception as e:  # noqa: BLE001
                detail = str(e)
            running = P.core_running()
            return self.ok({
                "ready": ready,
                "core_running": bool(running),
                "core_present": os.path.isfile(P.CORE_BIN),
                "bind": "127.0.0.1:%s" % ports["CONSOLE_PORT"],
                "detail": detail,
                "username": P.get_console_username(),
                "log_tail": P._tail(P.core_log(), 25) if not ready else "",
            })

        if path == "/selfcheck":
            return self._selfcheck()

        return self.err("未知接口: %s" % path, 404)

    # -- 连通性 / 自检 -----------------------------------------------------
    def _test_connection(self):
        """分项探测下载链路：直连 / 镜像 / 代理 / 实际路径。"""
        try:
            res = S.probe_network()
        except Exception as e:  # noqa: BLE001
            return self.err("测试失败: %s" % e)
        if not res.get("ok"):
            hint = res.get("hint") or "下载链路不通"
            return self.err(hint, data={"checks": res.get("checks", []),
                                       "mirror": res.get("mirror", ""),
                                       "proxy": res.get("proxy", "")})
        return self.ok(res)

    def _selfcheck(self):
        checks = []

        def add(name, ok, detail=""):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        ports = S.load_ports()
        ver = S.installed_version()
        add("核心已下载", os.path.isfile(S.CORE_BIN), ("v%s" % ver) if ver else "未下载")
        add("服务端配置存在", os.path.isfile(S.CONFIG_TOML), S.CONFIG_TOML)
        add("核心正在运行", bool(S.core_running()), "pid=%s" % S.core_running())
        add("日志目录独立", S.log_dir() != S.SERVER_DIR, S.log_dir())
        try:
            sk = socket.create_connection(("127.0.0.1", int(ports["CONSOLE_PORT"])), timeout=2)
            sk.close()
            add("控制台端口可连接", True, "127.0.0.1:%s" % ports["CONSOLE_PORT"])
        except OSError as e:
            add("控制台端口可连接", False, str(e))
        try:
            code, body, _ = self._console_fetch("/")
            add("控制台反代链路", code == 200, ("%d 字节" % len(body)) if code else "无响应")
        except Exception as e:  # noqa: BLE001
            add("控制台反代链路", False, str(e))
        return self.ok({"checks": checks})

    # -- vnts 控制台反代 ---------------------------------------------------
    def _console_port(self):
        return int(S.load_ports()["CONSOLE_PORT"])

    def _console_fetch(self, path, method="GET", headers=None, body=None, timeout=10):
        # 本地回环必须绕过系统/环境代理，否则 http_proxy 会把 127.0.0.1 也代理走
        url = "http://127.0.0.1:%d%s" % (self._console_port(), path)
        req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            r = opener.open(req, timeout=timeout)
            return r.getcode(), r.read(), ci_headers(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), ci_headers(e.headers)
        except (urllib.error.URLError, OSError):
            return None, b"", {}

    def _proxy_console_api(self, method, path, q):
        headers = {}
        ct = self.headers.get("Content-Type")
        if ct:
            headers["Content-Type"] = ct
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth
        body = None
        if method in ("POST", "PUT", "DELETE", "PATCH"):
            try:
                body = self._body()
            except ValueError as e:
                return self.err(str(e))
        full = path
        if "?" in self.path:
            full = path + "?" + self.path.split("?", 1)[1]
        code, data, hdrs = self._console_fetch(full, method, headers, body)
        if code is None:
            return self.err("控制台未运行", 502)
        return self._send(code, data, hdrs.get("Content-Type", "application/json; charset=utf-8"))

    def _console(self, method, path):
        """反代 vnts2 控制台：剥离 /console 前缀后取上游，再改写文本响应。

        为什么必须改写 JS/CSS（而非只改 HTML）：
          vnts2 前端把 API 基址 '/api' 与资源 '/assets/…' 硬编码在压缩后的 JS 里，
          <base> 标签对绝对路径无效。只改 HTML 会导致 JS 里的 /api 请求打到站点根。
          因此对所有文本响应统一做绝对路径改写，使它们带上 /console 前缀，
          由本服务统一接管（见路由中的 /console/api/* 与 /console/assets/*）。
        """
        rel = path[len(CONSOLE_PREFIX):] or "/"
        cpre = (getattr(self, "_prefix", "") or "") + CONSOLE_PREFIX  # /console 或 /app/x/console

        headers = {}
        ct = self.headers.get("Content-Type")
        if ct:
            headers["Content-Type"] = ct
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth

        body = None
        if method in ("POST", "PUT", "DELETE", "PATCH"):
            try:
                body = self._body()
            except ValueError as e:
                return self.err(str(e))

        full = rel
        if "?" in self.path:
            full = rel + "?" + self.path.split("?", 1)[1]

        code, data, hdrs = self._console_fetch(full, method, headers, body)
        if code is None:
            if method in ("GET", "HEAD"):
                return self._send(
                    502, "控制台未运行，请先在「核心更新」中下载并启动核心",
                    "text/plain; charset=utf-8")
            return self.err("控制台未运行", 502)

        ctype = hdrs.get("Content-Type", "application/octet-stream")

        # 文本类响应：只改写**完整资源路径**（/assets 等）。
        # API 不改文本，交给运行时拦截器 —— 文本层改 /api 会误伤拼接片段。
        if is_text_type(ctype) and method in ("GET", "HEAD") and data:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None:
                text = rewrite_assets(text, cpre)
                if "text/html" in ctype:
                    text = self._console_inject(text, cpre)
                    ctype = "text/html; charset=utf-8"
                data = text.encode("utf-8")

        return self._send(code, data, ctype)

    def _console_inject(self, html, cpre):
        """给控制台 HTML 注入 <base> 与运行时前缀拦截器。

        - <base>：保证相对路径解析正确（对绝对路径无效，故仍需资源路径改写）
        - fetch/XHR 拦截：把运行时发出的 /api、/assets 请求改写到控制台前缀

        **不做令牌注入**：控制台有自带的登录页，登录态由用户自行处理。
        这样反代层只负责「路径」，不掺和「身份」，边界更清晰、问题面更小。
        """
        pre = getattr(self, "_prefix", "") or ""
        pieces = ['<base href="%s/">' % cpre]

        # 运行时前缀拦截 —— API 的唯一处理点。
        # 拦截器拿到的是**完整 URL**（如 "/api/networks"），因此可安全判断前缀，
        # 不会误伤 `fetch('/api' + p)` 里 p="/networks" 这类拼接片段。
        #
        # 注意：直连时 pre 为空，`pre + "/"` 退化成 "/"，若不加保护会令
        # `indexOf("/")===0` 恒真而提前 return，导致 API 前缀永远补不上。
        # 因此仅在 pre 非空时才加入该判断分支。
        #
        # 用 % 占位替换而非 str.format：JS 里大量 {} 会被 format 当占位符，
        # 早前误用 {{}} 转义导致生成的脚本花括号重复、语法错误、整段被浏览器丢弃。
        pre_guard = ""
        if pre:
            pre_guard = "||u.indexOf(%s+'/')===0" % json.dumps(pre)
        interceptor = (
            "<script>(function(){var A=%s,X=%s;"
            "function fix(u){try{u=String(u);"
            "if(u.charAt(0)!=='/'||u.slice(0,2)==='//')return u;"
            "if(u.indexOf(X+'/')===0%s)return u;"
            "if(u.indexOf('/api/')===0||u==='/api')return X+u;"
            "if(u.indexOf('/assets/')===0)return X+u;"
            "}catch(e){}return u;}"
            "var of=window.fetch;"
            "window.fetch=function(i,o){"
            "if(typeof i==='string')return of.call(this,fix(i),o);"
            "if(i&&i.url)return of.call(this,new Request(fix(i.url),i),o);"
            "return of.call(this,i,o);};"
            "var ox=XMLHttpRequest.prototype.open;"
            "XMLHttpRequest.prototype.open=function(m,u){"
            "var a=[].slice.call(arguments);a[1]=fix(u);return ox.apply(this,a);};"
            "})();</script>"
        ) % (json.dumps(pre), json.dumps(cpre), pre_guard)
        pieces.append(interceptor)

        inject = "".join(pieces)
        if "</head>" in html:
            return html.replace("</head>", inject + "</head>", 1)
        return inject + html

    # -- 管理页静态资源 ----------------------------------------------------
    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if full.startswith(WEB_DIR) and os.path.isfile(full):
            with open(full, "rb") as f:
                data = f.read()
            if full.endswith("index.html"):
                return self._send(200, self._inject_base(data), "text/html; charset=utf-8")
            return self._send(200, data, mime_of(full))

        # 图标是构建产物（见 .gitignore），fpk 由 pack.sh 现场生成。
        # 若以「直接跑源码」的方式启动且没执行过 pack.sh，web/icon.png 并不存在，
        # 这里退回 app/ui/images 里的同名生成物，避免 logo 404。
        if rel == "icon.png":
            alt = os.path.normpath(
                os.path.join(WEB_DIR, "..", "..", "ui", "images", "icon_256.png"))
            if os.path.isfile(alt):
                with open(alt, "rb") as f:
                    return self._send(200, f.read(), "image/png")

        # 走到这里说明路径没有对应任何真实文件 —— 一律 404。
        #
        # 这里原先是「SPA 回退」：任何**不像资源**的路径（basename 不含点）
        # 都返回 index.html。那是个漏洞 —— 实测 `/test/test`、`/任意/乱写`
        # 都能打开管理界面（地址栏还是一串无关路径）。
        #
        # 之所以能安全去掉回退：前端用的是 **hash 路由**
        # （app.js: `location.hash = name`，即 #overview / #core 这类），
        # path 层面从不导航。它对 path 的全部需求只有三样：
        #   ① `/` 或 `/index.html` 拿到首页 —— 上面已按真实文件返回；
        #   ② 首页里那几个相对资源（app.js / app.css / icon.png）—— 同理；
        #   ③ /panel-api/* 与 /console/* —— 在 _route 里单独分支处理，不走这里。
        # 因此没有未知 path 需要回退，去掉它不影响任何正常使用。
        return self._send(404, "404 Not Found", "text/plain; charset=utf-8")

    def _inject_base(self, data):
        """给 index.html 注入 <base>，使相对路径在任意挂载前缀下都解析正确。

        直连（/）时前缀为空 -> base="/"；
        经飞牛网关（/app/vnts-nas/）时 base="/app/vnts-nas/"。
        """
        html = data.decode("utf-8", "replace")
        if "<base " in html:
            return data
        pre = getattr(self, "_prefix", "") or ""
        base = pre + "/" if pre else "/"

        # 注入 <base> 与令牌（令牌自动带给前端，用户无需手工填写）
        token = S.get_panel_token()
        inject = '<base href="%s">' % base
        if token:
            inject += (
                "<script>window.__PANEL__={token:%s};"
                "try{localStorage.setItem('panel_token',%s);}catch(e){}</script>"
                % (json.dumps(token), json.dumps(token))
            )
        if "<head>" in html:
            html = html.replace("<head>", "<head>" + inject, 1)
        else:
            html = inject + html
        return html.encode("utf-8")


HAVE_AF_UNIX = hasattr(socket, "AF_UNIX")


class PanelHTTPServer(ThreadingHTTPServer):
    """管理页/反代用的 HTTP 服务器。

    关键调整（默认值不够用）：
      - request_queue_size 默认仅 5。控制台是 SPA，Vite 会**并行**预加载十几个
        分块（js/css），队列一满内核就拒掉新连接，浏览器报 net::ERR_*，
        随后重试才成功（Network 面板会出现「(failed) + 200」成对记录）。
        这里放宽到 128，避免这类抖动。
      - daemon_threads：主进程退出时不让残留线程拖住。
      - allow_reuse_address：重启后立即复用端口，避免 TIME_WAIT 占用。
    """
    request_queue_size = 128
    daemon_threads = True
    allow_reuse_address = True
    # 单连接读超时：防止慢客户端长期占线程
    timeout = 60

    def handle_error(self, request, client_address):
        """兜住残余的连接类异常，别把 traceback 打到 stderr。

        socketserver 默认实现会把整段 traceback 写进 sys.stderr，
        而本服务的 stderr 指向日志文件 —— 客户端正常断开就会刷满日志。
        Handler.handle_one_request 已处理读阶段的异常，这里是最后一道防线
        （例如 _send 之后连接才被重置、或 HEAD 探测中途断开）。
        非连接类异常仍然照常打印，以免掩盖真实故障。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        # BrokenPipeError 在部分平台表现为普通 OSError，一并容错
        if isinstance(exc, OSError) and getattr(exc, "errno", None) in (
                errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


if HAVE_AF_UNIX:
    class UnixHTTPServer(PanelHTTPServer):
        address_family = socket.AF_UNIX

        def server_bind(self):
            try:
                if os.path.exists(self.server_address):
                    os.unlink(self.server_address)
            except OSError:
                pass
            socketserver.TCPServer.server_bind(self)
            try:
                os.chmod(self.server_address, 0o660)
            except OSError:
                pass

        def get_request(self):
            req, _ = self.socket.accept()
            return req, ("unix", 0)


def serve_unix():
    if not HAVE_AF_UNIX:
        S.log("unix socket 不受当前平台支持（非 fnOS 环境），跳过")
        return
    try:
        srv = UnixHTTPServer(S.SOCK_PATH, Handler)
    except Exception as e:  # noqa: BLE001
        S.log("unix socket listen failed: %s" % e)
        return
    S.log("unix socket listening: %s" % S.SOCK_PATH)
    try:
        srv.serve_forever()
    except Exception as e:  # noqa: BLE001
        S.log("unix server stopped: %s" % e)


def panel_bind_host():
    v = (os.environ.get("PANEL_BIND") or "").strip()
    if v in ("0.0.0.0", "127.0.0.1", "::", "::1"):
        return v
    return "127.0.0.1"


def serve_tcp():
    ports = S.load_ports()
    port = int(ports["PANEL_PORT"])
    host = panel_bind_host()
    try:
        srv = PanelHTTPServer((host, port), Handler)
    except OSError as e:
        S.log("管理页端口 %d 在 %s 不可用: %s" % (port, host, e))
        srv = None
        for alt in range(port + 1, port + 11):
            try:
                srv = PanelHTTPServer((host, alt), Handler)
                ports["PANEL_PORT"] = str(alt)
                S.save_ports(ports)
                S.log("管理页端口回退到 %d" % alt)
                break
            except OSError:
                continue
        if srv is None:
            S.log("无可用管理页端口，服务未启动")
            return

    S.log("管理页监听: %s:%s (backlog=%d)"
          % (host, ports["PANEL_PORT"], PanelHTTPServer.request_queue_size))
    if host in ("0.0.0.0", "::"):
        S.log("提示: 管理页对局域网开放；如需仅本机访问，设 PANEL_BIND=127.0.0.1")
    srv.serve_forever()


def main():
    S.log("=== vnts-nas shell backend starting ===")
    S.ensure_dirs()
    S.trim_logs()
    threading.Thread(target=S.watcher, daemon=True).start()
    threading.Thread(target=serve_unix, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(2), S.bootstrap()), daemon=True).start()
    serve_tcp()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        S.log("interrupted, shutting down")
        S.stop_core()
