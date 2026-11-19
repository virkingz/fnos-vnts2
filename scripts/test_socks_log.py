#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代理（curl 方案）与核心日志配置的测试。

全部在**回环**内完成：自建「假 SOCKS5 代理 + 假目标服务器」验证链路，
不访问外网、不依赖真实代理软件。

覆盖：
  - 有代理时一律走 curl（http/https/socks5/socks5h/socks4 行为一致）
  - curl 参数：--proxy 与 --noproxy 空串（后者防 NO_PROXY 把代理绕掉）
  - 无代理时走 urllib 直连，且清掉环境变量里的 http_proxy
  - 代理校验：放行 socks5/socks5h/socks4/socks4a/http，缺端口或非法协议被拒
  - 日志级别 / 大小 / 份数写入 log4rs.yaml，非法值被拒
  - trim 不再截断 vnts2.log（修掉的面板与核心轮转冲突）
"""
import os
import socket
import struct
import sys
import tempfile
import threading
import time

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


def recvn(sock, n):
    """读满 n 字节 —— TCP 会分包，一次 recv 不保证读全。"""
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            raise OSError("EOF")
        buf += d
    return buf


def start_target(respond=b"HELLO", hits=None):
    """起一个极简 HTTP 目标服务器，返回端口。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            if hits is not None:
                hits.append(1)

            def h(c):
                try:
                    req = b""
                    while b"\r\n\r\n" not in req:
                        d = c.recv(4096)
                        if not d:
                            break
                        req += d
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s"
                              % (len(respond), respond))
                except OSError:
                    pass
                finally:
                    try:
                        c.close()
                    except OSError:
                        pass
            threading.Thread(target=h, args=(c,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return port


def start_socks(record=None, reply=0x00):
    """起一个假 SOCKS5 代理。record 收集它看到的内容；reply 用于模拟错误码。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return

            def h(c):
                try:
                    hello = recvn(c, 3)
                    if record is not None:
                        record["hello"] = hello
                    c.sendall(b"\x05\x00")
                    hdr = recvn(c, 4)
                    if record is not None:
                        record["req"] = hdr
                    atyp = hdr[3]
                    if atyp == 1:
                        host = socket.inet_ntoa(recvn(c, 4))
                    elif atyp == 4:
                        host = socket.inet_ntop(socket.AF_INET6, recvn(c, 16))
                    else:
                        n = recvn(c, 1)[0]
                        host = recvn(c, n).decode("utf-8")
                    dport = struct.unpack("!H", recvn(c, 2))[0]
                    if record is not None:
                        record["dest"] = (host, dport, atyp)

                    if reply != 0x00:
                        c.sendall(bytes([5, reply, 0, 1]) + b"\x00" * 6)
                        c.close()
                        return

                    up = socket.create_connection((host, dport), timeout=5)
                    c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

                    def pump(a, b):
                        try:
                            while True:
                                d = a.recv(65536)
                                if not d:
                                    break
                                b.sendall(d)
                        except OSError:
                            pass
                        finally:
                            for s in (a, b):
                                try:
                                    s.close()
                                except OSError:
                                    pass
                    threading.Thread(target=pump, args=(c, up), daemon=True).start()
                    pump(up, c)
                except OSError:
                    try:
                        c.close()
                    except OSError:
                        pass
            threading.Thread(target=h, args=(c,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return port


import shell_server as P  # noqa: E402  (需在 sys.path 设置后导入)

# ===========================================================================
print("=" * 66)
print("[1] 有代理时一律交给 curl（不再自实现 SOCKS5 协议栈）")
# ===========================================================================
# 背景：早期版本用「本地回环中转 + 自实现 SOCKS5」走代理，在 Clash 上
# 表现为 HTTP 代理正常、SOCKS5 报 554。改为统一调 curl 后几种协议一致。
calls = []


def fake_run_factory(code=200, body=b"ok", rc=0, err=b""):
    def fake_run(args, **kw):
        calls.append(args)

        class R:
            returncode = rc
            stdout = body + b"\n" + str(code).encode()
            stderr = err

        return R()

    return fake_run


real_run = P.subprocess.run
real_find = P.find_curl
try:
    P.find_curl = lambda: "/usr/bin/curl"

    for px in ("http://127.0.0.1:7890", "socks5://127.0.0.1:1081",
               "socks5h://127.0.0.1:1081", "socks4://127.0.0.1:1080",
               "socks4a://127.0.0.1:1080"):
        calls[:] = []
        P.subprocess.run = fake_run_factory()
        with P._open("https://example.com/x", timeout=5, proxy=px) as r:
            r.read()
        args = calls[0]
        check("%-26s 走 curl 且带 --proxy" % px,
              args[0] == "/usr/bin/curl" and "--proxy" in args and px in args,
              args[:3])
        # NO_PROXY 环境变量会让 curl 绕过代理直连 —— 必须显式关掉
        check("%-26s 带 --noproxy 空串（防绕过）" % px,
              "--noproxy" in args and args[args.index("--noproxy") + 1] == "",
              args)

    # 无代理时不走 curl
    calls[:] = []
    P.subprocess.run = fake_run_factory()
    try:
        P._open("http://127.0.0.1:1/", timeout=1, proxy="")
    except Exception:  # noqa: BLE001
        pass
    check("无代理时不调 curl", calls == [], calls)

    # curl 返回非 0 → 抛出可读错误
    P.subprocess.run = fake_run_factory(rc=7, err=b"curl: (7) Failed to connect")
    try:
        P._open("https://example.com/", timeout=5, proxy="socks5h://127.0.0.1:1")
        check("curl 失败应抛错", False)
    except Exception as e:  # noqa: BLE001
        check("curl 失败抛可读错误", "curl" in str(e), str(e)[:80])

    # 找不到 curl → 明确提示
    P.find_curl = lambda: ""
    try:
        P._open("https://example.com/", timeout=5, proxy="socks5h://127.0.0.1:1")
        check("缺 curl 应抛错", False)
    except Exception as e:  # noqa: BLE001
        check("缺 curl 时给出明确提示", "curl" in str(e), str(e)[:80])
finally:
    P.subprocess.run = real_run
    P.find_curl = real_find

# ===========================================================================
print("\n[2] 响应包装：status / read / geturl / 上下文管理")
# ===========================================================================
resp = P._CurlResponse(200, b"abcdef", "https://x/y")
check("status 可读", resp.status == 200 and resp.code == 200)
check("read() 全量", resp.read() == b"abcdef")
check("read(n) 分段", P._CurlResponse(200, b"abcdef", "u").read(2) == b"ab")
r2 = P._CurlResponse(200, b"abcdef", "u")
r2.read(2)
check("read(n) 续读", r2.read(2) == b"cd")
check("geturl 返回原地址", resp.geturl() == "https://x/y")
with P._CurlResponse(200, b"x", "u") as rr:
    check("支持上下文管理", rr.status == 200)

# ===========================================================================
print("\n[3] 代理校验：放行 socks4，拒绝非法协议与缺端口")
# ===========================================================================
for raw, want_ok in [
    ("socks5://127.0.0.1:7891", True),
    ("socks5h://127.0.0.1:1080", True),
    ("socks4://127.0.0.1:1080", True),
    ("socks4a://127.0.0.1:1080", True),
    ("http://127.0.0.1:7890", True),
    ("socks5://127.0.0.1", False),
    ("ftp://1.2.3.4:21", False),
]:
    addr, err = P.normalize_proxy(raw)
    check("%-28s -> %s" % (raw, "合法" if not err else "拒绝"),
          (not err) == want_ok, err or addr)

check("socks 判定函数正确", P._is_socks("socks5://x:1") and P._is_socks("SOCKS5H://x:1"))
check("socks4 也归入 socks 系", P._is_socks("socks4://x:1"))
check("http 不被误判为 socks", not P._is_socks("http://x:1"))

# 无 curl 时的降级：HTTP 代理回去走 urllib；SOCKS 代理必须明确报错
real_find2 = P.find_curl
try:
    P.find_curl = lambda: ""
    # HTTP 代理 + 无 curl → 降级到 urllib（会因端口没人监听而失败，
    # 但错误信息不应是「未找到 curl」）
    try:
        P._open("http://127.0.0.1:1/", timeout=1, proxy="http://127.0.0.1:2")
        check("HTTP 代理无 curl 时降级", False, "不该成功")
    except Exception as e:  # noqa: BLE001
        check("HTTP 代理无 curl 时降级到 urllib（非缺少 curl 的错）",
              "未找到 curl" not in str(e), str(e)[:70])
    # SOCKS 代理 + 无 curl → 必须明确提示缺 curl
    try:
        P._open("http://127.0.0.1:1/", timeout=1, proxy="socks5h://127.0.0.1:2")
        check("SOCKS 无 curl 应报错", False)
    except Exception as e:  # noqa: BLE001
        check("SOCKS 无 curl 时提示明确", "curl" in str(e), str(e)[:70])
finally:
    P.find_curl = real_find2

# ===========================================================================
print("\n[7] 日志级别 / 轮转可配置")
# ===========================================================================
P.ensure_dirs()
# 先清掉可能残留的日志配置，确保从「默认值」开始断言：
# 本文件后续小节（[9]）会把配置改成 512KB 等值，若顺序变化会污染默认值断言。
try:
    os.remove(P.LOG_CONF_FILE)
except OSError:
    pass
P.render_config()
conf = P.load_log_conf()
check("默认级别为 info", conf["level"] == "info", conf)
check("默认 10MB / 5 份", conf["max_kb"] == 10240 and conf["keep"] == 5, conf)

y = open(os.path.join(P.SERVER_DIR, "logs", "log4rs.yaml"), encoding="utf-8").read()
check("默认写进 yaml 的 level=info", "level: info" in y)
check("默认写进 yaml 的 limit=10 mb（10240KB 整除故用 mb）", "limit: 10 mb" in y)
check("默认写进 yaml 的 count=5", "count: 5" in y)

c, errs = P.normalize_log_conf("debug", 51200, 10)
check("合法值通过校验", not errs, errs)
P.save_log_conf(c)
y = open(os.path.join(P.SERVER_DIR, "logs", "log4rs.yaml"), encoding="utf-8").read()
check("改后 level=debug 生效", "level: debug" in y)
check("改后 limit=50 mb 生效", "limit: 50 mb" in y)
check("改后 count=10 生效", "count: 10" in y)

check("非法级别被拒", P.normalize_log_conf("verbose", None, None)[1])
check("大小越界被拒", P.normalize_log_conf(None, 0, None)[1])
check("份数越界被拒", P.normalize_log_conf(None, None, 999)[1])
check("非数字被拒", P.normalize_log_conf(None, "abc", None)[1])
check("只传 level 时其它字段保留",
      P.normalize_log_conf("warn", None, None)[0]["max_kb"] == 51200,
      P.normalize_log_conf("warn", None, None)[0])

# 配置损坏时回落默认
P.write_file(P.LOG_CONF_FILE, "{ 这不是 json")
check("配置损坏回落默认", P.load_log_conf()["level"] == "info")
P.write_file(P.LOG_CONF_FILE, '{"level":"nope","max_kb":999999999,"keep":-1}')
d = P.load_log_conf()
check("配置非法值被逐项过滤", d == {"level": "info", "max_kb": 10240, "keep": 5}, d)

# 视图含界面需要的元信息
P.save_log_conf({"level": "debug", "max_kb": 20480, "keep": 3})
v = P.log_conf_view()
check("视图含级别列表", "debug" in v["levels"], v["levels"])
check("视图含取值范围", v["max_kb_range"] == [64, 1048576], v["max_kb_range"])

# ===========================================================================
print("\n[8] trim 不再截断 vnts2.log（面板与核心轮转不再打架）")
# ===========================================================================
vlog = os.path.join(P.log_dir(), "vnts2.log")
open(vlog, "wb").write(b"X" * (1024 * 1024))
check("核心 log4rs 已生效", P._core_log4rs_active() is True)
P.trim_logs()
check("vnts2.log 保持 1MB 不被面板截断",
      os.path.getsize(vlog) == 1024 * 1024, os.path.getsize(vlog))

mlog = os.path.join(P.log_dir(), "main.log")
open(mlog, "wb").write(b"Y" * (1024 * 1024))
P.trim_logs()
check("面板自己的 main.log 仍会截断",
      os.path.getsize(mlog) < 512 * 1024, os.path.getsize(mlog))

# 核心未用 log4rs 时（老核心）兜底截断
os.replace(os.path.join(P.SERVER_DIR, "logs", "log4rs.yaml"),
           os.path.join(P.SERVER_DIR, "logs", "log4rs.yaml.off"))
open(vlog, "wb").write(b"X" * (1024 * 1024))
check("无 log4rs 时判定为未生效", P._core_log4rs_active() is False)
P.trim_logs()
check("无 log4rs 时兜底截断 vnts2.log",
      os.path.getsize(vlog) < 512 * 1024, os.path.getsize(vlog))

# ===========================================================================
print("\n[9] 日志上限支持 KB 档（含 512KB）")
# ===========================================================================
# log4rs 的 size 支持 b/kb/kib/mb/gb/tb，且大小写不敏感
# （源码 size.rs：unit.eq_ignore_ascii_case("kb") -> number * 1024）。
# 因此 512 KB 可以精确表达，界面按 KB 提交。
c, errs = P.normalize_log_conf(None, 512, None)
check("512 KB 被接受", not errs and c["max_kb"] == 512, (errs, c))
P.save_log_conf(c)
y = open(os.path.join(P.SERVER_DIR, "logs", "log4rs.yaml"), encoding="utf-8").read()
check("512KB 写进 yaml 为 '512 kb'（未满 1MB 不换算成 mb）", "limit: 512 kb" in y,
      [l for l in y.splitlines() if "limit" in l])

# 整 MB 仍写成 mb，便于阅读（与 kb 等价）
c, _ = P.normalize_log_conf(None, 2048, None)
P.save_log_conf(c)
y = open(os.path.join(P.SERVER_DIR, "logs", "log4rs.yaml"), encoding="utf-8").read()
check("2048KB 写成 '2 mb'", "limit: 2 mb" in y, [l for l in y.splitlines() if "limit" in l])

# 非整 MB 的 KB 一律用 kb
c, _ = P.normalize_log_conf(None, 1536, None)
P.save_log_conf(c)
y = open(os.path.join(P.SERVER_DIR, "logs", "log4rs.yaml"), encoding="utf-8").read()
check("1536KB 写成 '1536 kb'", "limit: 1536 kb" in y)

# 兼容老配置：只有 max_mb 时应换算为 KB
P.write_file(P.LOG_CONF_FILE, '{"level":"warn","max_mb":3,"keep":4}')
old = P.load_log_conf()
check("老配置 max_mb=3 换算成 max_kb=3072",
      old["max_kb"] == 3072 and old["level"] == "warn" and old["keep"] == 4, old)

# 越界
check("低于下限被拒", P.normalize_log_conf(None, 10, None)[1], )
check("高于上限被拒", P.normalize_log_conf(None, 2 * 1024 * 1024, None)[1])
check("小数 KB 可四舍五入", P.normalize_log_conf(None, 512.6, None)[0]["max_kb"] == 513)
check("空串按未填处理（不改动）",
      P.normalize_log_conf(None, "", None)[0]["max_kb"] == P.load_log_conf()["max_kb"])

# 视图里有 KB 范围与常用档位
v = P.log_conf_view()
check("视图提供 512KB 档位", 512 in v["presets_kb"], v["presets_kb"])
check("视图含 max_kb", "max_kb" in v, list(v.keys())[:8])
check("视图同时给出 max_mb 便于显示", v["max_mb"] == round(v["max_kb"] / 1024.0, 2), v.get("max_mb"))

print("\n[10] 客户端 RST 断开不再刷 traceback（原报错来源）")
# 现象：浏览器切路由 / 反代探活后立刻断开时，BaseHTTPRequestHandler 的
# rfile.readline() 抛 ConnectionResetError，socketserver 把整段 traceback
# 打到 stderr（进而进日志），把日志刷满。
import contextlib  # noqa: E402
import io  # noqa: E402
import struct as _struct  # noqa: E402
import shell_http as H  # noqa: E402

srv = H.PanelHTTPServer(("127.0.0.1", 0), H.Handler)
srv_port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    for _ in range(12):
        sk = socket.create_connection(("127.0.0.1", srv_port), timeout=5)
        sk.sendall(b"GET /panel-api/health HTTP/1.1\r\nHost: x\r\n")   # 半截请求
        # SO_LINGER 强制发 RST，模拟客户端粗暴断开
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                      _struct.pack("ii", 1, 0))
        sk.close()
    time.sleep(1.0)

noise = buf.getvalue()
check("RST 断开不产生 stderr 输出", noise.strip() == "", repr(noise[:300]))
check("stderr 里没有 traceback", "Traceback" not in noise)
check("stderr 里没有 ConnectionResetError", "ConnectionResetError" not in noise)


def _raw(payload, timeout=3):
    sk = socket.create_connection(("127.0.0.1", srv_port), timeout=timeout)
    sk.sendall(payload)
    out = b""
    try:
        while True:
            d = sk.recv(4096)
            if not d:
                break
            out += d
    except OSError:
        pass
    sk.close()
    return out


check("异常输入后服务仍正常",
      b"200 OK" in _raw(b"GET /panel-api/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"))
check("HEAD 仍是 Content-Length: 0",
      b"Content-Length: 0" in _raw(b"HEAD /panel-api/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"))
check("畸形请求行不导致进程崩溃",
      isinstance(_raw(b"GARBAGE\r\n\r\n"), bytes))
srv.shutdown()
srv.server_close()

print("\n" + "=" * 66)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
