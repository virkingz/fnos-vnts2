#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vnts-nas 管理页后端。

职责：
  1. 托管管理页 UI（服务端设置、核心版本与更新、日志、导入导出）。
  2. 托管 vnts2 服务端进程：启动、停止、健康检查、崩溃拉起。
  3. 从 GitHub Releases 检查并下载/替换核心二进制（裸二进制）。
  4. 服务器配置导入导出（config.toml + network_control.db，不含日志与二进制）。

仅使用 Python 标准库，兼容 Python 3.8+。
"""

import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dns_peers as DP  # noqa: E402
import vnts_config as C  # noqa: E402

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
APPDEST = os.environ.get("VNT_APPDEST", "/var/apps/vnts-nas/target")
PKGVAR = os.environ.get("VNT_PKGVAR", "/var/apps/vnts-nas/var")
PKGETC = os.environ.get("VNT_PKGETC", "/var/apps/vnts-nas/etc")
APPNAME = os.environ.get("VNT_APPNAME", "vnts-nas")

BIN_DIR = os.path.join(PKGVAR, "bin")
SERVER_DIR = os.path.join(PKGVAR, "server")
CACHE_DIR = os.path.join(PKGVAR, "cache")
PORT_FILE = os.path.join(PKGVAR, "ports.env")
LOGDIR_FILE = os.path.join(PKGVAR, "logdir")
# 核心日志级别与轮转设置（级别 / 单文件 KB / 保留份数）
LOG_CONF_FILE = os.path.join(PKGVAR, "logconf.json")
# 三协议的独立端口。留空（不写或写空）= 复用 SERVICE_PORT。
#
# 为什么要单独存而不能从 config.toml 反推：config.toml 里只有一个
# 展开后的端口数字，无法区分「用户显式填了 29872」与「留空所以复用了
# 29872」。一旦用户改了 SERVICE_PORT，前者不该跟随、后者必须跟随 ——
# 反推必然出错（曾因此出现「改了服务端口，tcp_bind 却不跟随」的 bug）。
PROTO_PORTS_FILE = os.path.join(SERVER_DIR, "proto-ports.json")
MIRROR_FILE = os.path.join(SERVER_DIR, "mirror")
PROXY_FILE = os.path.join(SERVER_DIR, "proxy")
# 出站互联域名清单 + 解析结果（与 mirror/proxy 同级，导出包自动带上）
DNS_PEERS_FILE = os.path.join(SERVER_DIR, "dns-ip.json")
SOCK_PATH = os.path.join(APPDEST, "app.sock")

CORE_BIN = os.path.join(BIN_DIR, "vnts2")
CORE_VER_FILE = os.path.join(BIN_DIR, ".vnts_version")
CORE_SRC_FILE = os.path.join(BIN_DIR, ".vnts_source")
PANEL_TOKEN_FILE = os.path.join(BIN_DIR, "panel_token")
CORE_PID_FILE = os.path.join(PKGVAR, "core.pid")

CONFIG_TOML = os.path.join(SERVER_DIR, "config.toml")
SERVER_DB = os.path.join(SERVER_DIR, "network_control.db")

DEFAULTS = {
    "PANEL_PORT": "19098",
    "SERVICE_PORT": "29872",
    "CONSOLE_PORT": "29071",
}

# ---------------------------------------------------------------------------
# 日志目录（独立于配置）
# ---------------------------------------------------------------------------
def log_dir():
    """日志目录：LOG_DIR 环境变量 > logdir 文件 > 数据目录/log。"""
    v = (os.environ.get("LOG_DIR") or "").strip()
    if v:
        return v
    if os.path.isfile(LOGDIR_FILE):
        try:
            v = open(LOGDIR_FILE, encoding="utf-8").read().strip().splitlines()[0].strip()
            if v:
                return v
        except (OSError, IndexError):
            pass
    return os.path.join(PKGVAR, "log")


# ---------------------------------------------------------------------------
# 飞牛统一网关挂载前缀
# 经桌面图标访问时 URL 形如 /app/vnts-nas/...，需在入口剥掉前缀，
# 前端再据 location.pathname 拼回前缀（见 web/app.js）。直连时前缀为空。
# ---------------------------------------------------------------------------
GATEWAY_PREFIX = "/app/%s" % APPNAME


def remove_gateway_prefix(path):
    if path == GATEWAY_PREFIX:
        return "/"
    if path.startswith(GATEWAY_PREFIX + "/"):
        return path[len(GATEWAY_PREFIX):]
    return path


def detect_path_prefix(path):
    """返回当前请求所处的挂载前缀（'' 表示根路径直连）。"""
    if path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/"):
        return GATEWAY_PREFIX
    return ""


def save_log_dir(d):
    d = (d or "").strip()
    if not d:
        _rm(LOGDIR_FILE)
    else:
        os.makedirs(os.path.dirname(LOGDIR_FILE), exist_ok=True)
        with open(LOGDIR_FILE, "w", encoding="utf-8") as f:
            f.write(d)
    # 同步更新 log4rs.yaml，让 vnts2 立刻写到新目录
    render_config()


# ---------------------------------------------------------------------------
# 核心日志级别与轮转（写进 logs/log4rs.yaml，由 vnts2 自己读取）
#
# vnts2 用 log4rs::init_file() 读 CWD/logs/log4rs.yaml，其 root.level 决定
# 输出哪些日志。yaml 里 refresh_rate 为 30 秒，所以改完约 30 秒自动生效，
# **不需要重启核心** —— 这点对「临时开 debug 排查」很关键。
# ---------------------------------------------------------------------------
LOG_LEVELS = ("trace", "debug", "info", "warn", "error")
# 单文件上限以 **KB** 为单位存储，这样 512KB 这类小档位能精确表达。
# 界面按 KB 收，渲染时能被 1024 整除则写成 mb（更可读），否则写 kb。
LOG_KB_MIN, LOG_KB_MAX = 64, 1024 * 1024          # 64KB ~ 1GB
LOG_KEEP_MIN, LOG_KEEP_MAX = 1, 100
DEFAULT_LOG_KB = 10240                             # 默认 10MB


def load_log_conf():
    """读日志配置。缺失或损坏时回到默认（info / 10MB / 5 份）。

    兼容两种历史键：
      - max_mb（上一版，整 MB）
      - max_kb（当前，精确到 KB）
    两者都接受，统一归一化到 max_kb。
    """
    conf = {"level": "info", "max_kb": DEFAULT_LOG_KB, "keep": 5}
    raw = read_file(LOG_CONF_FILE).strip()
    if not raw:
        return conf
    try:
        data = json.loads(raw)
    except ValueError:
        return conf
    if not isinstance(data, dict):
        return conf
    lv = str(data.get("level") or "").lower()
    if lv in LOG_LEVELS:
        conf["level"] = lv
    # 优先读 max_kb；没有则用 max_mb 换算（老配置）
    kb = None
    if data.get("max_kb") is not None:
        kb = data.get("max_kb")
    elif data.get("max_mb") is not None:
        try:
            kb = int(data.get("max_mb")) * 1024
        except (TypeError, ValueError):
            kb = None
    if kb is not None:
        try:
            n = int(kb)
        except (TypeError, ValueError):
            n = None
        if n is not None and LOG_KB_MIN <= n <= LOG_KB_MAX:
            conf["max_kb"] = n
    try:
        n = int(data.get("keep"))
    except (TypeError, ValueError):
        n = None
    if n is not None and LOG_KEEP_MIN <= n <= LOG_KEEP_MAX:
        conf["keep"] = n
    return conf


def normalize_log_conf(level, max_kb, keep):
    """校验日志配置。返回 (配置, 错误列表)。只校验传进来的字段。

    max_kb 若为浮点或带小数，四舍五入到整数 KB —— 界面上用 MB 输入时
    可能是 0.5 这种值。
    """
    conf = load_log_conf()
    errs = []
    if level is not None:
        lv = str(level).strip().lower()
        if lv not in LOG_LEVELS:
            errs.append("日志级别只支持 %s" % " / ".join(LOG_LEVELS))
        else:
            conf["level"] = lv
    if max_kb is not None and str(max_kb).strip() != "":
        try:
            n = int(round(float(max_kb)))
        except (TypeError, ValueError):
            errs.append("文件大小上限需为数字（KB）")
        else:
            if not (LOG_KB_MIN <= n <= LOG_KB_MAX):
                errs.append("文件大小上限需在 %d KB - %d KB 之间"
                            % (LOG_KB_MIN, LOG_KB_MAX))
            else:
                conf["max_kb"] = n
    if keep is not None and str(keep).strip() != "":
        try:
            n = int(keep)
        except (TypeError, ValueError):
            errs.append("保留份数需为整数")
        else:
            if not (LOG_KEEP_MIN <= n <= LOG_KEEP_MAX):
                errs.append("保留份数需在 %d-%d 之间" % (LOG_KEEP_MIN, LOG_KEEP_MAX))
            else:
                conf["keep"] = n
    return conf, errs


def save_log_conf(conf):
    """保存日志配置并立刻重渲染 log4rs.yaml。

    refresh_rate 为 30 秒，核心会自动重读，无需重启。
    """
    write_file(LOG_CONF_FILE, json.dumps(conf, ensure_ascii=False, indent=2) + "\n")
    render_config()
    return conf


def log_conf_view():
    conf = load_log_conf()
    conf["levels"] = list(LOG_LEVELS)
    conf["max_kb_range"] = [LOG_KB_MIN, LOG_KB_MAX]
    conf["keep_range"] = [LOG_KEEP_MIN, LOG_KEEP_MAX]
    # 便于界面直接显示
    conf["max_mb"] = round(conf["max_kb"] / 1024.0, 2)
    # 常用档位，界面做成下拉/快捷项
    conf["presets_kb"] = [512, 1024, 5120, 10240, 51200, 102400]
    # 核心是否在跑，决定「改完是否立即生效」的提示
    conf["core_running"] = bool(core_running())
    return conf


def main_log():
    return os.path.join(log_dir(), "main.log")


def shell_log():
    return os.path.join(log_dir(), "shell.log")


def core_log():
    return os.path.join(log_dir(), "vnts2.log")


def update_log():
    return os.path.join(log_dir(), "update.log")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def ensure_dirs():
    for d in (BIN_DIR, SERVER_DIR, CACHE_DIR, log_dir()):
        os.makedirs(d, exist_ok=True)


def heal_legacy_conf():
    """自愈历史误配：把「无端口的 http(s) 地址」从代理挪到镜像前缀。

    这类地址（如 https://ghproxy.example.com）是加速前缀，当代理用必然失败，
    会让所有下载与连通性测试都超时。启动时纠正一次，避免用户被困在错配置里。
    注意：此函数不能调用 log()，log() 会反过来调用 ensure_dirs()。
    """
    try:
        proxy = read_file(PROXY_FILE).strip()
        mirror = read_file(MIRROR_FILE).strip().rstrip("/")
        if not proxy or not _MIRROR_LIKE.match(proxy):
            return False
        write_file(PROXY_FILE, "")
        msg = "已自动把代理栏里的 %s 移到镜像前缀" % proxy
        if not mirror:
            write_file(MIRROR_FILE, proxy.rstrip("/"))
        else:
            msg += "（原镜像前缀 %s 保留，未覆盖）" % mirror
        try:
            with open(main_log(), "a", encoding="utf-8") as fh:
                fh.write("%s [shell] %s\n" % (now(), msg))
        except OSError:
            pass
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 出站互联域名同步（dns-ip.json 是唯一真源）
#
# vnts2 的 peer_servers 只认字面 IP:port，写域名必然连不上（见 dns_peers.py
# 顶部说明）。因此 config.toml 里 peer_servers 恒为空，出站互联改为：
#   管理页定时解析域名 → 经控制台 API 增删 → 记录写回 dns-ip.json
# 增删即时生效，无需重启核心。
# ---------------------------------------------------------------------------
_dns_lock = threading.RLock()
_dns_stop = threading.Event()


def load_dns_doc():
    """读 dns-ip.json（缺失/损坏均返回默认值）。"""
    return DP.load_doc(DNS_PEERS_FILE)


def save_dns_doc(doc):
    DP.save_doc(DNS_PEERS_FILE, doc)


def dns_peers_status():
    """界面用：清单 + 每条记录的解析状态。"""
    return DP.status_view(load_dns_doc())


def _console_base():
    """控制台 API 的本地入口（仅回环，由本壳反代对外）。"""
    _, console_port = C.read_ports(PORT_FILE)
    return "http://127.0.0.1:%d" % console_port


def _console_client():
    """按当前 config.toml 里的管理员账号构造控制台客户端。"""
    s = C.load_settings(SERVER_DIR)
    return DP.ConsoleClient(
        _console_base(),
        str(s.get("username") or "admin"),
        str(s.get("password") or "admin"),
    )


def sync_dns_peers(once=False, log_prefix="域名同步"):
    """执行一轮域名 → IP 同步。

    返回 {"ok":bool, "actions":[...], "error":str}。

    **清单为空不等于什么都不做**：若此前托管过地址（doc["managed"] 非空），
    本轮负责把它们从核心撤下 —— 这是「从有到无」必须生效的路径。
    只有在「清单为空且从未托管过任何地址」时才是真正的空转（skipped）。
    """
    doc = load_dns_doc()
    if not doc.get("records") and not doc.get("managed"):
        return {"ok": True, "actions": [], "skipped": True, "error": ""}
    if not core_running():
        return {"ok": False, "actions": [], "error": "核心未运行，暂不同步"}

    with _dns_lock:
        # 重新读一次：可能刚被界面改过
        doc = load_dns_doc()
        if not doc.get("records") and not doc.get("managed"):
            return {"ok": True, "actions": [], "skipped": True, "error": ""}
        try:
            client = _console_client()
            actions = DP.sync_once(doc, client, log=log)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "actions": [], "error": "同步异常: %s" % e}
        try:
            save_dns_doc(doc)
        except OSError as e:
            return {"ok": False, "actions": actions, "error": "写入 dns-ip.json 失败: %s" % e}

    changed = [a for a in actions if a.get("action") in ("add", "del")]
    errors = [a for a in actions if a.get("action") == "error"]
    if changed or errors:
        log("%s：变更 %d 项，异常 %d 项" % (log_prefix, len(changed), len(errors)))
    return {"ok": not errors, "actions": actions, "error": ""}


def set_dns_peers(hosts, interval=None):
    """保存域名清单（界面入口）。保存后立刻同步一次。

    hosts 可以是多行文本、列表或逗号分隔串。
    注意 merge_records：同名域名的解析状态会被保留，避免无谓的删了又加。
    """
    recs, errs = DP.parse_lines(hosts)
    if errs:
        return {"ok": False, "errors": errs}

    if interval is not None:
        iv, ierr = DP.normalize_interval(interval)
        if ierr:
            return {"ok": False, "errors": [ierr]}
    else:
        iv = load_dns_doc().get("interval", DP.DEFAULT_INTERVAL)

    # 配了出站互联域名就必须开持久化（互联记录存在 SQLite 里）
    if recs and not C.load_settings(SERVER_DIR).get("persistence", True):
        return {"ok": False, "errors": [
            "配置了出站互联域名时必须开启「数据持久化」（互联记录存在数据库中）"]}

    with _dns_lock:
        old = load_dns_doc()
        doc = {"version": 1, "interval": iv,
               "records": DP.merge_records(old.get("records") or [], recs),
               # managed 必须一起带上：清空清单时它是「撤下哪些地址」的唯一依据，
               # 在这里丢掉就再也无从得知我们曾加过什么。
               "managed": list(old.get("managed") or [])}
        ensure_dirs()
        save_dns_doc(doc)

    res = sync_dns_peers(log_prefix="域名同步（保存后）")
    return {"ok": True, "status": dns_peers_status(),
            "sync": res.get("error") or "已同步", "actions": res.get("actions") or []}


def dns_sync_loop(stop_event):
    """后台轮询线程。

    只要「还有清单」或「还有待撤下的托管地址」就同步；两者都空则空转等待，
    不做任何网络请求。清理成功后 managed 会归空，线程自然转入空转，不会空转
    发起请求。
    """
    while not stop_event.is_set():
        interval = DP.DEFAULT_INTERVAL
        try:
            doc = load_dns_doc()
            interval = doc.get("interval") or DP.DEFAULT_INTERVAL
            if doc.get("records") or doc.get("managed"):
                sync_dns_peers()
        except Exception as e:  # noqa: BLE001
            log("域名轮询异常: %s" % e)
        # 用 Event.wait 替代 sleep，退出时能立刻醒来
        try:
            iv = max(DP.MIN_INTERVAL, int(interval))
        except (TypeError, ValueError):
            iv = DP.DEFAULT_INTERVAL
        stop_event.wait(iv)


def _migrate_legacy_peer_domains():
    """把历史 config.toml 里的 peer_servers 域名搬到 dns-ip.json。

    老版本允许在配置里直接写出站互联地址。由于核心只认 IP:port，
    这些条目（无论域名还是裸 IP）在库里都是「连不上却一直重试」的死记录。
    迁移做法：能解析出 host:port 的都搬进 dns-ip.json 清单，
    然后重渲染 config.toml 把 peer_servers 清空 —— 记录不会丢，
    且下次同步就会自动把它换成当前正确的 IP。
    """
    if not os.path.isfile(CONFIG_TOML):
        return False
    try:
        text = read_file(CONFIG_TOML)
    except OSError:
        return False
    m = re.search(r"^\s*peer_servers\s*=\s*\[(.*?)\]", text, re.M | re.S)
    if not m:
        return False
    raw = m.group(1).strip()
    if not raw:
        return False
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', raw)
    hosts = [i.replace('\\"', '"').replace("\\\\", "\\") for i in items]
    if not hosts:
        return False

    # 只搬「还没进清单」的条目，避免覆盖已有解析状态
    doc = load_dns_doc()
    existing = set()
    for r in doc.get("records") or []:
        existing.add("%s:%s" % (str(r.get("host", "")).lower(), r.get("port")))
    new_recs, errs = DP.parse_lines(hosts)
    add = []
    for r in new_recs:
        key = "%s:%s" % (r["host"].lower(), r["port"])
        if key not in existing:
            add.append(r)
    for e in errs:
        log("历史互联地址无法迁移：%s" % e)

    if add:
        doc["records"] = (doc.get("records") or []) + add
        try:
            save_dns_doc(doc)
            log("已把 %d 个历史互联地址迁入 dns-ip.json" % len(add))
        except OSError as e:
            log("迁移 dns-ip.json 失败: %s" % e)
            return False
    # 清空配置里的记录（重渲染即恒写 []）
    try:
        render_config()
    except Exception as e:  # noqa: BLE001
        log("迁移后重渲染 config.toml 失败: %s" % e)
    return bool(add)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg, f=None):
    line = "%s [shell] %s\n" % (now(), msg)
    try:
        ensure_dirs()
        with open(f or main_log(), "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def read_file(p, default=""):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def write_file(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# 端口 / 镜像 / 代理
# ---------------------------------------------------------------------------
def load_ports():
    ports = dict(DEFAULTS)
    txt = read_file(PORT_FILE)
    for line in txt.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            ports[k.strip()] = v.strip()
    for k, v in DEFAULTS.items():
        ports.setdefault(k, v)
    return ports


def save_ports(ports):
    lines = ["%s=%s" % (k, ports.get(k, DEFAULTS[k])) for k in
             ("PANEL_PORT", "SERVICE_PORT", "CONSOLE_PORT")]
    write_file(PORT_FILE, "\n".join(lines) + "\n")


def get_mirror():
    return read_file(MIRROR_FILE).strip().rstrip("/")


def get_proxy():
    return read_file(PROXY_FILE).strip()


# 形如 https://ghproxy.example.com 的地址是「加速镜像前缀」（Cloudflare Worker
# 之类的反代，用法是 <前缀>/https://github.com/...），不是正向代理。
# 误填进代理栏后 urllib 会向它发 CONNECT 而无人应答，表现为「连通性测试超时」，
# 且之后所有下载都会失败。这类地址必须引导到镜像前缀。
_MIRROR_LIKE = re.compile(r"^https?://[^/?#:]+/?$", re.I)
_SOCKS_SCHEMES = ("socks", "socks4", "socks4a", "socks5", "socks5h")


def normalize_proxy(p):
    """校验并归一化代理地址。返回 (地址, 错误信息)。空串视为「不使用代理」。"""
    raw = (p or "").strip()
    p = raw
    if not p:
        return "", ""
    # 用户显式写了 scheme（https://xxx）才可能是「把加速前缀填错了栏」；
    # 只写 127.0.0.1 这种，多半是忘了端口。
    explicit_scheme = "://" in raw
    if not explicit_scheme:
        p = "http://" + p
    try:
        u = urllib.parse.urlsplit(p)
        scheme = (u.scheme or "").lower()
        host = u.hostname
        port = u.port
    except ValueError:
        return "", "代理地址格式无效：%s" % p

    if scheme in _SOCKS_SCHEMES:
        # 各协议都交给 curl 处理（见页面里的说明），因此 socks4/socks4a
        # 也一并放行 —— 与同作者的 fnos-vnt2 行为一致。
        if not host:
            return "", "代理地址缺少主机名"
        if not port:
            return "", "代理地址缺少端口，例如 socks5h://127.0.0.1:7891"
        return "%s://%s:%d" % (scheme, host, port), ""
    if scheme not in ("http", "https"):
        return "", ("代理地址只支持 http:// 、https:// 、socks5:// "
                    "或 socks4:// 开头")
    if not host:
        return "", "代理地址缺少主机名"
    if not port:
        if explicit_scheme and _MIRROR_LIKE.match(p):
            return "", ("这不是代理，是「下载加速前缀」（不带端口）。"
                        "带端口的才是代理，如 http://127.0.0.1:7890；"
                        "%s 请填到上面的「镜像前缀」里" % p)
        return "", "代理地址缺少端口，例如 http://127.0.0.1:7890"
    # 去掉末尾斜杠，避免拼进请求行
    return "%s://%s:%d" % (scheme, host, port), ""


def normalize_mirror(m):
    """校验并归一化镜像前缀。返回 (前缀, 错误信息, 警告)。"""
    m = (m or "").strip().rstrip("/")
    if not m:
        return "", "", ""
    if "://" not in m:
        m = "https://" + m
        m = m.rstrip("/")
    try:
        u = urllib.parse.urlsplit(m)
        scheme = (u.scheme or "").lower()
    except ValueError:
        return "", "镜像前缀格式无效：%s" % m, ""
    if scheme not in ("http", "https"):
        return "", "镜像前缀必须以 http:// 或 https:// 开头", ""
    if not u.hostname:
        return "", "镜像前缀缺少主机名", ""
    if u.port and scheme in _SOCKS_SCHEMES:
        return "", "这看起来是代理地址，请填到「网络代理」里", ""
    warn = ""
    if u.port:
        warn = "镜像前缀通常不带端口；若 %s 其实是 HTTP 代理，请改填到「网络代理」" % m
    return m, "", warn


def describe_net_error(e):
    """把 urllib 的异常翻译成能看懂的话。"""
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 407:
            return "代理要求认证（407）"
        if e.code in (403, 429):
            return "已连通，但被 GitHub 限流（HTTP %d，不影响下载）" % e.code
        return "HTTP %d" % e.code
    reason = getattr(e, "reason", e)
    text = str(reason)
    low = text.lower()
    if isinstance(reason, TimeoutError) or "timed out" in low:
        return "连接超时（地址/端口不通，或被墙）"
    if "connection refused" in low or "拒绝" in text:
        return "连接被拒绝（端口无人监听）"
    if "name or service not known" in low or "getaddrinfo" in low:
        return "域名解析失败"
    if "certificate" in low or "ssl" in low:
        return "TLS/证书错误"
    if "tunnel connection failed" in low:
        return "代理隧道建立失败（该地址不是正向代理）"
    return text or "未知错误"


def set_proxy(p):
    """保存代理：只校验格式，**不实测**。

    为什么不实测：保存是用户的动作，不该由面板代发起外连 —— 启动了代理时
    会在代理端留下一条莫名的连接记录（用户会以为是异常流量）。
    通不通由用户点「测试连通性」自行确认，那里会走完整的下载路径。
    """
    norm, err = normalize_proxy(p)
    if err:
        return {"ok": False, "error": err}
    write_file(PROXY_FILE, norm)
    return {"ok": True, "proxy": norm}


def set_mirror(prefix):
    m, err, warn = normalize_mirror(prefix)
    if err:
        return {"ok": False, "error": err}
    write_file(MIRROR_FILE, m)
    return {"ok": True, "mirror": m, "warning": warn}


def url_with_mirror(u):
    m = get_mirror()
    return "%s/%s" % (m, u) if m else u


def _is_socks(proxy):
    """判断代理串是否为 SOCKS 系（socks5 / socks5h / socks4 / socks4a）。"""
    return str(proxy or "").lower().startswith("socks")


# ---------------------------------------------------------------------------
# 经代理发请求：统一交给 curl
#
# 为什么不自己实现 SOCKS5：早期版本起过一个本地回环中转，把 urllib 的
# CONNECT 转成 SOCKS5 CONNECT。它在 Clash 系代理上不可用 —— 表现为
# HTTP 代理正常、SOCKS5 报 HTTP 554。那条路径把 HTTPS 请求转成
# 「urllib 以为在用 HTTP 代理」的形态，与 curl 这类成熟实现的握手/隧道
# 语义并不等价，自己维护协议栈也不划算。
#
# 现在与同作者的 fnos-vnt2 保持一致：只要有代理就调 curl，
# 由 curl 原生处理 http/https/socks5/socks5h/socks4 —— 三种协议行为一致。
# ---------------------------------------------------------------------------
def find_curl():
    """找一个可用的 curl，返回路径；找不到返回空串。"""
    for c in ("/usr/bin/curl", "/usr/local/bin/curl", "curl"):
        try:
            r = subprocess.run([c, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return c
        except Exception:  # noqa: BLE001
            continue
    return ""


class _CurlResponse(object):
    """curl 结果的响应包装：只实现调用方用到的那几个属性/方法。"""

    def __init__(self, status, body, url):
        self.status = status
        self.code = status
        self._body = body
        self._url = url
        self._pos = 0
        self.closed = False

    def read(self, n=-1):
        if n is None or n < 0:
            data = self._body[self._pos:]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos:self._pos + n]
        self._pos += len(data)
        return data

    def geturl(self):
        return self._url

    def info(self):
        return {}

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _curl_run(url, proxy, timeout, method="GET", headers=None):
    """调 curl 取 url，返回 (status, body)。失败抛异常（消息可直接展示）。"""
    curl = find_curl()
    if not curl:
        raise RuntimeError("未找到 curl —— SOCKS 代理需要它，请先安装")
    args = [curl, "-sSL", "--max-time", str(timeout),
            "-w", "\n%{http_code}", "-A", "vnts-nas-fpk"]
    if method == "HEAD":
        args.append("-I")
    for k, v in (headers or {}).items():
        args += ["-H", "%s: %s" % (k, v)]
    if proxy:
        # --noproxy "" 显式关掉 NO_PROXY 环境变量的影响：
        # 否则环境里若设了 no_proxy=127.0.0.1（很常见），curl 会绕过代理
        # 直连，行为与用户预期不符（配了代理却没走代理）。
        args += ["--proxy", proxy, "--noproxy", ""]
    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        raise TimeoutError("超时（%ds）" % timeout)
    if r.returncode != 0:
        raise RuntimeError(
            (r.stderr or b"").decode("utf-8", "replace").strip()
            or "curl 退出码 %d" % r.returncode)
    out = r.stdout
    idx = out.rfind(b"\n")
    body = out[:idx] if idx >= 0 else out
    code = 0
    if idx >= 0:
        try:
            code = int(out[idx + 1:] or b"0")
        except ValueError:
            code = 0
    return code, body


def _open(url, timeout=30, proxy=None, method="GET"):
    """打开 URL。

    proxy=None  -> 使用配置里的代理（若有）
    proxy=""    -> 强制直连，同时禁用系统环境变量里的 http_proxy
    proxy="x://host:port" -> 用指定代理（http/https/socks5/socks5h/socks4）

    **有代理时优先交给 curl**（与同作者的 fnos-vnt2 同策略）：
    curl 原生支持各协议，行为统一，也省掉自己维护 SOCKS5 协议栈的成本。
    无代理时走 urllib 直连（并显式清掉环境变量里的 http_proxy）。

    环境里没有 curl 时的降级：HTTP/HTTPS 代理仍可交给 urllib（它原生支持），
    SOCKS 代理则无法工作 —— 这时抛出明确提示，而不是静默失败。
    """
    headers = {
        "User-Agent": "vnts-nas-fpk",
        "Accept": "application/vnd.github+json, */*",
    }
    if proxy is None:
        proxy = get_proxy()

    if proxy:
        if find_curl() or _is_socks(proxy):
            code, body = _curl_run(url, proxy, timeout, method=method,
                                   headers=headers)
            return _CurlResponse(code, body, url)
        # 无 curl 且是 HTTP(S) 代理：urllib 也能做，降级处理
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        req = urllib.request.Request(url, method=method, headers=headers)
        return op.open(req, timeout=timeout)

    req = urllib.request.Request(url, method=method, headers=headers)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return op.open(req, timeout=timeout)


def _proxy_opener():
    """返回一个「按当前配置代理打开 URL」的函数。"""
    proxy = get_proxy()

    def _opener(req, timeout=30):
        if proxy:
            if find_curl() or _is_socks(proxy):
                headers = dict(req.header_items())
                code, body = _curl_run(req.full_url, proxy, timeout,
                                       method=req.get_method() or "GET",
                                       headers=headers)
                return _CurlResponse(code, body, req.full_url)
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            return op.open(req, timeout=timeout)
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return op.open(req, timeout=timeout)

    return _opener


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": "vnts-nas-fpk",
        "Accept": "application/vnd.github+json, */*",
    })
    return _proxy_opener()(req, timeout=timeout)


# ---------------------------------------------------------------------------
# 管理页令牌
# ---------------------------------------------------------------------------
def get_panel_token():
    t = read_file(PANEL_TOKEN_FILE).strip()
    if not t:
        import secrets
        t = secrets.token_hex(16)
        os.makedirs(BIN_DIR, exist_ok=True)
        with open(PANEL_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(t)
        try:
            os.chmod(PANEL_TOKEN_FILE, 0o600)
        except OSError:
            pass
    return t


def set_panel_token(t):
    t = (t or "").strip()
    if len(t) < 8:
        raise ValueError("令牌至少 8 位")
    os.makedirs(BIN_DIR, exist_ok=True)
    with open(PANEL_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(t)
    try:
        os.chmod(PANEL_TOKEN_FILE, 0o600)
    except OSError:
        pass


def get_console_password():
    return C.load_settings(SERVER_DIR).get("password", "admin")


def get_console_username():
    return C.load_settings(SERVER_DIR).get("username", "admin")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def render_config():
    """渲染 config.toml 与 logs/log4rs.yaml。

    显式带上独立端口（proto-ports.json）—— 否则 C.render 内部用
    load_settings 反推，会把「复用」误判为「固定端口」，导致改了
    SERVICE_PORT 后复用者不跟随（曾出现的 bug）。
    """
    ensure_dirs()
    return C.render(SERVER_DIR, log_dir(), PORT_FILE,
                    settings=_settings_with_proto_ports(),
                    log_conf=load_log_conf())


def get_settings():
    """界面读取用：config.toml 的设置 + 独立端口。

    独立端口的**意图**（复用还是固定）不在 config.toml 里 —— 那里只有
    展开后的端口数字，反推不出用户本意。因此单独存在 proto-ports.json，
    这里合并进来给界面回显。
    """
    s = C.load_settings(SERVER_DIR, PORT_FILE)
    s.update(load_proto_ports())
    return s


def update_settings(patch):
    """保存服务端设置：写 config.toml（唯一配置文件）。

    独立端口不写进 config.toml，而是存 proto-ports.json（意图要与展开后的
    端口分开，否则「改服务端口时复用者是否跟随」会判断错）。

    只落盘，不重启核心 —— 由用户自行决定何时重启使其生效。
    """
    patch = patch or {}
    s = C.load_settings(SERVER_DIR, PORT_FILE)
    # 现有独立端口先合并进来，保证「只改一项」时其余不回退成复用
    s.update(load_proto_ports())
    s.update(patch)
    # 独立端口不能与面板/控制台/互联端口撞车，也需校验协议之间互不冲突
    ports = load_ports()
    errs = C.validate(
        s,
        reserved=[ports.get("PANEL_PORT"), ports.get("CONSOLE_PORT"),
                  s.get("peer_bind_port") if s.get("enable_peer") else None],
        reserved_names=["面板端口", "控制台端口", "服务端互联端口"])
    if errs:
        return {"ok": False, "errors": errs}
    ensure_dirs()
    # 独立端口单独落盘（空值会被省略，表示复用）
    save_proto_ports({k: s.get(k) for k in ("tcp_port", "quic_port", "ws_port")})
    # 一次渲染同时写出 config.toml 与 logs/log4rs.yaml，避免两次读改写
    C.render(SERVER_DIR, log_dir(), PORT_FILE, settings=s,
             log_conf=load_log_conf())
    return {"ok": True, "restart_needed": core_running()}


# ---------------------------------------------------------------------------
# 架构 / 版本
# ---------------------------------------------------------------------------
TRIPLES = {
    "x86_64": "x86_64-unknown-linux-musl",
    "amd64": "x86_64-unknown-linux-musl",
    "aarch64": "aarch64-unknown-linux-musl",
    "arm64": "aarch64-unknown-linux-musl",
    "armv7l": "armv7-unknown-linux-musleabihf",
    "armv7": "armv7-unknown-linux-musleabihf",
    "armv6l": "arm-unknown-linux-musleabihf",
    "arm": "arm-unknown-linux-musleabihf",
    "mips": "mips-unknown-linux-musl",
    "mipsel": "mipsel-unknown-linux-musl",
}


def machine():
    """跨平台架构名（Linux 上等价于 uname -m）。"""
    m = ""
    try:
        m = os.uname().machine  # type: ignore[attr-defined]
    except AttributeError:
        import platform
        m = platform.machine()
    return (m or "").lower()


def arch_triple(m=None):
    return TRIPLES.get((m or machine()).lower())


def installed_version():
    return read_file(CORE_VER_FILE).strip()


def latest_version():
    """查询最新版本号（不带 v）。"""
    for fn in (_latest_via_api, _latest_via_redirect):
        try:
            v = fn()
            if v and re.match(r"^\d+\.\d+(\.\d+)?$", v):
                return v
        except Exception:  # noqa: BLE001
            continue
    return ""


def _latest_via_api():
    url = url_with_mirror("https://api.github.com/repos/vnt-dev/vnts/releases/latest")
    with http_get(url, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return str(data.get("tag_name", "")).lstrip("v")


def _latest_via_redirect():
    """不调 API，直接跟随 releases/latest 的 302 拿版本号。

    这是 api.github.com 之外的一条路：api.github.com 对未认证请求限流更严
    （60 次/小时），且国内直连成功率更低；而 releases/latest 只是个跳转，
    经镜像前缀也能用。
    """
    url = url_with_mirror("https://github.com/vnt-dev/vnts/releases/latest")
    proxy = get_proxy()
    if proxy:
        # 交给 curl 取最终地址：%{url_effective} 就是跟随重定向后的 URL
        curl = find_curl()
        if not curl:
            return ""
        args = [curl, "-sSLI", "-o", os.devnull, "--max-time", "20",
                "-w", "%{url_effective}", "-A", "vnts-nas-fpk",
                "--proxy", proxy, "--noproxy", "", url]
        try:
            r = subprocess.run(args, capture_output=True, timeout=35)
        except subprocess.TimeoutExpired:
            return ""
        if r.returncode != 0:
            return ""
        eff = r.stdout.decode("utf-8", "replace").strip()
    else:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "vnts-nas-fpk"})
        try:
            r = _proxy_opener()(req, timeout=20)
            eff = r.geturl()
        except urllib.error.HTTPError as e:
            eff = e.geturl() if hasattr(e, "geturl") else ""
    eff = eff.replace("%3A", ":").replace("%2F", "/")
    tag = eff.rstrip("/").split("/")[-1].lstrip("v")
    return tag


# ---------------------------------------------------------------------------
# 下载链路自检
# ---------------------------------------------------------------------------
RELEASES_LATEST = "https://github.com/vnt-dev/vnts/releases/latest"


def _probe_url(url, proxy, timeout=10):
    """探测一个 URL 是否可达。返回 (ok, detail)。"""
    t0 = time.time()
    try:
        with _open(url, timeout=timeout, proxy=proxy) as r:
            try:
                r.read(256)
            except Exception:  # noqa: BLE001
                pass
        return True, "可达（%d ms）" % int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            return True, "已连通（GitHub 限流 %d，不影响下载）" % e.code
        if e.code == 407:
            return False, "代理要求认证（407）"
        if e.code == 404:
            return False, "HTTP 404：地址不存在（镜像路径可能不对）"
        return True, "HTTP %d" % e.code
    except Exception as e:  # noqa: BLE001
        return False, describe_net_error(e)


def probe_network(timeout=10):
    """探测「能不能下到核心」。

    只关心一件事：**按当前配置，核心文件下得下来吗**。
    因此只测与下载直接相关的两项（配了才测）：

      - 镜像前缀   ：镜像地址本身通不通
      - 网络代理   ：代理隧道通不通
      - 实际下载路径：把镜像+代理一起用上，去取**真实的版本文件**

    **不测「直连 GitHub」**：国内网络下它几乎必然超时，而用户的配置
    本就是「镜像/代理」这条路 —— 红着一行既无意义又误导，
    会让人以为配置有问题。判定标准就是最后一行的结果。
    """
    mirror = get_mirror()
    proxy = get_proxy()
    checks = []

    def add(name, ok, detail, url=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "url": url})

    mirror_ok = None
    if mirror:
        mu = url_with_mirror(RELEASES_LATEST)
        mirror_ok, md = _probe_url(mu, "", timeout)
        add("镜像前缀", mirror_ok, md, mu)

    proxy_ok = None
    if proxy:
        proxy_ok, pd = _probe_url(RELEASES_LATEST, proxy, timeout)
        add("网络代理", proxy_ok, pd, RELEASES_LATEST)

    # 实际下载路径：镜像与代理一起用上，这才是真正下核心时走的链路。
    # 没配镜像/代理时，这一项等价于直连 —— 仍需测，因为它是判定依据。
    pu = url_with_mirror(RELEASES_LATEST)
    pu_proxy = proxy or ""
    pu_ok, pu_detail = _probe_url(pu, pu_proxy, timeout)
    add("实际下载路径", pu_ok, pu_detail, pu)
    ok = pu_ok

    # 给能直接照做的修法，而不是只说失败
    hint = ""
    if not ok:
        if proxy and _MIRROR_LIKE.match(proxy):
            hint = ("代理栏里的 %s 是「加速前缀」不是代理，请清空代理、把它填到镜像前缀"
                    % proxy)
        elif proxy and proxy_ok is False:
            hint = "代理连不上：检查地址与端口，或清空代理改直连/镜像"
        elif mirror and mirror_ok is False:
            hint = "镜像前缀不可用：换一个加速前缀，或清空镜像直连 GitHub"
        else:
            hint = ("下载不通（国内网络常见）。建议填一个镜像前缀；"
                    "若已填镜像仍不通，检查镜像地址是否写对、或改用代理")
    return {"ok": ok, "checks": checks, "hint": hint,
            "mirror": mirror, "proxy": proxy}


# ---------------------------------------------------------------------------
# 核心安装
# ---------------------------------------------------------------------------
def core_asset_url(ver):
    triple = arch_triple()
    if not triple:
        raise RuntimeError("当前架构 %s 无官方预编译包" % machine())
    asset = "vnts2-%s-v%s" % (triple, ver)
    return url_with_mirror(
        "https://github.com/vnt-dev/vnts/releases/download/v%s/%s" % (ver, asset)
    ), asset


def install_core_from_bytes(data, ver=""):
    """从字节流安装核心（在线下载与手动上传共用）。"""
    if len(data) < 200 * 1024:
        return {"ok": False, "error": "不像是核心程序（文件过小）"}
    if data[:4] != b"\x7fELF":
        return {"ok": False, "error": "不是 Linux 可执行文件（ELF）"}
    ensure_dirs()
    if os.path.isfile(CORE_BIN):
        try:
            shutil.copyfile(CORE_BIN, os.path.join(CACHE_DIR, "vnts2.bak"))
        except OSError:
            pass
    newp = CORE_BIN + ".new"
    with open(newp, "wb") as f:
        f.write(data)
    os.chmod(newp, 0o755)
    os.replace(newp, CORE_BIN)
    if ver:
        write_file(CORE_VER_FILE, ver)
    _probe_clear()
    return {"ok": True, "version": ver or installed_version()}


def _github_asset_url(ver, asset):
    return "https://github.com/vnt-dev/vnts/releases/download/v%s/%s" % (ver, asset)


def download_core(ver):
    """下载核心二进制。

    一次误配（例如把加速前缀填进代理栏）不该让「更新核心」永久不可用，
    因此按 配置路径 -> 镜像直连 -> GitHub 直连 依次尝试，并逐次记日志。
    """
    url, asset = core_asset_url(ver)          # url 已带镜像前缀
    direct = _github_asset_url(ver, asset)

    plan = [("配置路径", url, None)]
    if get_mirror():
        plan.append(("镜像直连", url, ""))
    if get_mirror() or get_proxy():
        plan.append(("GitHub 直连", direct, ""))

    last = None
    for label, u, px in plan:
        log("下载核心 %s <- [%s] %s" % (ver, label, u))
        try:
            with _open(u, timeout=600, proxy=px) as r:
                data = r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            log("下载失败 [%s]：%s" % (label, describe_net_error(e)))
            continue
        res = install_core_from_bytes(data, ver)
        if res.get("ok"):
            write_file(CORE_SRC_FILE, "version=%s\nasset=%s\nurl=%s\ninstalled_at=%s\n"
                       % (ver, asset, u, now()))
        return res
    return {"ok": False, "error": "下载失败：%s"
            % (describe_net_error(last) if last else "未知错误")}


def rollback_core():
    bak = os.path.join(CACHE_DIR, "vnts2.bak")
    if not os.path.isfile(bak):
        return False
    shutil.copyfile(bak, CORE_BIN)
    os.chmod(CORE_BIN, 0o755)
    return True


# ---------------------------------------------------------------------------
# 进程托管
# ---------------------------------------------------------------------------
# 进程操作锁。
# 必须是**可重入**且覆盖「整个操作序列」的：
#   restart = stop + start，若两步各自加锁，中间空隙会被 watcher 巡检插入，
#   导致两个 start_core 同时通过「未运行」检查、抢同一个控制台端口
#   → 第二个实例报 `Error: Address in use (os error 98)`。
# 因此对外函数统一持锁，内部实现（_stop_core_locked / _start_core_locked）不加锁。
_proc_lock = threading.RLock()


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def core_running():
    """返回核心进程 pid，未运行返回 None。

    除了看 pid 文件，还会**额外检查控制台端口**是否真的被本进程占用，
    以防上次异常退出留下「pid 文件在、进程却已死」的假象。
    """
    pid = read_file(CORE_PID_FILE).strip().splitlines()
    if not pid:
        return None
    try:
        p = int(pid[0])
    except ValueError:
        return None
    if not _pid_alive(p):
        return None
    return p


def port_in_use(port, timeout=1.5):
    """TCP 端口是否已被占用（本地回环）。"""
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def udp_port_in_use(port, timeout=0.5):
    """UDP 端口是否已被占用（本地回环）。

    为什么必须单独测：核心的 quic 与 server_quic（互联信令）都走 **UDP**，
    而 TCP 的 connect_ex 对 UDP 绑定完全无感（实测返回非 0 = 误判为空闲）。
    只测 TCP 就会漏掉 UDP 冲突，核心起来后 bind 失败并立刻退出。
    """
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    try:
        # 已被别人独占绑定（无 SO_REUSEADDR）时，bind 会失败
        s.bind(("127.0.0.1", int(port)))
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except OSError:
            pass


def core_bind_ports():
    """核心启动时会 bind 的全部端口（去重、排序）。

    **这是「Address in use」的真正排查范围。** 之前只看控制台端口，
    于是「组网端口被孤儿进程占着、控制台端口空着」时误判为无冲突，
    核心一起来就 bind 失败。

    实际绑定（见 vnts_config.render_config_toml）：
      tcp_bind / quic_bind / ws_bind —— 默认同为 SERVICE_PORT
      web_bind                     —— CONSOLE_PORT
      server_quic_bind             —— peer_bind_port（默认 29873）
    """
    ports = set()
    try:
        ports.add(int(load_ports()["SERVICE_PORT"]))
    except (KeyError, ValueError):
        pass
    try:
        ports.add(int(load_ports()["CONSOLE_PORT"]))
    except (KeyError, ValueError):
        pass
    # 三协议可能各自设了独立端口
    try:
        pp = load_proto_ports()
        for k in ("tcp_port", "quic_port", "ws_port"):
            v = str(pp.get(k) or "").strip()
            if v.isdigit():
                ports.add(int(v))
    except Exception:  # noqa: BLE001
        pass
    # 服务端互联端口
    try:
        s = C.load_settings(SERVER_DIR, PORT_FILE)
        v = int(s.get("peer_bind_port") or 0)
        if v:
            ports.add(v)
    except Exception:  # noqa: BLE001
        pass
    return sorted(p for p in ports if p > 0)


def describe_port_conflicts(ports=None):
    """返回 [(port, "tcp"/"udp")]，列出当前被占用的端口。"""
    out = []
    for p in (ports if ports is not None else core_bind_ports()):
        if port_in_use(p):
            out.append((p, "tcp"))
        if udp_port_in_use(p):
            out.append((p, "udp"))
    return out


def _console_port():
    return int(load_ports()["CONSOLE_PORT"])


def _kill_stragglers():
    """清理未记录在 pid 文件、但仍占着端口的残留 vnts2。

    这是「端口被占用」的兜底：正常路径下不该有残留，
    但异常退出（kill -9、容器被杀、卸载时 pid 文件已丢）都可能留下孤儿。

    排查范围是**核心会 bind 的全部端口**（见 core_bind_ports），
    而不是只看控制台端口 —— 只查一个会漏掉组网端口被占的情况：
    那时核心起身读配置都正常，一到 bind 就报 Address in use 并退出。
    """
    ports = core_bind_ports()
    conflicts = describe_port_conflicts(ports)
    if not conflicts:
        return
    desc = "、".join("%d/%s" % (p, k) for p, k in conflicts)
    log("端口被占用（%s），尝试清理残留核心进程" % desc)
    killed = False
    # 优先按进程名精确匹配，避免误杀其它程序
    for pat in (os.path.basename(CORE_BIN), "vnts2", "vnts"):
        try:
            r = subprocess.run(["pkill", "-f", pat],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                killed = True
        except (OSError, subprocess.SubprocessError):
            pass
    if not killed:
        # 退回到按端口杀：tcp 与 udp 都要试（quic 走 udp）
        for p, kind in conflicts:
            for tool in (["fuser", "-k", "%d/%s" % (p, kind)],):
                try:
                    subprocess.run(tool, capture_output=True, timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
    # 等待端口释放
    t0 = time.time()
    while time.time() - t0 < 5:
        if not describe_port_conflicts(ports):
            return
        time.sleep(0.3)
    left = "、".join("%d/%s" % (p, k) for p, k in describe_port_conflicts(ports))
    if left:
        log("WARN: 端口仍被占用：%s" % left)


def wait_core(up=True, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if bool(core_running()) == up:
            return True
        time.sleep(0.5)
    return bool(core_running()) == up


def _stop_core_locked():
    """停止核心（**调用方须已持 _proc_lock**）。"""
    p = core_running()
    if not p:
        _rm(CORE_PID_FILE)
        return True
    log("停止核心 pid=%d" % p)
    try:
        os.kill(p, signal.SIGTERM)
    except OSError:
        pass
    t0 = time.time()
    while time.time() - t0 < 15:
        if not _pid_alive(p):
            break
        time.sleep(0.5)
    if _pid_alive(p):
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(1)
    _rm(CORE_PID_FILE)
    return not _pid_alive(p)


def stop_core():
    with _proc_lock:
        return _stop_core_locked()


_probe_cache = {"t": 0, "ok": False, "info": ""}


def _probe_clear():
    _probe_cache["t"] = 0


def probe_core(timeout=5):
    """运行 vnts2 --version / --conf-example，检查二进制是否可执行。"""
    if not os.path.isfile(CORE_BIN):
        return False, "核心未下载"
    try:
        r = subprocess.run([CORE_BIN, "--version"], capture_output=True, timeout=timeout)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        txt = (out + err).strip()
        if r.returncode == 0 or "vnts" in txt.lower() or "version" in txt.lower():
            return True, txt.splitlines()[0] if txt else "ok"
        return True, txt or "ok"
    except subprocess.TimeoutExpired:
        return True, "可执行（未返回版本）"
    except OSError as e:
        return False, "无法执行: %s" % e


def _start_core_locked():
    """启动核心（**调用方须已持 _proc_lock**）。"""
    if core_running():
        return True, "核心已在运行"
    if not os.path.isfile(CORE_BIN):
        return False, "核心未下载"
    ok, info = probe_core()
    if not ok:
        return False, "核心无法运行（%s）" % info

    # 启动前清掉可能残留的孤儿实例，避免控制台端口冲突
    # （报错长相：Error: Address in use (os error 98)）。
    # 必须持锁执行：否则 watcher 会在此刻插入、产生第二个实例。
    _kill_stragglers()
    stuck = describe_port_conflicts()
    if stuck:
        desc = "、".join("%d/%s" % (p, k) for p, k in stuck)
        return False, ("端口已被占用，无法启动核心：%s。"
                       "这些是核心要监听的端口（组网 TCP/UDP、控制台、互联）。"
                       "请先停掉占用它的程序，或到「服务端设置」换端口。"
                       % desc)

    render_config()
    os.makedirs(SERVER_DIR, exist_ok=True)
    os.makedirs(log_dir(), exist_ok=True)

    # 以 SERVER_DIR 为 CWD：vnts2 会在此读 config.toml、写数据库，
    # 并从 logs/log4rs.yaml 读取日志配置（其已指向独立 LOG_DIR）。
    lf = open(core_log(), "ab")
    try:
        env = dict(os.environ)
        env["RUST_BACKTRACE"] = "1"
        proc = subprocess.Popen(
            [CORE_BIN],
            cwd=SERVER_DIR,
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    finally:
        lf.close()
    write_file(CORE_PID_FILE, str(proc.pid))
    log("核心已启动 pid=%d" % proc.pid)
    time.sleep(1.5)
    if proc.poll() is not None:
        _rm(CORE_PID_FILE)
        tail = _tail(core_log(), 20)
        # 「立即退出」最常见的原因是端口被占（日志里的 Address in use）。
        # 直接点出来并给出可操作建议，比只甩一段日志有用得多。
        if "Address in use" in tail or "os error 98" in tail:
            stuck = describe_port_conflicts()
            hint = ("、".join("%d/%s" % (p, k) for p, k in stuck)
                    if stuck else "（端口可能刚被释放，请重试）")
            return False, (
                "启动失败：端口被占用（%s）。\n"
                "这是核心要监听的端口。请先停掉占用它的程序；"
                "或到「服务端设置」里把这些端口改成别的号。\n"
                "日志尾部：\n%s" % (hint, tail))
        return False, "核心启动后立即退出。日志尾部：\n%s" % tail
    return True, "核心运行中 (pid=%d)" % proc.pid


def _sync_after_core_up(prefix):
    """核心刚就绪时补一次域名同步。

    为什么必须放在核心起来之后：同步是经**控制台 API**把地址写进核心的
    SQLite（`/api/peer_servers`），核心没运行就无从写起。而 dns-ip.json
    随时可能被改（保存清单、导入配置），所以每次核心就绪都补一次，
    不必等下一个轮询周期。
    """
    try:
        sync_dns_peers(log_prefix=prefix)
    except Exception as e:  # noqa: BLE001
        log("%s 失败: %s" % (prefix, e))


def start_core():
    """启动核心；成功后立刻补一次域名同步（见 _sync_after_core_up）。"""
    with _proc_lock:
        ok, msg = _start_core_locked()
    if ok:
        _sync_after_core_up("域名同步（核心启动后）")
    return ok, msg


def restart_core():
    """重启核心。

    **整体持锁**：stop 与 start 必须在同一临界区内完成。
    否则两步之间会出现「核心已停、尚未起」的空窗，watcher 巡检会插入并
    再次 start，导致两个实例抢占控制台端口（Error: Address in use）。
    使用 RLock 是为了让内部的 _stop_core_locked/_start_core_locked 可重入。
    """
    with _proc_lock:
        _stop_core_locked()
        ok, msg = _start_core_locked()
    if ok:
        _sync_after_core_up("域名同步（核心重启后）")
    return ok, msg


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
VNT_LOG_MAX = 512 * 1024
VNT_LOG_KEEP = 256 * 1024


def trim_one(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size <= VNT_LOG_MAX:
        return
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - VNT_LOG_KEEP))
            tail = f.read()
        with open(path, "wb") as f:
            f.write(("=== 日志已截断，仅保留最近 %d KB ===\n" % (VNT_LOG_KEEP // 1024)).encode())
            f.write(tail)
    except OSError:
        pass


def trim_logs():
    """截断**面板自己**的日志，避免无限增长。

    ⚠ vnts2.log **不在这里截断**。
    核心日志由它自己的 log4rs 按大小滚动（默认 10MB × 5 份，可在管理页调整）。
    此前这里也截断 vnts2.log，512KB 就砍到 256KB —— 那会把核心的滚动配置
    完全架空，用户看到的永远是「最近 256KB」，历史日志莫名消失。
    只有核心未启用 log4rs（老版本核心或 yaml 缺失）时才兜底截断它。
    """
    for n in ("main.log", "shell.log", "update.log"):
        trim_one(os.path.join(log_dir(), n))
    if not _core_log4rs_active():
        trim_one(os.path.join(log_dir(), "vnts2.log"))


def _core_log4rs_active():
    """核心是否在用我们写的 log4rs.yaml（决定 vnts2.log 归谁管）。"""
    l4 = os.path.join(SERVER_DIR, "logs", "log4rs.yaml")
    if not os.path.isfile(l4):
        return False
    # 我们渲染的 yaml 一定含 rolling_file 段落
    return "rolling_file" in read_file(l4)


def _tail(path, n=100):
    try:
        with open(path, "rb") as f:
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def read_log(name):
    safe = os.path.basename(name)
    p = os.path.join(log_dir(), safe)
    return _tail(p, 400)


def log_sizes():
    out = {}
    for n in ("main.log", "shell.log", "vnts2.log", "update.log"):
        p = os.path.join(log_dir(), n)
        try:
            out[n] = os.path.getsize(p)
        except OSError:
            out[n] = 0
    return out


def clear_log():
    for n in ("main.log", "shell.log", "vnts2.log", "update.log"):
        try:
            open(os.path.join(log_dir(), n), "w").close()
        except OSError:
            pass
    return True


# ---------------------------------------------------------------------------
# 导入导出（配置 + 数据库，不含日志与二进制）
# ---------------------------------------------------------------------------
EXPORT_MARKER = "vnts-nas-export.json"

# 导出包含：配置相关的小文件。明确排除：日志、核心二进制、pid、端口运行参数。
# config.toml 是唯一的服务端配置文件（不再有 settings.json）。
EXPORT_FILES = [
    ("config.toml", "server/config.toml"),
    ("network_control.db", "server/network_control.db"),
    ("dns-ip.json", "server/dns-ip.json"),
    ("proto-ports.json", "server/proto-ports.json"),
    ("mirror", "server/mirror"),
    ("proxy", "server/proxy"),
    ("cert.pem", "server/cert.pem"),
    ("key.pem", "server/key.pem"),
    ("ikev2-cert.pem", "server/ikev2-cert.pem"),
    ("ikev2-key.pem", "server/ikev2-key.pem"),
]

MANIFEST_NAME = "manifest.json"


def export_bundle():
    """生成导出 zip 的字节流：server 配置 + 数据库，不含日志。"""
    ensure_dirs()
    # 导出前确保配置已渲染落盘（首次安装尚未渲染时也能导出）
    try:
        if not os.path.isfile(CONFIG_TOML):
            render_config()
    except Exception as e:  # noqa: BLE001
        log("导出前生成配置失败: %s" % e)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        manifest = {
            "app": "vnts-nas",
            "format": 1,
            "created_at": now(),
            "core_version": installed_version(),
            "note": "服务器配置导出：含 config.toml、数据库与证书；不含日志与核心二进制。",
            "includes": [],
        }
        for src_name, arc in EXPORT_FILES:
            src = os.path.join(SERVER_DIR, src_name)
            if os.path.isfile(src) and os.path.getsize(src) > 0:
                z.write(src, arc)
                manifest["includes"].append(arc)
        z.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()


def _safe_extract_member(name):
    """拒绝绝对路径与 .. 逃逸（zip-slip 防护）。"""
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def import_bundle(data):
    """解析导入 zip 并写回 server 目录。"""
    if not data or data[:2] != b"PK":
        return {"ok": False, "error": "不是有效的 zip 文件"}
    ensure_dirs()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {"ok": False, "error": "zip 解析失败"}

    names = z.namelist()
    # 兼容两种布局：带 server/ 前缀，或扁平
    def find(base):
        for cand in ("server/" + base, base):
            if cand in names:
                return cand
        return None

    has_cfg = find("config.toml")
    has_db = find("network_control.db")
    if not has_cfg and not has_db:
        return {"ok": False, "error": "包内未找到 config.toml 或 network_control.db"}

    # 导入前备份现有 server 目录（便于回滚）
    backup = None
    if os.path.isdir(SERVER_DIR) and os.listdir(SERVER_DIR):
        backup = os.path.join(CACHE_DIR, "server.bak")
        try:
            if os.path.isdir(backup):
                shutil.rmtree(backup)
            shutil.copytree(SERVER_DIR, backup)
        except OSError:
            backup = None

    restored = []
    try:
        targets = ["config.toml", "network_control.db", "dns-ip.json", "mirror", "proxy"]
        # 老版本的导出包可能只带 settings.json、没有 config.toml：
        # 先把它落盘，随后的渲染会把它并进 config.toml 并删除该文件。
        if not find("config.toml"):
            targets.append("settings.json")
        for base in targets:
            arc = find(base)
            if not arc:
                continue
            safe = _safe_extract_member(arc)
            if not safe:
                continue
            dst = os.path.join(SERVER_DIR, os.path.basename(safe))
            with z.open(arc) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass
            restored.append(os.path.basename(dst))
    except (OSError, zipfile.BadZipFile) as e:
        if backup:
            try:
                shutil.rmtree(SERVER_DIR)
                shutil.copytree(backup, SERVER_DIR)
            except OSError:
                pass
        return {"ok": False, "error": "写入失败，已回滚: %s" % e}

    # 包内有 config.toml 就直接用它；只剩老 settings.json 的话，
    # render_config() 内部的迁移会把设置并进 config.toml 并删除旧文件。
    render_config()

    # 导入的 dns-ip.json 可能与导入前的出站互联不一致（域名少了、或全没了）。
    # 立刻同步一次让核心收敛到导入后的清单；否则旧地址会一直留到下一轮轮询。
    # 注意：包内不含 dns-ip.json 时，本机原有清单保持不变，同样做一次对账。
    try:
        sync_dns_peers(log_prefix="域名同步（导入后）")
    except Exception as e:  # noqa: BLE001
        log("导入后同步域名失败: %s" % e)

    return {"ok": True, "restored": restored, "backup": bool(backup)}


# ---------------------------------------------------------------------------
# 三协议独立端口（留空 = 复用 SERVICE_PORT）
# ---------------------------------------------------------------------------
def load_proto_ports():
    """读三协议独立端口。空串表示复用；文件缺失即全部复用。"""
    out = {"tcp_port": "", "quic_port": "", "ws_port": ""}
    raw = read_file(PROTO_PORTS_FILE).strip()
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except ValueError:
        return out
    if not isinstance(data, dict):
        return out
    for k in out:
        v = data.get(k)
        if v is None or str(v).strip() == "":
            continue
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 65535:
            out[k] = n
    return out


def save_proto_ports(proto):
    """保存三协议独立端口。空值直接省略（表示复用）。"""
    data = {}
    for k in ("tcp_port", "quic_port", "ws_port"):
        v = (proto or {}).get(k)
        if v is None or str(v).strip() == "":
            continue
        data[k] = int(str(v).strip())
    if data:
        write_file(PROTO_PORTS_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    else:
        _rm(PROTO_PORTS_FILE)
    return data


def _settings_with_proto_ports():
    """设置 + 独立端口，供渲染使用。

    独立端口的**意图**存在 proto-ports.json，不写进 config.toml
    （那里只有展开后的端口数字，反推不出用户是想复用还是想固定）。
    """
    s = C.load_settings(SERVER_DIR, PORT_FILE)
    s.update(load_proto_ports())
    return s


# ---------------------------------------------------------------------------
# 状态汇总
# ---------------------------------------------------------------------------
def status():
    ports = load_ports()
    running = core_running()
    s = get_settings()
    ver = installed_version()
    has_config = os.path.isfile(CONFIG_TOML)
    # 首次访问时配置可能尚未渲染，这里兜底生成，保证界面与导出都完整
    if not has_config:
        try:
            render_config()
            has_config = os.path.isfile(CONFIG_TOML)
        except Exception as e:  # noqa: BLE001
            log("状态查询时生成配置失败: %s" % e)
    return {
        "core_present": os.path.isfile(CORE_BIN),
        "core_running": bool(running),
        "core_pid": running,
        "core_version": ver,
        "latest_version": "",
        "arch": machine(),
        "arch_supported": bool(arch_triple()),
        "ports": ports,
        "log_dir": log_dir(),
        "log_default_dir": os.path.join(PKGVAR, "log"),
        "server_dir": SERVER_DIR,
        "has_config": has_config,
        "has_db": os.path.isfile(SERVER_DB),
        "path": {
            "bin": BIN_DIR,
            "server": SERVER_DIR,
            "cache": CACHE_DIR,
            "log": log_dir(),
        },
        "mirror": get_mirror(),
        "proxy": get_proxy(),
        "settings": {
            "network": s.get("network"),
            "username": s.get("username"),
            "white_list": s.get("white_list"),
        },
        "log_sizes": log_sizes(),
    }


# ---------------------------------------------------------------------------
# 后台守护：崩溃拉起 + 日志巡检
# ---------------------------------------------------------------------------
_watch_stop = threading.Event()


def _autostart_marker():
    return os.path.join(PKGVAR, ".autostart")


def watcher():
    while not _watch_stop.is_set():
        try:
            trim_logs()
            if os.path.isfile(_autostart_marker()) and os.path.isfile(CORE_BIN):
                if not core_running():
                    log("巡检发现核心未运行，尝试拉起")
                    start_core()
        except Exception as e:  # noqa: BLE001
            log("watcher 异常: %s" % e)
        _watch_stop.wait(10)


def mark_autostart(on=True):
    if on:
        open(_autostart_marker(), "w").close()
    else:
        _rm(_autostart_marker())


def bootstrap():
    ensure_dirs()
    get_panel_token()
    if not os.path.isfile(CONFIG_TOML):
        try:
            render_config()
        except Exception as e:  # noqa: BLE001
            log("初次生成配置失败: %s" % e)
    if not os.path.isfile(CORE_BIN):
        log("核心不存在，请在管理页下载")
        return
    ok, info = probe_core()
    if not ok:
        log("核心不可运行（%s），请在管理页重新下载" % info)
        return
    ok, msg = start_core()
    log("bootstrap: %s" % msg)
    if ok:
        mark_autostart(True)
        # 域名同步由 start_core() 内部负责，这里不再重复
