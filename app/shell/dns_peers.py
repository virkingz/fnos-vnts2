#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出站互联域名 → IP 自动同步。

## 为什么需要这个模块

vnts2 的 `peer_servers` 只接受**字面 IP:端口**，域名会在这一行直接失败：

    let addr: SocketAddr = peer_addr.parse()?;      // peer_server.rs
    let connection = endpoint.connect(addr, "localhost")?.await?;

`SocketAddr::from_str` 不认域名，所以 `peer1.example.com:29873` 抛 Err，
且它会每 3~10 秒无限重试，日志一直刷错误。配置里写域名 = 永远连不上。

## 怎么办

绕开配置，改走控制台自身的 API（增删即时生效，无需重启核心）：

    POST   /api/peer_servers           {"server_addr": "1.2.3.4:29873"}
    GET    /api/peer_servers           -> {outbound: [...], inbound: [...]}
    DELETE /api/peer_servers/{addr}

核心启动时会 `load_all_peer_servers()` 从 SQLite 把记录全部拉起
（`src/main.rs:127`），所以经 API 写入的记录**同样能持久化并自动重连**，
效果与写在配置里完全等价 —— 唯一区别是 config.toml 里不出现域名。

本模块负责：定时解析域名 → 与核心实际列表比对 → 先增新、后删旧 → 落盘记录。

## 两条不可动摇的安全规则

1. **先加后删**。反序会出现「旧的已删、新的未加」→ 互联直接断掉。
2. **只删自己名下的地址**。核心列表里不认识的行（用户在控制台手工加的）
   一律不碰；删除前用 `current` 字符串**原样比对**，因为核心
   `remove_outbound_peer` 是按精确字符串查 `outbound_tasks` 的。

仅使用 Python 标准库，兼容 Python 3.8+。
"""

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_INTERVAL = 60
MIN_INTERVAL = 10
MAX_INTERVAL = 86400

# 形如 1.2.3.4:29873 / [2001:db8::1]:29873 / ::1:29873
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


# ---------------------------------------------------------------------------
# 地址工具
# ---------------------------------------------------------------------------
def split_host_port(text):
    """把 "host:port" 拆成 (host, port)。支持 IPv6 的 [::1]:29873。

    返回 (host, port, error)，error 非空表示格式非法。
    """
    s = (text or "").strip()
    if not s:
        return "", 0, "地址不能为空"
    if s.startswith("["):
        # [IPv6]:port
        i = s.find("]")
        if i < 0:
            return "", 0, "IPv6 地址缺少右方括号，应写作 [::1]:29873"
        host = s[1:i]
        rest = s[i + 1:]
        if not rest.startswith(":"):
            return "", 0, "IPv6 地址后需接端口，应写作 [::1]:29873"
        port_s = rest[1:]
    else:
        # 含多个冒号且无方括号 → 裸 IPv6，无法确定端口位置
        if s.count(":") > 1:
            return "", 0, "IPv6 地址需用方括号包裹，如 [2001:db8::1]:29873"
        if ":" not in s:
            return "", 0, "缺少端口，应写作 host:port"
        host, port_s = s.rsplit(":", 1)
    host = host.strip()
    if not host:
        return "", 0, "主机名不能为空"
    try:
        port = int(port_s.strip())
    except ValueError:
        return "", 0, "端口需为数字"
    if not (1 <= port <= 65535):
        return "", 0, "端口范围 1-65535"
    return host, port, ""


def is_ip_literal(host):
    """判断是否已是字面 IP（v4 或 v6），字面 IP 无需解析。"""
    h = (host or "").strip()
    if not h:
        return False
    if _IPV4_RE.match(h):
        return True
    try:
        socket.inet_pton(socket.AF_INET6, h)
        return True
    except (OSError, AttributeError, ValueError):
        return False


def parse_addr(addr):
    """解析 "IP:port"（含 IPv6 方括号形态）→ (host, port)；失败返回 (None, None)。"""
    s = (addr or "").strip()
    if not s:
        return None, None
    if s.startswith("["):
        i = s.find("]")
        if i < 0:
            return None, None
        host, rest = s[1:i], s[i + 1:]
        if not rest.startswith(":"):
            return None, None
        try:
            return host, int(rest[1:])
        except ValueError:
            return None, None
    if s.count(":") != 1:
        return None, None
    host, port_s = s.rsplit(":", 1)
    try:
        return host, int(port_s)
    except ValueError:
        return None, None


def format_addr(ip, port):
    """组装核心接受的地址串。IPv6 用方括号包裹（SocketAddr 的 Display 形态）。"""
    ip = (ip or "").strip()
    if ":" in ip and not ip.startswith("["):
        return "[%s]:%d" % (ip, int(port))
    return "%s:%d" % (ip, int(port))


def resolve_first(host, timeout=5, _getaddrinfo=None):
    """解析域名，只取第一个结果。返回 (ip, error)。

    - 字面 IP 直接返回，不做 DNS 查询
    - 多个 A/AAAA 记录只取第一个（多 IP 会让互联列表变复杂，且核心按
      精确字符串管理，同一域名出现两条记录反而难维护）
    - 失败返回 ("", 原因)，调用方必须**保留旧地址不动**
    """
    h = (host or "").strip()
    if not h:
        return "", "主机名为空"
    if is_ip_literal(h):
        return h, ""
    gai = _getaddrinfo or socket.getaddrinfo
    try:
        infos = gai(h, None, socket.AF_UNSPEC, socket.SOCK_DGRAM)
    except socket.gaierror as e:
        return "", "DNS 解析失败: %s" % (e,)
    except OSError as e:
        return "", "DNS 查询出错: %s" % (e,)
    if not infos:
        return "", "DNS 无解析结果"
    try:
        return infos[0][4][0], ""
    except (IndexError, TypeError):
        return "", "DNS 结果格式异常"


# ---------------------------------------------------------------------------
# dns-ip.json 读写
# ---------------------------------------------------------------------------
def default_doc():
    return {"version": 1, "interval": DEFAULT_INTERVAL, "records": [], "managed": []}


def new_record(host, port):
    return {
        "host": host,
        "port": int(port),
        "current": "",       # 核心列表里实际存在的那条（删除时原样比对）
        "previous": "",      # 仅供界面显示「上一跳」
        "last_sync": 0,      # 最近一次成功同步的时间戳
        "last_ok": 0,        # 最近一次同步动作成功的时间戳
        "last_error": "",
    }


def normalize_interval(v):
    """校验间隔，返回 (值, 错误)。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL, "轮询间隔需为整数（秒）"
    if n < MIN_INTERVAL:
        return DEFAULT_INTERVAL, "轮询间隔至少 %d 秒" % MIN_INTERVAL
    if n > MAX_INTERVAL:
        return DEFAULT_INTERVAL, "轮询间隔最多 %d 秒" % MAX_INTERVAL
    return n, ""


def load_doc(path):
    """读 dns-ip.json。文件缺失/损坏都退回默认值，绝不抛异常。"""
    doc = default_doc()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return doc
    if not isinstance(raw, dict):
        return doc
    n, err = normalize_interval(raw.get("interval", DEFAULT_INTERVAL))
    doc["interval"] = n if not err else DEFAULT_INTERVAL
    recs = raw.get("records")
    if isinstance(recs, list):
        out = []
        for r in recs:
            if not isinstance(r, dict):
                continue
            host = str(r.get("host") or "").strip()
            if not host:
                continue
            try:
                port = int(r.get("port"))
            except (TypeError, ValueError):
                continue
            rec = new_record(host, port)
            for k in ("current", "previous", "last_error"):
                v = r.get(k)
                if isinstance(v, str):
                    rec[k] = v.strip()
            for k in ("last_sync", "last_ok"):
                try:
                    rec[k] = int(r.get(k) or 0)
                except (TypeError, ValueError):
                    rec[k] = 0
            out.append(rec)
        doc["records"] = out
    # managed：曾经由我们写进核心的出站地址集合。
    #
    # 这是「清单被清空后仍能精确清理」的依据 —— 清空 records 会丢失
    # 「哪些地址归我们管」的信息，若只靠 records 推断，就只能退化成
    # 「删除所有出站地址」，那会误伤用户在控制台手工添加的互联。
    # 有了它，从有到无时也能只删自己加过的那几条。
    mgd = raw.get("managed")
    if isinstance(mgd, list):
        doc["managed"] = [str(x).strip() for x in mgd if str(x).strip()]
    return doc


def save_doc(path, doc):
    """原子写 dns-ip.json。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def records_of(doc):
    return doc.get("records") or []


def desired_addr(rec, _getaddrinfo=None):
    """算出该记录「应该」在核心里的地址。返回 (addr, error)。

    字面 IP 直接成形；域名走 DNS 取第一个。
    解析失败时返回错误，**旧地址必须原样保留**。
    """
    host = rec.get("host") or ""
    try:
        port = int(rec.get("port"))
    except (TypeError, ValueError):
        return "", "端口无效"
    ip, err = resolve_first(host, _getaddrinfo=_getaddrinfo)
    if err:
        return "", err
    return format_addr(ip, port), ""


def hosts_of(doc):
    """把文档摊平成 ["host:port", ...]，供界面与写入使用。"""
    out = []
    for r in records_of(doc):
        out.append(format_addr(r.get("host", ""), r.get("port", 0)))
    return out


def parse_lines(text):
    """解析界面来的多行文本（或逗号分隔）→ (records, errors)。

    容忍注释行（# 开头）与空行。
    """
    recs, errs = [], []
    if isinstance(text, (list, tuple)):
        lines = list(text)
    else:
        lines = str(text or "").replace(",", "\n").splitlines()
    seen = set()
    for ln in lines:
        s = str(ln).strip()
        if not s or s.startswith("#"):
            continue
        host, port, err = split_host_port(s)
        if err:
            errs.append("%s —— %s" % (s, err))
            continue
        key = (host.lower(), port)
        if key in seen:
            continue
        seen.add(key)
        recs.append(new_record(host, port))
    return recs, errs


def merge_records(old_recs, new_recs):
    """用新清单覆盖旧清单，但**保留同名域的解析状态**。

    这样用户只是调整顺序或改动其他域名时，已生效的 current 不会丢，
    避免下一轮无谓地删了又加。
    """
    by_key = {}
    for r in old_recs or []:
        by_key[(str(r.get("host", "")).lower(), int(r.get("port", 0) or 0))] = r
    out = []
    for r in new_recs:
        key = (str(r.get("host", "")).lower(), int(r.get("port", 0) or 0))
        old = by_key.get(key)
        if old:
            merged = new_record(r["host"], r["port"])
            for k in ("current", "previous", "last_sync", "last_ok", "last_error"):
                merged[k] = old.get(k, merged[k])
            out.append(merged)
        else:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# 控制台 API 客户端
# ---------------------------------------------------------------------------
class ConsoleClient(object):
    """vnts2 控制台 API 的极简客户端。

    鉴权：POST /api/login {username,password} -> JWT。
    JWT 有效期 1 天，且**密钥每次核心进程启动都随机生成**（web_server.rs
    里 rand 生成 jwt_secret），所以令牌只在内存缓存，绝不留盘；
    收到 401 就重新登录一次再重试。
    """

    def __init__(self, base, username, password, _open=None, timeout=10):
        self.base = (base or "").rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token = ""
        self._open = _open or self._default_open

    def _default_open(self, url, data=None, headers=None, method="GET"):
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return urllib.request.urlopen(req, timeout=self.timeout)

    def _call(self, method, path, payload=None, retry=True):
        """返回 (code, body_str, error)。code 为 HTTP 状态码，0 表示网络错误。"""
        url = self.base + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        try:
            resp = self._open(url, data=data, headers=headers, method=method)
            code = getattr(resp, "status", 200)
            body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code = e.code
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
        except Exception as e:  # noqa: BLE001
            return 0, "", "无法连接控制台 API: %s" % (e,)

        if code == 401 and retry and path != "/api/login":
            ok, err = self.login()
            if not ok:
                return code, body, err
            return self._call(method, path, payload, retry=False)
        return code, body, ""

    def login(self):
        code, body, err = self._call(
            "POST", "/api/login",
            {"username": self.username, "password": self.password}, retry=False,
        )
        if err:
            return False, err
        if code != 200:
            return False, "控制台登录失败（HTTP %s），请检查用户名密码" % code
        try:
            data = json.loads(body).get("data") or {}
            self._token = data.get("token") or ""
        except ValueError:
            return False, "控制台登录响应异常"
        if not self._token:
            return False, "控制台未返回令牌"
        return True, ""

    def list_peers(self):
        """返回 (outbound_addrs, all_addrs, error)。

        outbound 是核心主动外连的地址（我们要管的）；
        all 含 inbound，用于判断「某地址是否已被占用」，避免重复添加。
        """
        code, body, err = self._call("GET", "/api/peer_servers")
        if err:
            return [], [], err
        if code != 200:
            return [], [], "查询互联列表失败（HTTP %s）" % code
        try:
            data = json.loads(body).get("data") or {}
        except ValueError:
            return [], [], "互联列表响应异常"
        out, alladdr = [], []
        for item in data.get("outbound") or []:
            a = str((item or {}).get("addr") or "").strip()
            if a:
                out.append(a)
                alladdr.append(a)
        for item in data.get("inbound") or []:
            a = str((item or {}).get("addr") or "").strip()
            if a:
                alladdr.append(a)
        return out, alladdr, ""

    def add_peer(self, addr):
        code, body, err = self._call("POST", "/api/peer_servers", {"server_addr": addr})
        if err:
            return False, err
        if code != 200:
            return False, "添加 %s 失败（HTTP %s）" % (addr, code)
        try:
            j = json.loads(body)
        except ValueError:
            return False, "添加 %s 响应异常" % addr
        if j.get("code") != 200:
            return False, "添加 %s 失败：%s" % (addr, j.get("msg") or "未知错误")
        return True, ""

    def del_peer(self, addr):
        # 路径段需转义：IPv6 的 [ ] 与 : 在 URL 里必须百分号编码
        seg = urllib.parse.quote(addr, safe="")
        code, body, err = self._call("DELETE", "/api/peer_servers/" + seg)
        if err:
            return False, err
        if code != 200:
            return False, "删除 %s 失败（HTTP %s）" % (addr, code)
        try:
            j = json.loads(body)
        except ValueError:
            return False, "删除 %s 响应异常" % addr
        if j.get("code") != 200:
            return False, "删除 %s 失败：%s" % (addr, j.get("msg") or "未知错误")
        return True, ""


# ---------------------------------------------------------------------------
# 单轮同步（本模块的核心，全部纯度可测）
# ---------------------------------------------------------------------------
def sync_once(doc, client, now=None, _getaddrinfo=None, log=None):
    """按配置清单与核心出站列表做一轮**完全对账**，原地修改 doc。

    返回 actions 列表（供界面与日志展示）。每项形如：
        {"host":.., "action":"add"|"del"|"keep"|"error", "addr":.., "msg":..}

    对账口径：**以 dns-ip.json 清单为唯一真源**。核心出站列表里凡是
    不在清单中的地址，一律删除 —— 包括：
      - 我们上一次同步留下的旧 IP；
      - 先加新、尚未来得及删旧就重启核心而残留的多余记录；
      - 在控制台手工添加的地址。

    这样做的好处是「幂等收敛」：无论中间被打断多少次，最终状态一定
    等于清单所要求的状态，不会越积越多。

    分两阶段，顺序不可颠倒：

      阶段一（补齐 + 换旧）：逐条解析域名 → 确保目标地址存在 → 删掉该条的旧地址。
        解析失败时**该条原地不动**（保留旧地址），避免一次 DNS 抖动断掉互联。
      阶段二（清理残留）：删除出站列表里所有不属于清单的地址。
        放在最后是为了不误删「本轮刚加的新地址还没确认就清理」。

    **清单为空 = 撤下全部托管地址**（从有到无）：
    不再早退，而是删除 doc["managed"] 里记录的、我们曾加进去的地址。
    不能删「所有出站地址」—— 那会误伤用户在控制台手工添加的互联。

    安全约束：只处理 outbound（核心主动外连的）地址，绝不碰 inbound
    （别的服务端连进来的），那些不是我们的管辖范围。
    """
    now = int(now if now is not None else time.time())
    actions = []

    def _log(msg):
        if log:
            log(msg)

    records = records_of(doc)
    managed = [str(x).strip() for x in (doc.get("managed") or []) if str(x).strip()]

    out_addrs, all_addrs, err = client.list_peers()
    if err:
        for rec in records:
            rec["last_error"] = err
            actions.append({"host": rec.get("host"), "action": "error",
                            "addr": "", "msg": err})
        return actions

    # ---- 清单为空：撤下所有托管地址（从有到无的关键路径）----
    if not records:
        for addr in managed:
            if addr not in out_addrs:
                continue
            ok, derr = client.del_peer(addr)
            if ok:
                out_addrs.remove(addr)
                actions.append({"host": "", "action": "del", "addr": addr,
                                "msg": "清单已清空，撤下托管地址"})
                _log("清单已清空，撤下托管地址：%s" % addr)
            else:
                actions.append({"host": "", "action": "error", "addr": addr,
                                "msg": derr})
                _log("撤下托管地址 %s 失败：%s" % (addr, derr))
        # 全部撤下成功才清空 managed；有失败则保留，下轮继续重试
        failed = [a for a in actions if a.get("action") == "error"]
        doc["managed"] = [] if not failed else managed
        doc["records"] = []
        return actions

    # 清单要求的目标地址集合。
    #
    # 清理阶段的安全性依据：它只删「不在 wanted 里的出站地址」，而每个我们
    # 加过的地址必然属于下列之一，因而都受保护：
    #   - 解析成功 → 目标地址进 wanted，旧地址由阶段一亲自删除；
    #   - 解析失败 → 其 current 进 wanted，不会因一次 DNS 抖动被清掉。
    # 所以即便存在永久解析失败的条目，清理也只影响真正的残留记录。
    wanted = set()
    resolved = {}          # id(rec) -> (want, cur)
    for rec in records:
        host = rec.get("host") or ""
        want, rerr = desired_addr(rec, _getaddrinfo=_getaddrinfo)
        cur = (rec.get("current") or "").strip()
        if rerr:
            if cur:
                wanted.add(cur)      # 解析失败：保留旧地址，且不视为残留
            continue
        wanted.add(want)
        resolved[id(rec)] = (want, cur)

    # ---- 阶段一：补齐目标地址，并删掉该条的旧地址 ----
    for rec in records:
        host = rec.get("host") or ""
        pair = resolved.get(id(rec))
        if pair is None:
            rerr = desired_addr(rec, _getaddrinfo=_getaddrinfo)[1]
            rec["last_error"] = rerr
            actions.append({"host": host, "action": "error",
                            "addr": rec.get("current") or "", "msg": rerr})
            _log("域名解析失败，保留原地址：%s —— %s" % (host, rerr))
            continue

        want, cur = pair

        # 判定基准是「核心实际列表」，而非记录里的 current。
        # 这样即使记录被人在控制台删掉，也能自动补回（自愈）。
        if want not in out_addrs:
            ok, aerr = client.add_peer(want)
            if not ok:
                # 加失败 → 绝不删旧地址，否则互联会断
                rec["last_error"] = aerr
                actions.append({"host": host, "action": "error",
                                "addr": cur, "msg": aerr})
                _log("添加 %s 失败，保留原地址 %s：%s" % (want, cur or "(无)", aerr))
                continue
            out_addrs.append(want)
            actions.append({"host": host, "action": "add", "addr": want, "msg": ""})
            _log("已添加互联地址：%s（%s）" % (want, host))
        elif cur == want:
            # 地址未变、且确实在列表里 → 无需任何写操作
            rec["last_error"] = ""
            rec["last_sync"] = now
            rec["last_ok"] = now
            actions.append({"host": host, "action": "keep", "addr": want, "msg": ""})
            continue

        # 新地址就位后，再删自己名下的旧地址
        if cur and cur != want and cur in out_addrs:
            ok, derr = client.del_peer(cur)
            if ok:
                out_addrs.remove(cur)
                rec["previous"] = cur
                actions.append({"host": host, "action": "del", "addr": cur, "msg": ""})
                _log("已移除旧互联地址：%s（%s）" % (cur, host))
            else:
                # 删除失败不算致命：新地址已在，阶段二/下一轮会再试
                rec["last_error"] = derr
                actions.append({"host": host, "action": "error",
                                "addr": cur, "msg": derr})
                _log("移除旧地址 %s 失败（新地址已生效）：%s" % (cur, derr))

        rec["current"] = want
        rec["last_sync"] = now
        rec["last_ok"] = now
        rec["last_error"] = ""

    # ---- 阶段二：清理出站列表里所有不属于清单的残留 ----
    # 覆盖「先加后删中途重启」留下的多余记录，以及在控制台手工加的地址。
    #
    # protected 同时收纳「每条记录的 current」：阶段一在添加失败时会跳过
    # 删除旧地址，若这里不保护它，清理就会把旧地址删掉 —— 那正是
    # 「加失败绝不删旧」要防的事故。真正的残留（例如上一轮已把 current
    # 更新为新 IP 后遗留的旧 IP）不在任何记录的 current 里，仍会被清掉。
    protected = set(wanted)
    for rec in records:
        c = (rec.get("current") or "").strip()
        if c:
            protected.add(c)

    for addr in list(out_addrs):
        if addr in protected:
            continue
        ok, derr = client.del_peer(addr)
        if ok:
            out_addrs.remove(addr)
            actions.append({"host": "", "action": "del", "addr": addr,
                            "msg": "不在配置清单中，已清理"})
            _log("清理多余互联地址：%s（不在配置清单中）" % addr)
        else:
            actions.append({"host": "", "action": "error", "addr": addr,
                            "msg": derr})
            _log("清理多余地址 %s 失败：%s" % (addr, derr))

    # 重建 managed：清单要求的目标地址，加上仍受保护的历史托管地址
    # （解析失败的条目、以及删除失败待重试的残留）。
    # 这样「从有到无」时能凭它精确撤下我们加过的地址。
    keep_managed = set(wanted)
    keep_managed.update(protected)
    if not actions or all(a.get("action") != "error" for a in actions):
        # 本轮无异常 → 托管集合就是清单目标，丢掉一切历史痕迹
        doc["managed"] = sorted(wanted)
    else:
        # 有失败项 → 保留其地址，下一轮继续重试
        doc["managed"] = sorted(keep_managed)

    return actions


def status_view(doc):
    """把文档整理成界面要的形态（只读，不改文档）。"""
    items = []
    for r in records_of(doc):
        items.append({
            "host": r.get("host", ""),
            "port": r.get("port", 0),
            "addr": format_addr(r.get("host", ""), r.get("port", 0)),
            "current": r.get("current", ""),
            "previous": r.get("previous", ""),
            "last_sync": r.get("last_sync", 0),
            "last_ok": r.get("last_ok", 0),
            "last_error": r.get("last_error", ""),
        })
    managed = [str(x).strip() for x in (doc.get("managed") or []) if str(x).strip()]
    return {
        "interval": doc.get("interval", DEFAULT_INTERVAL),
        "records": items,
        "count": len(items),
        # enabled 表示「是否在托管中」：还有清单、或还有待撤下的托管地址。
        # 界面据它判断要不要提示「正在撤下残留」。
        "enabled": bool(items) or bool(managed),
        "managed": managed,
        "pending_cleanup": bool(managed) and not items,
    }


# ---------------------------------------------------------------------------
# CLI（供 shell_server 探测与人工排查用）
# ---------------------------------------------------------------------------
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="dns_peers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="解析一个域名，打印第一个 IP")
    r.add_argument("addr", help="host:port 或 [ipv6]:port")

    lp = sub.add_parser("list", help="打印 dns-ip.json 内容")
    lp.add_argument("--file", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "resolve":
        host, port, err = split_host_port(args.addr)
        if err:
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
            return 2
        ip, rerr = resolve_first(host)
        if rerr:
            print(json.dumps({"ok": False, "error": rerr}, ensure_ascii=False))
            return 2
        print(json.dumps({"ok": True, "host": host, "port": port,
                          "ip": ip, "addr": format_addr(ip, port)},
                         ensure_ascii=False))
        return 0

    if args.cmd == "list":
        doc = load_doc(args.file)
        print(json.dumps(status_view(doc), ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

