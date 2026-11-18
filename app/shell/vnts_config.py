#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vnts-nas 服务端配置读写。

职责：
  1. 读写 vnts2 的 config.toml —— **它是唯一的服务端配置文件**，
     管理页直接读写它，不再另存一份 settings.json。
  2. 预写 server/logs/log4rs.yaml，把 vnts2 自身日志指向独立日志目录
     —— 这是「日志与配置分离」的关键：日志写 LOG_DIR，绝不留在 server/。

vnts2 的日志路径规则（源码 utils/log.rs）：
  - 固定使用 CWD 下的 logs/ 目录，log_init("vnts2") 会创建
    logs/log4rs.yaml，并把 appender 路径设为 logs/vnts2.log；
  - 仅当 logs/log4rs.yaml 不存在时才创建。
因此我们以 server/ 为 CWD 启动 vnts2，并预写 logs/log4rs.yaml，
把 rolling_file 路径改到 LOG_DIR，即可让日志落到独立目录。
logs/ 目录里就只剩这份几百字节的 yaml（配置，可导出），没有日志数据。

命令行：
  render --server-dir DIR --log-dir DIR [--ports-file F]
  settings-get --server-dir DIR
  settings-set --server-dir DIR --json '{"...":...}' [--ports-file F]
  config-to-settings --server-dir DIR

仅使用 Python 标准库，兼容 Python 3.8+。
"""

import argparse
import json
import os
import re
import sys

# 旧版本把设置另存为 settings.json；现在 config.toml 是唯一真源。
# 这个常量只用于「一次性迁移」：把历史 settings.json 并回 config.toml 后删除。
LEGACY_SETTINGS_FILE = "settings.json"

# ---------------------------------------------------------------------------
# 默认设置
# ---------------------------------------------------------------------------
DEFAULTS = {
    # 服务端参数
    "network": "10.26.0.0/24",
    "lease_duration": 86400,
    "white_list": [],
    "custom_nets": {},          # {网络编号: 网段}
    "group_types": {},          # {网络编号: "Network"|"RemoteDesktop"}
    "persistence": True,
    # 监听开关（端口统一由 ports.env 的 SERVICE_PORT 决定）
    "enable_tcp": True,
    "enable_quic": True,
    "enable_wss": True,
    # 控制台管理员账号
    "username": "admin",
    "password": "admin",
    # 服务端互联（可选）
    "enable_peer": False,
    "peer_bind_port": 29873,
    # peer_servers 保留在建里只为兼容旧调用方，**渲染时恒写空数组**。
    # 出站互联的真实清单在 server/dns-ip.json（见 dns_peers.py）。
    "peer_servers": [],
    "server_token": "",
    # 订阅/数据面（可选，供服务端下发配置）
    "subscription_server": "",
    "traffic_servers": [],
}

DEMO_PASSWORD = "admin"


# ---------------------------------------------------------------------------
# 读写 config.toml —— 唯一的服务端配置文件
# ---------------------------------------------------------------------------
def config_path(server_dir):
    return os.path.join(server_dir, "config.toml")


def _default_settings():
    """DEFAULTS 的可变副本（列表/字典要深拷，避免调用方互相污染）。"""
    s = dict(DEFAULTS)
    s["white_list"] = list(DEFAULTS["white_list"])
    s["custom_nets"] = dict(DEFAULTS["custom_nets"])
    s["group_types"] = dict(DEFAULTS["group_types"])
    s["peer_servers"] = list(DEFAULTS["peer_servers"])
    s["traffic_servers"] = list(DEFAULTS["traffic_servers"])
    return s


def read_ports(ports_file=None):
    """从 ports.env 读 SERVICE_PORT / CONSOLE_PORT（缺省用官方默认值）。"""
    service_port, console_port = 29872, 29071
    if ports_file and os.path.isfile(ports_file):
        for line in open(ports_file, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("SERVICE_PORT="):
                try:
                    service_port = int(line.split("=", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("CONSOLE_PORT="):
                try:
                    console_port = int(line.split("=", 1)[1])
                except ValueError:
                    pass
    return service_port, console_port


def load_settings(server_dir):
    """解析 config.toml 得到界面用的设置字典；文件不存在则返回默认值。"""
    s = _default_settings()
    p = config_path(server_dir)
    if not os.path.isfile(p):
        return s
    try:
        text = open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return s
    _apply_toml(s, parse_toml_simple(text))
    return s


def save_settings(server_dir, s, ports_file=None):
    """把设置写回 config.toml。端口不落进设置里，渲染时从 ports.env 取。"""
    os.makedirs(server_dir, exist_ok=True)
    service_port, console_port = read_ports(ports_file)
    cfg = render_config_toml(s, service_port, console_port)
    p = config_path(server_dir)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(cfg)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def _migrate_legacy_settings(server_dir):
    """把旧版 settings.json 并回 config.toml 后删除，确保只剩一个配置文件。

    - 只有 settings.json、没有 config.toml → 用它生成 config.toml（不丢设置）
    - 两者都在 → config.toml 才是 vnts2 实际读的那份，直接删掉 settings.json
    删除永远是最后一步：config.toml 确认存在才动手。
    """
    legacy = os.path.join(server_dir, LEGACY_SETTINGS_FILE)
    if not os.path.isfile(legacy):
        return False
    try:
        if not os.path.isfile(config_path(server_dir)):
            raw = json.load(open(legacy, encoding="utf-8"))
            s = _default_settings()
            if isinstance(raw, dict):
                s.update(raw)
            save_settings(server_dir, s)
        if os.path.isfile(config_path(server_dir)):
            os.remove(legacy)
            return True
    except (OSError, ValueError):
        pass
    return False


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _is_ipv4_net(net):
    m = re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/(\d{1,2})$", str(net or ""))
    if not m:
        return False
    for i in range(1, 5):
        if int(m.group(i)) > 255:
            return False
    if not (0 <= int(m.group(5)) <= 32):
        return False
    return True


def validate(s):
    """返回错误信息列表，空列表表示通过。"""
    errs = []
    if not _is_ipv4_net(s.get("network", "")):
        errs.append("默认虚拟网段需为 IPv4 CIDR，如 10.26.0.0/24")
    for code, net in (s.get("custom_nets") or {}).items():
        if not _is_ipv4_net(net):
            errs.append("自定义网段 %s 无效（应为 IPv4 CIDR）" % code)
    try:
        ld = int(s.get("lease_duration", 0))
        if ld < 60:
            errs.append("IP 租约时长至少 60 秒")
    except (TypeError, ValueError):
        errs.append("IP 租约时长需为整数（秒）")
    if not str(s.get("username", "")).strip():
        errs.append("控制台用户名不能为空")
    if not str(s.get("password", "")):
        errs.append("控制台密码不能为空")
    if s.get("enable_peer"):
        try:
            pp = int(s.get("peer_bind_port", 0))
            if not (1 <= pp <= 65535):
                errs.append("服务端互联端口范围 1-65535")
        except (TypeError, ValueError):
            errs.append("服务端互联端口需为整数")
    for code in (s.get("group_types") or {}):
        if code in (s.get("custom_nets") or {}):
            pass  # 允许同时存在
    # 出站互联写入 SQLite，数据库只在 persistence=true 时初始化：
    # 关掉持久化后 add/remove_peer_server 会直接失败，域名轮询无从生效。
    # 因此只要配了出站互联域名，就必须保持持久化开启。
    # （peer_servers 由 dns_peers 托管，这里用 dns_hosts 传入的清单判断。）
    hosts = s.get("dns_hosts") or []
    if hosts and not s.get("persistence", True):
        errs.append("配置了出站互联域名时必须开启「数据持久化」（互联记录存在数据库中）")
    return errs


# ---------------------------------------------------------------------------
# TOML 渲染（手写，避免依赖 tomli-w；字段少且可控）
# ---------------------------------------------------------------------------
def _toml_escape(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _toml_str(v):
    return '"%s"' % _toml_escape(v)


def _toml_list(values):
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def render_config_toml(s, service_port, console_port):
    """生成 config.toml 文本。service_port 用于 tcp/quic/ws，console_port 为 web_bind。"""
    ip = "[::]"
    lines = []
    lines.append("# 由 vnts-nas 管理页生成，请勿手工编辑（会被覆盖）")
    lines.append("# 服务端参数请在管理页「服务端设置」中修改")
    lines.append("")

    if s.get("enable_tcp"):
        lines.append("# 绑定 tcp 地址，不写则不启用 tcp 服务")
        lines.append('tcp_bind = "%s:%d"' % (ip, service_port))
    if s.get("enable_quic"):
        lines.append("# 绑定 quic 地址，不写则不启用 quic 服务")
        lines.append('quic_bind = "%s:%d"' % (ip, service_port))
    if s.get("enable_wss"):
        lines.append("# 绑定 wss 地址，不写则不启用 wss 服务")
        lines.append('ws_bind = "%s:%d"' % (ip, service_port))

    lines.append("# 默认虚拟网段")
    lines.append("network = %s" % _toml_str(s.get("network", "10.26.0.0/24")))
    lines.append("# 网络编号白名单（空 = 允许任意编号加入）")
    lines.append("white_list = %s" % _toml_list(s.get("white_list") or []))
    lines.append("# IP 租约时长，单位秒")
    lines.append("lease_duration = %d" % int(s.get("lease_duration", 86400)))
    lines.append("# 控制台绑定地址（仅本机回环，由管理页反代对外）")
    lines.append('web_bind = "127.0.0.1:%d"' % console_port)
    lines.append("# 控制台登录用户名密码")
    lines.append("username = %s" % _toml_str(s.get("username", "admin")))
    lines.append("password = %s" % _toml_str(s.get("password", "admin")))
    lines.append("# 是否启用数据持久化（网络/设备记录写入 SQLite）")
    lines.append("persistence = %s" % ("true" if s.get("persistence", True) else "false"))
    lines.append("")

    # 服务端互联
    lines.append("# 服务端互联配置（可选）")
    if s.get("enable_peer"):
        lines.append('server_quic_bind = "%s:%d"' % (ip, int(s.get("peer_bind_port", 29873))))
        # 🔴 peer_servers 恒为空数组，**绝不写域名**：
        #    vnts2 用 `peer_addr.parse::<SocketAddr>()` 解析该字段，
        #    只接受字面 IP:port，写域名会每 3~10 秒无限重试并刷错误日志。
        #    出站互联改由管理页定时解析域名后经控制台 API 写入数据库
        #    （见 dns_peers.py），清单存在 server/dns-ip.json。
        lines.append("# 出站互联地址由管理页按域名自动同步（见 dns-ip.json），此处恒为空")
        lines.append("peer_servers = []")
        tok = str(s.get("server_token") or "")
        if tok:
            lines.append("server_token = %s" % _toml_str(tok))
    else:
        lines.append('# server_quic_bind = "[::]:29873"')
        lines.append('# peer_servers = []   # 出站互联请在管理页填写域名')
        lines.append('# server_token = "your-secret-token"')
    lines.append("")

    # 订阅/数据面（可选）
    sub = str(s.get("subscription_server") or "")
    traffic = s.get("traffic_servers") or []
    if sub or traffic:
        lines.append("[client_access]")
        lines.append("# 下发订阅中使用的控制端点")
        lines.append('subscription_server = %s' % _toml_str(sub))
        lines.append("# 默认数据面端点（空 = 使用订阅端点）")
        lines.append("traffic_servers = %s" % _toml_list(traffic))
        lines.append("")

    # 自定义网段
    lines.append("# 自定义虚拟网段 格式：网络编号 = \"网段\"")
    lines.append("[custom_nets]")
    for code in sorted((s.get("custom_nets") or {}).keys()):
        lines.append("%s = %s" % (_toml_str(code), _toml_str(s["custom_nets"][code])))
    lines.append("")

    # 组类型（远程连接组）
    gt = {k: v for k, v in (s.get("group_types") or {}).items() if v}
    if gt:
        lines.append("# 预配置组用途：Network（默认，组网设备组）/ RemoteDesktop（远程连接组）")
        lines.append("[group_types]")
        for code in sorted(gt.keys()):
            lines.append("%s = %s" % (_toml_str(code), _toml_str(gt[code])))
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# log4rs.yaml 渲染（日志分离核心）
# ---------------------------------------------------------------------------
def render_log4rs(log_dir, level="info", max_mb=10, keep=5):
    """生成 log4rs 配置：rolling_file 指向 log_dir/vnts2.log。

    使用绝对路径，因此无论 CWD 在哪，日志都落在 log_dir。
    """
    log_dir = os.path.abspath(log_dir)
    log_file = os.path.join(log_dir, "vnts2.log").replace("\\", "/")
    log_pattern = os.path.join(log_dir, "vnts2.{}.log").replace("\\", "/")
    return (
        "refresh_rate: 30 seconds\n"
        "appenders:\n"
        "  rolling_file:\n"
        "    kind: rolling_file\n"
        "    path: {log_file}\n"
        "    append: true\n"
        "    encoder:\n"
        '      pattern: "{{d}} [{{f}}:{{L}}] {{h({{l}})}} {{M}}:{{m}}{{n}}"\n'
        "    policy:\n"
        "      kind: compound\n"
        "      trigger:\n"
        "        kind: size\n"
        "        limit: {max_mb} mb\n"
        "      roller:\n"
        "        kind: fixed_window\n"
        "        pattern: {log_pattern}\n"
        "        base: 1\n"
        "        count: {keep}\n"
        "  console:\n"
        "    kind: console\n"
        "    encoder:\n"
        '      pattern: "{{d}} {{h({{l}})}} {{m}}{{n}}"\n'
        "root:\n"
        "  level: {level}\n"
        "  appenders:\n"
        "    - rolling_file\n"
        "    - console\n"
    ).format(
        log_file=log_file,
        log_pattern=log_pattern,
        max_mb=int(max_mb),
        keep=int(keep),
        level=level,
    )


# ---------------------------------------------------------------------------
# 渲染全部
# ---------------------------------------------------------------------------
def render(server_dir, log_dir, ports_file=None, settings=None, log_conf=None):
    """渲染 config.toml 与 logs/log4rs.yaml。

    settings 传入时用它（保存设置场景，避免「读—改—写」间的竞态）；
    否则从现有 config.toml 读。

    log_conf 控制核心日志：{"level","max_mb","keep"}。不传则用默认
    （info / 10MB / 5 份）—— 保持向后兼容，老调用方无需改动。
    """
    os.makedirs(server_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    # 一次性迁移：老版本的 settings.json 并进来后删除
    _migrate_legacy_settings(server_dir)

    s = settings if settings is not None else load_settings(server_dir)
    errs = validate(s)
    if errs:
        raise SystemExit("配置校验失败: " + "; ".join(errs))

    cfg_path = save_settings(server_dir, s, ports_file)

    # logs/log4rs.yaml —— 让 vnts2 把日志写进独立 LOG_DIR。
    # 级别/大小/份数由管理页配置（logconf.json），核心每 30 秒自动重读，
    # 因此改完不必重启核心。
    lc = log_conf or {}
    logs_dir = os.path.join(server_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    l4 = os.path.join(logs_dir, "log4rs.yaml")
    tmp = l4 + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render_log4rs(log_dir,
                              level=lc.get("level", "info"),
                              max_mb=lc.get("max_mb", 10),
                              keep=lc.get("keep", 5)))
    os.replace(tmp, l4)

    return cfg_path


# ---------------------------------------------------------------------------
# config.toml -> settings（界面读取的唯一入口）
# ---------------------------------------------------------------------------
def parse_toml_simple(text):
    """极简 TOML 解析：只支持本模块生成的形态（顶层键值 + 字符串数组 + 表）。"""
    result = {"__tables__": {}}
    cur_table = None
    for raw in text.splitlines():
        # 去掉行内注释（不在引号内的 #）
        line = _strip_inline_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            cur_table = _unquote(line[1:-1].strip())
            result["__tables__"].setdefault(cur_table, {})
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = _unquote(key.strip())
        val = val.strip()
        parsed = _parse_value(val)
        if cur_table:
            result["__tables__"][cur_table][key] = parsed
        else:
            result[key] = parsed
    return result


def _strip_inline_comment(line):
    out = []
    in_str = False
    esc = False
    for ch in line:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if ch == "#" and not in_str:
            break
        out.append(ch)
    return "".join(out)


def _parse_value(val):
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_unquote(x.strip()) for x in _split_top(inner)]
    if val.startswith('"') and val.endswith('"'):
        return _unquote(val)
    if val in ("true", "false"):
        return val == "true"
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def _split_top(inner):
    parts = []
    buf = []
    in_str = False
    for ch in inner:
        if ch == '"':
            in_str = not in_str
            buf.append(ch)
        elif ch == "," and not in_str:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _unquote(x):
    x = x.strip()
    if len(x) >= 2 and x[0] == '"' and x[-1] == '"':
        x = x[1:-1]
    return x.replace('\\"', '"').replace("\\\\", "\\")


def _apply_toml(s, t):
    """把 parse_toml_simple 的结果映射进设置字典（原地修改并返回）。"""
    if "network" in t:
        s["network"] = t["network"]
    if "lease_duration" in t:
        s["lease_duration"] = t["lease_duration"]
    if "white_list" in t:
        s["white_list"] = t["white_list"]
    if "username" in t:
        s["username"] = t["username"]
    if "password" in t:
        s["password"] = t["password"]
    if "persistence" in t:
        s["persistence"] = bool(t["persistence"])

    # 端口/开关：能解析出各自 bind 则视为启用
    s["enable_tcp"] = "tcp_bind" in t
    s["enable_quic"] = "quic_bind" in t
    s["enable_wss"] = "ws_bind" in t

    # 服务端互联
    if "server_quic_bind" in t:
        s["enable_peer"] = True
        s["peer_bind_port"] = _port_of(t["server_quic_bind"]) or s.get("peer_bind_port", 29873)
        s["server_token"] = t.get("server_token", "")
    else:
        s["enable_peer"] = False

    # peer_servers 不再是配置项：即便历史 config.toml 里残留域名，也不回灌到
    # 界面（由 dns-ip.json 托管出站互联）。这里显式清空，避免旧值复活后
    # 被 render 写回配置，重新踩「域名写进配置必然连不上」的坑。
    s["peer_servers"] = []

    tables = t.get("__tables__", {})
    if "custom_nets" in tables:
        s["custom_nets"] = dict(tables["custom_nets"])
    if "group_types" in tables:
        s["group_types"] = dict(tables["group_types"])
    if "client_access" in tables:
        ca = tables["client_access"]
        s["subscription_server"] = ca.get("subscription_server", "")
        s["traffic_servers"] = ca.get("traffic_servers", [])
    return s


def config_to_settings(server_dir):
    """兼容旧接口：config.toml 即唯一真源，等价于 load_settings。"""
    return load_settings(server_dir)


def _port_of(bind):
    m = re.search(r":(\d+)\s*$", str(bind))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="vnts_config")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render")
    r.add_argument("--server-dir", required=True)
    r.add_argument("--log-dir", required=True)
    r.add_argument("--ports-file", default=None)

    g = sub.add_parser("settings-get")
    g.add_argument("--server-dir", required=True)

    st = sub.add_parser("settings-set")
    st.add_argument("--server-dir", required=True)
    st.add_argument("--json", required=True)
    st.add_argument("--ports-file", default=None)

    c2s = sub.add_parser("config-to-settings")
    c2s.add_argument("--server-dir", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "render":
        p = render(args.server_dir, args.log_dir, args.ports_file)
        print(p)
        return 0
    if args.cmd == "settings-get":
        json.dump(load_settings(args.server_dir), sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    if args.cmd == "settings-set":
        s = load_settings(args.server_dir)
        incoming = json.loads(args.json)
        if not isinstance(incoming, dict):
            raise SystemExit("settings 必须是 JSON 对象")
        s.update(incoming)
        errs = validate(s)
        if errs:
            json.dump({"ok": False, "errors": errs}, sys.stdout, ensure_ascii=False)
            print()
            return 2
        save_settings(args.server_dir, s, args.ports_file)
        json.dump({"ok": True}, sys.stdout, ensure_ascii=False)
        print()
        return 0
    if args.cmd == "config-to-settings":
        # config.toml 已是唯一真源，这里只把映射结果打印出来（不写任何文件）
        json.dump(load_settings(args.server_dir), sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
