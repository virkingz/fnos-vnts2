#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯标准库的 SOCKS5 代理支持。

## 为什么自己写

`urllib` 的 `ProxyHandler` 只认 `http://` / `https://` 代理，标准库不提供
SOCKS 能力（PySocks 是第三方包）。而本项目在飞牛 NAS 上运行，安装第三方
依赖需要联网+编译环境，运维成本高、失败面大 —— 因此坚持零依赖。

SOCKS5 的 CONNECT 本身很简单（RFC 1928：握手两字节、请求几个字节），
自己实现既能覆盖需求，又不必引入依赖。

## 思路：本地中转 + 隧道

`urllib` 只会对 http 代理发 `CONNECT host:port` 来建隧道。所以这里：

    urllib ──CONNECT──> 本地回环中转端口 ──SOCKS5──> 真正的 SOCKS5 代理 ──> 目标

中转线程收到 CONNECT 后，向 SOCKS5 代理发起真正的 CONNECT，成功即回
`200 Connection established`，随后两个方向对拷字节。对 urllib 而言它就是在
用一个普通 http 代理，完全无感。

只实现 CONNECT：本项目用它下载 GitHub 文件与查版本号，全是 HTTPS。

## socks5 vs socks5h

- `socks5://`  域名由**本地**解析成 IP 再发给代理
- `socks5h://` 域名**原样交给代理**解析（远端 DNS）

需要经过代理才能解析内部域名时用 `socks5h`；这也是 Clash 等工具的惯例。

仅使用 Python 标准库，兼容 Python 3.8+。
"""

import socket
import struct
import threading

SOCKS_VERSION = 5
# 认证方式：0x00 无需认证
_AUTH_NONE = 0x00
_AUTH_NO_ACCEPTABLE = 0xFF
# 命令：0x01 CONNECT
_CMD_CONNECT = 0x01
# 地址类型
_ATYP_IPV4 = 0x01
_ATYP_DOMAIN = 0x03
_ATYP_IPV6 = 0x04

DEFAULT_TIMEOUT = 15


class SocksError(OSError):
    """SOCKS5 交互失败。消息面向用户，可直接展示。"""


# RFC 1928 第 6 章的 reply 字段定义
_REPLY_TEXT = {
    0x01: "代理返回：通用失败",
    0x02: "代理返回：规则不允许连接",
    0x03: "代理返回：网络不可达",
    0x04: "代理返回：主机不可达（目标地址不通）",
    0x05: "代理返回：连接被拒绝",
    0x06: "代理返回：TTL 超时",
    0x07: "代理返回：不支持的命令",
    0x08: "代理返回：不支持的地址类型",
}


def _recv_exact(sock, n):
    """读满 n 字节；对端提前关闭则抛 SocksError。"""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SocksError("代理连接被提前关闭（握手未完成）")
        buf += chunk
    return buf


def _build_addr(host, port, remote_dns):
    """构造 ATYP + 地址 + 端口。remote_dns=True 时域名原样交给代理。"""
    host = (host or "").strip()
    if not host:
        raise SocksError("目标主机名为空")

    if remote_dns:
        try:
            hb = host.encode("idna")
        except (UnicodeError, UnicodeDecodeError):
            hb = host.encode("utf-8")
        if len(hb) > 255:
            raise SocksError("域名过长，无法通过 SOCKS5 传输")
        return bytes([_ATYP_DOMAIN, len(hb)]) + hb + struct.pack("!H", port)

    # 本地解析成 IP 再发（socks5://）
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    if not infos:
        raise SocksError("无法解析目标主机：%s" % host)
    family, _, _, _, sockaddr = infos[0]
    ip = sockaddr[0]
    if family == socket.AF_INET6:
        return bytes([_ATYP_IPV6]) + socket.inet_pton(socket.AF_INET6, ip) + struct.pack("!H", port)
    return bytes([_ATYP_IPV4]) + socket.inet_aton(ip) + struct.pack("!H", port)


def _skip_bound_addr(sock, atyp):
    """读掉 BND.ADDR + BND.PORT，我们不需要这两个值。"""
    if atyp == _ATYP_IPV4:
        _recv_exact(sock, 4)
    elif atyp == _ATYP_IPV6:
        _recv_exact(sock, 16)
    elif atyp == _ATYP_DOMAIN:
        n = _recv_exact(sock, 1)[0]
        _recv_exact(sock, n)
    else:
        raise SocksError("代理返回了未知的地址类型 0x%02x" % atyp)
    _recv_exact(sock, 2)


def connect(proxy_host, proxy_port, dest_host, dest_port,
            remote_dns=False, timeout=DEFAULT_TIMEOUT):
    """经 SOCKS5 代理连到 dest_host:dest_port，返回**已建好隧道**的 socket。

    调用方负责关闭返回的 socket。失败抛 SocksError（消息可直接展示给用户）。
    """
    try:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    except OSError as e:
        raise SocksError("无法连接 SOCKS5 代理 %s:%s —— %s" % (proxy_host, proxy_port, e))

    try:
        sock.settimeout(timeout)
        # 1) 握手：声明只支持「无认证」
        sock.sendall(bytes([SOCKS_VERSION, 1, _AUTH_NONE]))
        resp = _recv_exact(sock, 2)
        if resp[0] != SOCKS_VERSION:
            raise SocksError("对方不是 SOCKS5 代理（版本字节 0x%02x）" % resp[0])
        if resp[1] == _AUTH_NO_ACCEPTABLE:
            raise SocksError("SOCKS5 代理要求认证，本面板不支持带账号密码的代理")
        if resp[1] != _AUTH_NONE:
            raise SocksError("SOCKS5 代理要求不支持的认证方式 0x%02x" % resp[1])

        # 2) 请求：CONNECT
        addr = _build_addr(dest_host, dest_port, remote_dns)
        sock.sendall(bytes([SOCKS_VERSION, _CMD_CONNECT, 0x00]) + addr)

        # 3) 应答
        head = _recv_exact(sock, 4)
        if head[0] != SOCKS_VERSION:
            raise SocksError("SOCKS5 应答版本异常 0x%02x" % head[0])
        if head[1] != 0x00:
            raise SocksError(_REPLY_TEXT.get(head[1], "代理返回错误码 0x%02x" % head[1]))
        _skip_bound_addr(sock, head[3])
        sock.settimeout(None)
        return sock
    except SocksError:
        try:
            sock.close()
        except OSError:
            pass
        raise
    except OSError as e:
        try:
            sock.close()
        except OSError:
            pass
        raise SocksError("SOCKS5 交互失败：%s" % e)


# ---------------------------------------------------------------------------
# 本地中转：让 urllib 的 http 代理机制能走 SOCKS5
# ---------------------------------------------------------------------------
def _pipe(a, b, closer):
    """a -> b 单向对拷，任一方向出错就一起收场。"""
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        closer()


class _Relay(object):
    """一条 CONNECT 会话：本地客户端 <-> SOCKS5 隧道。"""

    def __init__(self, client, proxy_addr, remote_dns, timeout):
        self.client = client
        self.proxy_addr = proxy_addr
        self.remote_dns = remote_dns
        self.timeout = timeout

    def run(self):
        upstream = None
        try:
            self.client.settimeout(self.timeout)
            line = self._read_request()
            if line is None:
                return
            method, target = line
            if method != "CONNECT":
                self._reply(405, "Method Not Allowed")
                return
            if ":" not in target:
                self._reply(400, "Bad Request")
                return
            host, _, port_s = target.rpartition(":")
            host = host.strip("[]")          # IPv6 字面量带方括号
            try:
                port = int(port_s)
            except ValueError:
                self._reply(400, "Bad Request")
                return

            upstream = connect(self.proxy_addr[0], self.proxy_addr[1],
                               host, port, remote_dns=self.remote_dns,
                               timeout=self.timeout)
            self._reply(200, "Connection established")
        except SocksError as e:
            # 把 SOCKS 侧的失败原样反馈给客户端，便于用户看到根因
            self._reply(502, "Bad Gateway: %s" % e)
            return
        except OSError:
            return
        finally:
            if upstream is None and self.client is not None:
                try:
                    self.client.close()
                except OSError:
                    pass

        def close_both():
            for s in (upstream, self.client):
                try:
                    s.close()
                except OSError:
                    pass

        t = threading.Thread(target=_pipe, args=(self.client, upstream, close_both),
                             daemon=True)
        t.start()
        _pipe(upstream, self.client, close_both)

    def _read_request(self):
        """读一行请求行；返回 (method, target) 或 None。"""
        buf = b""
        while b"\r\n" not in buf:
            try:
                chunk = self.client.recv(1)
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
            if len(buf) > 8192:
                return None
        parts = buf.split(b"\r\n")[0].split(b" ")
        if len(parts) < 2:
            return None
        try:
            return parts[0].decode("ascii").upper(), parts[1].decode("ascii")
        except UnicodeDecodeError:
            return None

    def _reply(self, code, text):
        try:
            self.client.sendall(("HTTP/1.1 %d %s\r\n\r\n" % (code, text)).encode("ascii", "replace"))
        except OSError:
            pass


class SocksRelayServer(object):
    """本地回环 http 代理，把 CONNECT 转发进 SOCKS5 隧道。

    用法：
        srv = SocksRelayServer("127.0.0.1", 7891, remote_dns=True)
        port = srv.start()          # 拿到本地端口
        proxy_url = "http://127.0.0.1:%d" % port   # 交给 urllib
        ...
        srv.stop()
    """

    def __init__(self, proxy_host, proxy_port, remote_dns=False, timeout=DEFAULT_TIMEOUT):
        self.proxy_addr = (proxy_host, int(proxy_port))
        self.remote_dns = remote_dns
        self.timeout = timeout
        self._srv = None
        self._thread = None
        self.port = 0

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))     # 端口交给内核分配
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def _serve(self):
        while True:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return                      # stop() 关掉监听即退出
            threading.Thread(
                target=_Relay(client, self.proxy_addr, self.remote_dns,
                              self.timeout).run,
                daemon=True,
            ).start()

    def stop(self):
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None
