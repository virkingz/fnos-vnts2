#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SOCKS5 代理支持与核心日志配置的测试。

全部在**回环**内完成：自建「假 SOCKS5 代理 + 假目标服务器」验证链路，
不访问外网、不依赖真实代理软件。

覆盖：
  - SOCKS5 协议报文（握手 / CONNECT / 三种 ATYP / 错误码翻译）
  - socks5 与 socks5h 的语义差异（本地解析 vs 远端解析）
  - 本地中转让 urllib 可用（这是 urllib 唯一能接 SOCKS 的方式）
  - 中转服务器在请求结束后被关闭（不泄漏监听端口）
  - 代理校验：接受 socks5/socks5h，拒绝 socks4
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


import socks5 as S  # noqa: E402  (需在 sys.path 设置后导入)

# ===========================================================================
print("=" * 66)
print("[1] SOCKS5 协议：握手与 CONNECT")
# ===========================================================================
tport = start_target(b"HELLO")
rec = {}
pport = start_socks(record=rec)

sock = S.connect("127.0.0.1", pport, "127.0.0.1", tport, remote_dns=False, timeout=5)
check("握手字节正确（VER=5, 1 种方法, 无认证）", rec["hello"] == b"\x05\x01\x00", rec["hello"])
check("CONNECT 请求头正确（VER=5, CMD=1, RSV=0）", rec["req"][:3] == b"\x05\x01\x00", rec["req"])
check("目标为 IPv4（ATYP=1）", rec["dest"][2] == 1, rec["dest"])

sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
got = sock.recv(200)
check("隧道可双向传输", b"HELLO" in got, got[:40])
sock.close()

# ===========================================================================
print("\n[2] socks5 vs socks5h：域名解析位置")
# ===========================================================================
rec2 = {}
p2 = start_socks(record=rec2)
# socks5h：域名原样交给代理（ATYP=3）
try:
    s = S.connect("127.0.0.1", p2, "internal.example.com", 443,
                  remote_dns=True, timeout=5)
    s.close()
except S.SocksError:
    pass
check("socks5h 把域名原样交给代理（ATYP=3）", rec2["dest"][2] == 3, rec2["dest"])
check("socks5h 域名未被本地改写", rec2["dest"][0] == "internal.example.com", rec2["dest"])
check("socks5h 端口正确传递", rec2["dest"][1] == 443, rec2["dest"])

rec3 = {}
p3 = start_socks(record=rec3)
try:
    s = S.connect("127.0.0.1", p3, "127.0.0.1", 80, remote_dns=False, timeout=5)
    s.close()
except S.SocksError:
    pass
check("socks5 本地解析为 IP（ATYP=1）", rec3["dest"][2] == 1, rec3["dest"])

# ===========================================================================
print("\n[3] 错误码与异常翻译")
# ===========================================================================
for code, keyword in [(0x04, "主机不可达"), (0x05, "连接被拒绝"), (0x02, "规则不允许")]:
    rec4 = {}
    pp = start_socks(record=rec4, reply=code)
    try:
        S.connect("127.0.0.1", pp, "x.example.com", 80, remote_dns=True, timeout=5)
        check("reply=0x%02x 应抛错" % code, False, "未抛错")
    except S.SocksError as e:
        check("reply=0x%02x 翻译为「%s」" % (code, keyword), keyword in str(e), str(e))

# 非 SOCKS 服务
plain = socket.socket()
plain.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
plain.bind(("127.0.0.1", 0))
plain.listen(4)
plain_port = plain.getsockname()[1]


def plain_serve():
    while True:
        try:
            c, _ = plain.accept()
        except OSError:
            return
        try:
            recvn(c, 3)
            c.sendall(b"\x04\x00")       # 版本 4 —— 不是 SOCKS5
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
threading.Thread(target=plain_serve, daemon=True).start()
try:
    S.connect("127.0.0.1", plain_port, "x", 80, timeout=5)
    check("非 SOCKS5 服务应抛错", False)
except S.SocksError as e:
    check("非 SOCKS5 服务给出明确提示", "不是 SOCKS5" in str(e), str(e))

# 代理完全不可达
dead = socket.socket()
dead.bind(("127.0.0.1", 0))
dead_port = dead.getsockname()[1]
dead.close()
try:
    S.connect("127.0.0.1", dead_port, "x", 80, timeout=3)
    check("代理不可达应抛错", False)
except S.SocksError as e:
    check("代理不可达给出明确提示", "无法连接" in str(e), str(e))

# 要求认证
auth = socket.socket()
auth.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
auth.bind(("127.0.0.1", 0))
auth.listen(4)
auth_port = auth.getsockname()[1]


def auth_serve():
    while True:
        try:
            c, _ = auth.accept()
        except OSError:
            return
        try:
            recvn(c, 3)
            c.sendall(b"\x05\x02")       # 只支持用户名密码认证
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
threading.Thread(target=auth_serve, daemon=True).start()
try:
    S.connect("127.0.0.1", auth_port, "x", 80, timeout=5)
    check("要求认证应抛错", False)
except S.SocksError as e:
    check("要求认证给出明确提示", "认证" in str(e), str(e))

# ===========================================================================
print("\n[4] 本地中转：让 urllib 能走 SOCKS5")
# ===========================================================================
# ⚠ 注意：不能拿 127.0.0.1 当 urllib 的目标来测。
# urllib 会查 no_proxy/proxy_bypass，而 no_proxy 通常含 127.0.0.1/localhost，
# 于是请求**直连**、根本不经过代理（这是 no_proxy 的预期行为）。
# 真实使用中目标都是 github.com 这类外部域名，不受影响。
# 因此这里用 SOCKS5 协议直连来验证中继的转发行为。
rec5 = {}
p5 = start_socks(record=rec5)
relay = S.SocksRelayServer("127.0.0.1", p5, remote_dns=True)
lport = relay.start()
check("中转服务器已监听", lport > 0, lport)

# 直接对中转发 CONNECT（这就是 urllib 会做的事），目标用非本机域名
c = socket.create_connection(("127.0.0.1", lport), timeout=5)
c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
resp = c.recv(200)
check("中转对 CONNECT 回 200", b"200" in resp, resp[:60])

for _ in range(50):
    if rec5.get("dest"):
        break
    time.sleep(0.02)
check("中转把 CONNECT 目标交给了 SOCKS5", rec5.get("dest") is not None, rec5.get("dest"))
check("交给代理的是目标域名与端口（不是中转端口）",
      (rec5.get("dest") or ("", 0, 0))[:2] == ("example.com", 443),
      (rec5.get("dest"), lport))
check("socks5h 语义下 ATYP 为域名", (rec5.get("dest") or ("", 0, 0))[2] == 3, rec5.get("dest"))
try:
    c.close()
except OSError:
    pass
relay.stop()

# 中转回 200 后，字节应当在隧道里双向流动（用真实目标验证）
target3 = start_target(b"TUNNEL-OK")
rec6 = {}
p6 = start_socks(record=rec6)
relay_b = S.SocksRelayServer("127.0.0.1", p6, remote_dns=False)
lpb = relay_b.start()
cb = socket.create_connection(("127.0.0.1", lpb), timeout=5)
cb.sendall(("CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % target3).encode())
head = cb.recv(200)
check("中转到本机目标也回 200", b"200" in head, head[:60])
cb.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
body = b""
try:
    while True:
        d = cb.recv(4096)
        if not d:
            break
        body += d
except OSError:
    pass
check("隧道内可完成完整 HTTP 往返", b"TUNNEL-OK" in body, body[-30:])
try:
    cb.close()
except OSError:
    pass
relay_b.stop()

# 连续两次请求：验证 stop 后端口仍可正常重建（不泄漏）
relay2 = S.SocksRelayServer("127.0.0.1", p5, remote_dns=True)
lp2 = relay2.start()
check("中转可重建（端口独立分配）", lp2 > 0 and lp2 != lport, (lport, lp2))
relay2.stop()

# ===========================================================================
print("\n[5] 非 CONNECT 请求被拒")
# ===========================================================================
relay3 = S.SocksRelayServer("127.0.0.1", p5, remote_dns=True)
lp3 = relay3.start()
try:
    c = socket.create_connection(("127.0.0.1", lp3), timeout=5)
    c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    resp = c.recv(200)
    check("非 CONNECT 返回 405", b"405" in resp, resp[:40])
    c.close()
except OSError as e:
    check("非 CONNECT 返回 405", False, str(e))
relay3.stop()

# ===========================================================================
print("\n[6] 代理校验：接受 socks5/socks5h，拒绝 socks4")
# ===========================================================================
base = tempfile.mkdtemp(prefix="vnts-log-")
os.environ["VNT_PKGVAR"] = os.path.join(base, "var")
os.environ["VNT_APPDEST"] = os.path.join(base, "app")
os.environ["LOG_DIR"] = os.path.join(base, "log")
import shell_server as P  # noqa: E402

for raw, want_ok in [("socks5://127.0.0.1:7891", True),
                     ("socks5h://127.0.0.1:7891", True),
                     ("socks4://127.0.0.1:1080", False),
                     ("socks5://127.0.0.1", False),
                     ("http://127.0.0.1:7890", True)]:
    addr, err = P.normalize_proxy(raw)
    check("%-28s -> %s" % (raw, "合法" if not err else "拒绝"),
          (not err) == want_ok, err or addr)

check("socks 判定函数正确", P._is_socks("socks5://x:1") and P._is_socks("SOCKS5H://x:1"))
check("http 不被误判为 socks", not P._is_socks("http://x:1"))

# ===========================================================================
print("\n[7] 日志级别 / 轮转可配置")
# ===========================================================================
P.ensure_dirs()
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
