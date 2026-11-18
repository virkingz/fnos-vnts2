#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发加载测试：复现并验证「路由切换时多个分块并行加载」的场景。

背景（真实症状）：
  控制台切到「系统设置」会并行加载约 6 个懒加载分块
  （SettingsView-*.js/.css、clipboard-*.js、ConfirmDialog-*.js、eye-*.js、modules-*.js）。
  Python 的 socketserver.request_queue_size 默认只有 5，
  超出队列的连接会被内核拒绝 → 浏览器 Network 面板出现
  「(failed) net::ERR_*」与随后重试的 200 成对记录。

本测试用 N 个**同时**发起的连接打服务器，统计失败数。
并可反向验证：把 backlog 调回 5 时应当出现失败。
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "shell"))
PASS = FAIL = 0

# 模拟「系统设置」视图并行拉取的分块数量
PARALLEL = 12


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def main():
    base = tempfile.mkdtemp(prefix="vnts-conc-")
    var = os.path.join(base, "var")
    os.makedirs(os.path.join(var, "server", "logs"), exist_ok=True)
    os.makedirs(os.path.join(base, "log"), exist_ok=True)

    cport = _free_port()
    pport = _free_port()
    with open(os.path.join(var, "ports.env"), "w") as f:
        f.write("PANEL_PORT=%d\nSERVICE_PORT=29872\nCONSOLE_PORT=%d\n" % (pport, cport))

    os.environ.update({"VNT_PKGVAR": var, "VNT_APPDEST": base, "VNT_APPNAME": "vnts-nas"})
    import shell_server as S
    import shell_http as H

    print("[1] 服务器 backlog 已放宽（默认 5 不够用）")
    check("PanelHTTPServer 存在", hasattr(H, "PanelHTTPServer"))
    check("backlog >= 64", H.PanelHTTPServer.request_queue_size >= 64,
          H.PanelHTTPServer.request_queue_size)
    check("开启 daemon 线程", H.PanelHTTPServer.daemon_threads is True)

    # 一个只回小响应的上游，避免依赖真实 vnts2
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Up(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'{"code":200,"msg":"ok","data":1}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    up = ThreadingHTTPServer(("127.0.0.1", cport), Up)
    up.request_queue_size = 128
    threading.Thread(target=up.serve_forever, daemon=True).start()

    S.ensure_dirs(); S.render_config()
    ports = S.load_ports(); ports["PANEL_PORT"] = str(pport); S.save_ports(ports)
    srv = H.PanelHTTPServer(("127.0.0.1", pport), H.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.8)

    token = S.get_panel_token()

    print("\n[2] %d 个并发请求同时打管理页（模拟路由切换的分块并行加载）" % PARALLEL)
    errs, oks = _burst(pport, PARALLEL, token)
    check("并发请求全部成功（无连接被拒）", not errs, "失败 %d/%d: %s" % (len(errs), PARALLEL, errs[:3]))
    check("成功数等于并发数", oks == PARALLEL, "%d/%d" % (oks, PARALLEL))

    print("\n[3] HEAD 请求不得让连接失步（真实症状根因）")
    # HEAD 必须回 Content-Length: 0。若按 GET 的长度声明却无 body，
    # 客户端会一直等那些字节 → 超时/失步 → 浏览器报 net::ERR_* 后重试，
    # 于是 Network 面板出现「(failed) + 200」成对记录。
    hd_ok, detail = _head_test(pport)
    check("HEAD 响应 Content-Length 为 0", hd_ok, detail)
    ka_ok, kd = _head_then_get(pport)
    check("HEAD 后复用同连接仍可正常 GET（不失步）", ka_ok, kd)

    print("\n[4] 连接复用：同连接连续 3 个请求")
    reuse, rdet = _keepalive_test(pport)
    check("keep-alive 连续请求正常", reuse, rdet)

    srv.shutdown(); up.shutdown()
    shutil.rmtree(base, ignore_errors=True)
    print("\n===== 结果：%d 通过 / %d 失败 =====" % (PASS, FAIL))
    return 1 if FAIL else 0


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _parse_cl(head):
    for ln in head.split("\r\n"):
        if ln.lower().startswith("content-length:"):
            try:
                return int(ln.split(":", 1)[1].strip())
            except ValueError:
                return -1
    return -1


def _head_test(port):
    """发 HEAD，检查 Content-Length 是否为 0 且连接正常关闭（不超时）。"""
    sk = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sk.sendall(b"HEAD / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        sk.settimeout(3)
        data = b""
        while True:
            c = sk.recv(4096)
            if not c:
                break
            data += c
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin1")
        cl = _parse_cl(head)
        return cl == 0, "Content-Length=%d" % cl
    except OSError as e:
        return False, "超时/出错: %s" % e
    finally:
        try:
            sk.close()
        except OSError:
            pass


def _head_then_get(port):
    """同一连接：先 HEAD 再 GET，验证连接未失步。"""
    sk = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sk.sendall(b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n")
        sk.settimeout(3)
        d = b""
        while b"\r\n\r\n" not in d:
            c = sk.recv(4096)
            if not c:
                break
            d += c
        if b"\r\n\r\n" not in d:
            return False, "HEAD 无完整响应"
        sk.sendall(b"GET /panel-api/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        d2 = b""
        while True:
            c = sk.recv(4096)
            if not c:
                break
            d2 += c
        ok = d2.startswith(b"HTTP/1.1 200")
        return ok, d2.split(b"\r\n", 1)[0].decode("latin1") if d2 else "无响应"
    except OSError as e:
        return False, "失步/超时: %s" % e
    finally:
        try:
            sk.close()
        except OSError:
            pass


def _keepalive_test(port):
    """同连接连续 3 个请求，验证 keep-alive 正常。"""
    sk = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        for i in range(3):
            sk.sendall(b"GET /panel-api/health HTTP/1.1\r\nHost: x\r\n\r\n")
            sk.settimeout(3)
            data = b""
            while b"\r\n\r\n" not in data:
                c = sk.recv(4096)
                if not c:
                    break
                data += c
            head, _, rest = data.partition(b"\r\n\r\n")
            cl = _parse_cl(head.decode("latin1"))
            while len(rest) < cl:
                c = sk.recv(4096)
                if not c:
                    break
                rest += c
            if not head.startswith(b"HTTP/1.1 200"):
                return False, "第 %d 个请求异常" % (i + 1)
        return True, "3 个请求均正常"
    except OSError as e:
        return False, "超时/出错: %s" % e
    finally:
        try:
            sk.close()
        except OSError:
            pass


def _burst(port, n, token):
    """同时发起 n 个 HTTP 请求，返回 (错误列表, 成功数)。"""
    errs = []
    oks = [0]
    lock = threading.Lock()
    start = threading.Barrier(n)

    def one(i):
        try:
            start.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=8)
        except OSError as e:
            with lock:
                errs.append("connect#%d:%s" % (i, e))
            return
        try:
            req = ("GET /panel-api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   "X-Panel-Token: %s\r\nConnection: close\r\n\r\n" % token)
            s.sendall(req.encode())
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            if b"200" in data.split(b"\r\n", 1)[0]:
                with lock:
                    oks[0] += 1
            else:
                with lock:
                    errs.append("resp#%d:%s" % (i, data[:60]))
        except OSError as e:
            with lock:
                errs.append("recv#%d:%s" % (i, e))
        finally:
            try:
                s.close()
            except OSError:
                pass

    ts = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=15)
    return errs, oks[0]


if __name__ == "__main__":
    sys.exit(main())
