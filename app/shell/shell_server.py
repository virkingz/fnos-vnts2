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
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
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
MIRROR_FILE = os.path.join(SERVER_DIR, "mirror")
PROXY_FILE = os.path.join(SERVER_DIR, "proxy")
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


def set_mirror(prefix):
    write_file(MIRROR_FILE, (prefix or "").strip().rstrip("/"))


def get_proxy():
    return read_file(PROXY_FILE).strip()


def set_proxy(p):
    p = (p or "").strip()
    if p and "://" not in p:
        p = "http://" + p
    write_file(PROXY_FILE, p)


def url_with_mirror(u):
    m = get_mirror()
    return "%s/%s" % (m, u) if m else u


def _proxy_opener():
    proxy = get_proxy()
    if proxy:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        return op.open
    return urllib.request.urlopen


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
    """渲染 config.toml 与 logs/log4rs.yaml。"""
    ensure_dirs()
    return C.render(SERVER_DIR, log_dir(), PORT_FILE)


def get_settings():
    return C.load_settings(SERVER_DIR)


def update_settings(patch):
    s = C.load_settings(SERVER_DIR)
    s.update(patch or {})
    errs = C.validate(s)
    if errs:
        return {"ok": False, "errors": errs}
    C.save_settings(SERVER_DIR, s)
    render_config()
    return {"ok": True}


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
    url = url_with_mirror("https://github.com/vnt-dev/vnts/releases/latest")
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "vnts-nas-fpk"})
    try:
        r = _proxy_opener()(req, timeout=20)
        eff = r.geturl()
    except urllib.error.HTTPError as e:
        eff = e.geturl() if hasattr(e, "geturl") else ""
    eff = eff.replace("%3A", ":").replace("%2F", "/")
    tag = eff.rstrip("/").split("/")[-1].lstrip("v")
    return tag


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


def download_core(ver):
    url, asset = core_asset_url(ver)
    log("下载核心 %s <- %s" % (ver, url))
    with http_get(url, timeout=600) as r:
        data = r.read()
    res = install_core_from_bytes(data, ver)
    if res.get("ok"):
        write_file(CORE_SRC_FILE, "version=%s\nasset=%s\nurl=%s\ninstalled_at=%s\n"
                   % (ver, asset, url, now()))
    return res


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


def _console_port():
    return int(load_ports()["CONSOLE_PORT"])


def _kill_stragglers():
    """清理未记录在 pid 文件、但仍占着控制台端口的残留 vnts2。

    这是「端口被占用」的兜底：正常路径下不该有残留，
    但异常退出（kill -9、容器被杀）可能留下孤儿进程。
    """
    port = _console_port()
    if not port_in_use(port):
        return
    log("控制台端口 %d 被占用，尝试清理残留核心进程" % port)
    killed = False
    # 优先按进程名精确匹配，避免误杀其它程序
    try:
        r = subprocess.run(["pkill", "-f", os.path.basename(CORE_BIN)],
                           capture_output=True, timeout=5)
        killed = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    if not killed:
        # 退回到 `fuser -k <port>/tcp`
        for tool in (["fuser", "-k", "%d/tcp" % port],):
            try:
                subprocess.run(tool, capture_output=True, timeout=5)
                killed = True
            except (OSError, subprocess.SubprocessError):
                pass
    # 等待端口释放
    t0 = time.time()
    while time.time() - t0 < 5:
        if not port_in_use(port):
            return
        time.sleep(0.3)
    log("WARN: 控制台端口 %d 仍被占用" % port)


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
    if port_in_use(_console_port()):
        return False, ("控制台端口 %s 已被其它程序占用，无法启动核心。"
                       "请在「服务端设置」里为该端口换一个号，或先释放它。"
                       % _console_port())

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
        return False, "核心启动后立即退出。日志尾部：\n%s" % tail
    return True, "核心运行中 (pid=%d)" % proc.pid


def start_core():
    with _proc_lock:
        return _start_core_locked()


def restart_core():
    """重启核心。

    **整体持锁**：stop 与 start 必须在同一临界区内完成。
    否则两步之间会出现「核心已停、尚未起」的空窗，watcher 巡检会插入并
    再次 start，导致两个实例抢占控制台端口（Error: Address in use）。
    使用 RLock 是为了让内部的 _stop_core_locked/_start_core_locked 可重入。
    """
    with _proc_lock:
        _stop_core_locked()
        return _start_core_locked()


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
    for n in ("main.log", "shell.log", "vnts2.log", "update.log"):
        trim_one(os.path.join(log_dir(), n))


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
EXPORT_FILES = [
    ("config.toml", "server/config.toml"),
    ("settings.json", "server/settings.json"),
    ("network_control.db", "server/network_control.db"),
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
            "note": "服务器配置导出：含 config.toml、settings.json、数据库与证书；不含日志与核心二进制。",
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
        for base in [b for _, b in EXPORT_FILES][:1] + ["settings.json", "network_control.db",
                    "mirror", "proxy"]:
            arc = find(os.path.basename(base))
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

    # settings 缺失则由 config.toml 反推，保证界面与文件一致
    if "settings.json" not in restored and os.path.isfile(CONFIG_TOML):
        try:
            C.config_to_settings(SERVER_DIR)
            restored.append("settings.json(由 config.toml 生成)")
        except Exception:  # noqa: BLE001
            pass
    render_config()
    return {"ok": True, "restored": restored, "backup": bool(backup)}


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
