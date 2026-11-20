#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试：令牌、网关前缀、导入导出、日志分离、控制台反代。"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_SHELL = os.path.join(HERE, "..", "app", "shell")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def get(url, headers=None, method="GET", data=None):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        r = op.open(req, timeout=8)
        return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return None, str(e).encode()


def main():
    base = tempfile.mkdtemp(prefix="vnts-test-")
    target = os.path.join(base, "target")
    var = os.path.join(base, "var")
    os.makedirs(var)
    shutil.copytree(SRC_SHELL, os.path.join(target, "shell"))

    env = dict(os.environ)
    env.update({
        "VNT_APPDEST": target, "VNT_PKGVAR": var, "VNT_PKGETC": os.path.join(base, "etc"),
        "VNT_PKGTMP": os.path.join(base, "tmp"), "VNT_APPNAME": "vnts-nas",
        "PANEL_BIND": "127.0.0.1",
    })
    # 不设 LOG_DIR：验证「默认日志在数据目录内、且可通过接口改到外部」的路径。
    # （设了 LOG_DIR 时接口会正确拒绝修改，另有专门断言。）
    env.pop("LOG_DIR", None)
    # 清除可能干扰的代理环境
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy"):
        env.pop(k, None)

    proc = subprocess.Popen([sys.executable, "shell_main.py"], cwd=os.path.join(target, "shell"),
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        port = 19098
        url = "http://127.0.0.1:%d" % port
        # 等就绪
        for _ in range(40):
            c, _b = get(url + "/panel-api/health")
            if c == 200:
                break
            time.sleep(0.5)

        print("\n[1] 首页与令牌注入")
        c, body = get(url + "/")
        html = body.decode("utf-8", "replace")
        check("首页可访问", c == 200)
        m = re.search(r'window\.__PANEL__=\{token:"([^"]+)"\}', html)
        check("令牌已注入首页", bool(m))
        check("<base> 已注入", '<base href="/">' in html)
        token = m.group(1) if m else ""

        # 管理页入口限制：只有 / 与真实存在的资源可访问，其余一律 404。
        # 背景：原先有「SPA 回退」——任何不像资源的路径都返回 index.html，
        # 于是 /test/test、/任意/乱写 都能打开管理界面（地址栏还是一串无关
        # 路径）。前端实际用 hash 路由（#overview / #core），path 层面从不
        # 导航，因此该回退没有合法用途，已去掉。
        print("\n[1b] 路径限制（非入口路径一律 404）")
        # 只列**仓库里确实跟踪**的首页资源。
        # 不断言 /icon.png：它是构建产物（.gitignore 忽略 app/ui/images/icon_*.png
        # 与 app/shell/web/icon.png），由 gen_icons.py / pack.sh 现场生成。
        # 而本测试把 app/shell 单独拷到临时目录，_static 回退用的
        # `web/../../ui/images/` 在那里并不存在 —— 干净检出下它必然 404，
        # 断言 200 会误报（CI 上确实这么红过一次）。图标另由冒烟测试覆盖。
        for okpath in ("/", "/index.html", "/app.js", "/app.css"):
            c, _ = get(url + okpath)
            check("合法路径 %s -> 200" % okpath, c == 200, c)
        for badpath in ("/test/test", "/foo", "/a/b/c", "/admin", "/foo/"):
            c, _ = get(url + badpath)
            check("非法路径 %s -> 404" % badpath, c == 404, c)

        print("\n[2] 令牌校验")
        c, _ = get(url + "/panel-api/state")
        check("无令牌 -> 401", c == 401, c)
        c, b = get(url + "/panel-api/state", {"X-Panel-Token": token})
        check("带令牌 -> 200", c == 200, c)
        state = json.loads(b.decode())["data"]
        check("日志目录独立于数据目录", state["log_dir"] != state["server_dir"],
              state["log_dir"])
        check("配置已生成", state["has_config"])

        print("\n[3] 网关前缀")
        c, body = get(url + "/app/vnts-nas/")
        check("网关首页可访问", c == 200)
        check("网关 base 正确", '<base href="/app/vnts-nas/">' in body.decode("utf-8", "replace"))
        c, b = get(url + "/app/vnts-nas/panel-api/state", {"X-Panel-Token": token})
        check("网关下 API 可用", c == 200, c)

        print("\n[4] 日志分离（导出不含日志）")
        c, zb = get(url + "/panel-api/config/export", {"X-Panel-Token": token})
        check("导出成功", c == 200 and zb[:2] == b"PK")
        z = zipfile.ZipFile(io_bytes(zb))
        names = z.namelist()
        check("导出含 config.toml", any("config.toml" in n for n in names), names)
        check("导出不含 settings.json（已并入 config.toml）",
              not any("settings.json" in n for n in names), names)
        check("导出不含日志", not any(n.endswith(".log") for n in names), names)
        check("导出不含核心二进制", not any("vnts2" in n and n.endswith("vnts2") for n in names))

        print("\n[5] 保存设置并回读")
        c, b = get(url + "/panel-api/settings", {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({
                       "network": "10.88.0.0/24",
                       "white_list": ["2001", "2002"],
                       "username": "boss", "password": "pw123456",
                   }).encode())
        check("保存设置成功", c == 200, b[:120])
        saved = json.loads(b.decode())["data"]
        check("保存不再自动重启核心（无 restarted 字段）", "restarted" not in saved, saved)
        c, b = get(url + "/panel-api/settings", {"X-Panel-Token": token})
        s = json.loads(b.decode())["data"]
        check("网段已保存", s["network"] == "10.88.0.0/24", s.get("network"))
        check("白名单已保存", s["white_list"] == ["2001", "2002"], s.get("white_list"))
        cfg = open(os.path.join(var, "server", "config.toml"), encoding="utf-8").read()
        check("config.toml 已同步网段", '10.88.0.0/24' in cfg)
        check("config.toml 已同步白名单", '"2001"' in cfg)
        check("server 目录只有一个配置文件（无 settings.json）",
              not os.path.exists(os.path.join(var, "server", "settings.json")),
              sorted(os.listdir(os.path.join(var, "server"))))

        print("\n[6] 日志默认位置与 log4rs 指向")
        check("默认日志目录 = 数据目录/log", state["log_dir"] == os.path.join(var, "log"),
              state["log_dir"])
        l4 = open(os.path.join(var, "server", "logs", "log4rs.yaml"), encoding="utf-8").read()
        check("log4rs 指向日志目录",
              state["log_dir"].replace("\\", "/") in l4.replace("\\", "/"))
        check("server/logs 内无日志文件",
              not any(x.endswith(".log") for x in os.listdir(os.path.join(var, "server", "logs"))))

        print("\n[7] 导入往返")
        c, b = get(url + "/panel-api/config/export", {"X-Panel-Token": token})
        bundle = b
        c, b = get(url + "/panel-api/settings", {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"network": "10.77.0.0/24"}).encode())
        c, b = get(url + "/panel-api/config/import", {"X-Panel-Token": token,
                   "Content-Type": "application/zip"}, "POST", bundle)
        check("导入成功", c == 200, b[:160])
        c, b = get(url + "/panel-api/settings", {"X-Panel-Token": token})
        s2 = json.loads(b.decode())["data"]
        check("导入后网段还原", s2["network"] == "10.88.0.0/24", s2.get("network"))

        print("\n[8] 日志目录可通过接口修改")
        newlog = os.path.join(base, "logs2")
        c, b = get(url + "/panel-api/logdir", {"X-Panel-Token": token,
                   "Content-Type": "application/json"}, "PUT",
                   json.dumps({"log_dir": newlog}).encode())
        check("修改日志目录成功", c == 200, b[:160])
        l4 = open(os.path.join(var, "server", "logs", "log4rs.yaml"), encoding="utf-8").read()
        check("log4rs 已跟随更新", newlog.replace("\\", "/") in l4.replace("\\", "/"))

        print("\n[9] 出站互联域名接口")
        c, b = get(url + "/panel-api/dns-peers", {"X-Panel-Token": token})
        st = json.loads(b.decode())["data"]
        check("GET 返回清单结构", "interval" in st and "records" in st,
              list(st.keys()))
        check("初始清单为空", st["count"] == 0 and st["enabled"] is False)

        # 非法地址必须被拦下，且不落盘
        c, b = get(url + "/panel-api/dns-peers",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"peers": "bad-no-port"}).encode())
        check("非法地址被拒", c == 400, b[:120])

        c, b = get(url + "/panel-api/dns-peers",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"peers": "peer1.example.com:29873",
                                      "interval": 5}).encode())
        check("间隔越界被拒", c == 400 and "10" in b.decode("utf-8", "replace"), b[:120])

        # 合法保存（核心未运行 → 同步会跳过，但清单要落盘）
        c, b = get(url + "/panel-api/dns-peers",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"peers": "peer1.example.com:29873\na.example.com:29874",
                                      "interval": 120}).encode())
        check("保存域名清单成功", c == 200, b[:160])
        st = json.loads(b.decode())["data"]["status"]
        check("清单条数为 2", st["count"] == 2, st)
        check("间隔已保存", st["interval"] == 120, st["interval"])
        check("域名未写进 config.toml",
              "peer1.example.com" not in
              open(os.path.join(var, "server", "config.toml"), encoding="utf-8").read())
        check("config.toml 里 peer_servers 为空",
              "peer_servers = []" in
              open(os.path.join(var, "server", "config.toml"), encoding="utf-8").read())

        dns_json = os.path.join(var, "server", "dns-ip.json")
        check("dns-ip.json 已落盘", os.path.isfile(dns_json))
        saved = json.load(open(dns_json, encoding="utf-8"))
        check("落盘的间隔正确", saved["interval"] == 120)
        check("落盘两条记录", len(saved["records"]) == 2)

        # 导出包必须带上 dns-ip.json
        c, b = get(url + "/panel-api/config/export", {"X-Panel-Token": token})
        import io as _io
        import zipfile as _zip
        names = _zip.ZipFile(_io.BytesIO(b)).namelist()
        check("导出包含 dns-ip.json", "server/dns-ip.json" in names, names)

        # 核心未运行时手动同步应给出明确提示而不是崩
        c, b = get(url + "/panel-api/dns-peers/sync",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "POST", b"{}")
        j = json.loads(b.decode())
        check("核心未运行时同步有明确反馈",
              c == 200 and j["data"]["ok"] is False, b[:160])

        # 清空清单 → 恢复「不轮询」状态
        # 注意：本用例里核心始终未运行，地址从未真正加进核心，因此 managed
        # 为空、pending_cleanup 为假 —— 这正是「没连上就没什么可撤」的正确表现。
        c, b = get(url + "/panel-api/dns-peers",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"peers": ""}).encode())
        st = json.loads(b.decode())["data"]["status"]
        check("清空后无域名记录", st["count"] == 0, st)
        check("清空后 enabled 为假（无待撤地址）",
              st["enabled"] is False and st["pending_cleanup"] is False, st)
        check("清空后 managed 为空", st["managed"] == [], st)

        # 重新只填一条 → 再清空，managed 仍应保持干净（核心没运行过）
        c, b = get(url + "/panel-api/dns-peers",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"peers": "peer1.example.com:29873"}).encode())
        st = json.loads(b.decode())["data"]["status"]
        check("重新只填一条后清单为 1", st["count"] == 1, st)

        c, b = get(url + "/panel-api/dns-peers",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"peers": ""}).encode())
        st = json.loads(b.decode())["data"]["status"]
        check("再次清空后状态一致", st["count"] == 0 and st["managed"] == [], st)

        print("\n[10] vnts2 日志级别与轮转接口")
        c, b = get(url + "/panel-api/logconf", {"X-Panel-Token": token})
        lc = json.loads(b.decode())["data"]
        check("GET 返回日志配置", lc["level"] == "info" and lc["max_kb"] == 10240,
              {k: lc[k] for k in ("level", "max_kb", "keep")})
        check("返回可选级别列表", "debug" in lc["levels"], lc.get("levels"))
        check("提供 512KB 档位", 512 in lc["presets_kb"], lc.get("presets_kb"))

        c, b = get(url + "/panel-api/logconf",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"level": "verbose"}).encode())
        check("非法级别被拒", c == 400 and "级别" in b.decode("utf-8", "replace"), b[:120])

        c, b = get(url + "/panel-api/logconf",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"max_kb": 10}).encode())
        check("低于下限被拒", c == 400, b[:120])

        # 关键：512KB 能保存并写进 log4rs.yaml
        c, b = get(url + "/panel-api/logconf",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"max_kb": 512}).encode())
        check("512KB 保存成功", c == 200, b[:160])
        l4 = open(os.path.join(var, "server", "logs", "log4rs.yaml"),
                  encoding="utf-8").read()
        check("yaml 里 limit 为 512 kb", "limit: 512 kb" in l4,
              [x for x in l4.splitlines() if "limit" in x])

        c, b = get(url + "/panel-api/logconf",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"level": "debug", "max_kb": 32768, "keep": 7}).encode())
        check("合法配置保存成功", c == 200, b[:160])
        l4 = open(os.path.join(var, "server", "logs", "log4rs.yaml"),
                  encoding="utf-8").read()
        check("log4rs.yaml 的 level 已更新", "level: debug" in l4)
        check("log4rs.yaml 的轮转大小已更新", "limit: 32 mb" in l4)
        check("log4rs.yaml 的保留份数已更新", "count: 7" in l4)

        c, b = get(url + "/panel-api/logconf", {"X-Panel-Token": token})
        check("回读为保存后的值",
              json.loads(b.decode())["data"]["max_kb"] == 32768, b[:160])

        print("\n[11] tcp/quic/ws 端口分设")
        s = json.loads(get(url + "/panel-api/settings",
                           {"X-Panel-Token": token})[1].decode())["data"]
        check("默认三协议端口为空（=复用）",
              str(s.get("tcp_port", "")) == "" and str(s.get("quic_port", "")) == "",
              {k: s.get(k) for k in ("tcp_port", "quic_port", "ws_port")})

        c, b = get(url + "/panel-api/settings",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"quic_port": "31000"}).encode())
        check("给 QUIC 设独立端口成功", c == 200, b[:200])
        cfg = open(os.path.join(var, "server", "config.toml"), encoding="utf-8").read()
        check("config.toml 里 quic_bind 用独立端口",
              "quic_bind" in cfg and "31000" in cfg,
              [x for x in cfg.splitlines() if "quic_bind" in x][:1])
        check("tcp/ws 仍用组网服务端口", "29872" in cfg)

        c, b = get(url + "/panel-api/settings",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"tcp_port": "31000"}).encode())
        check("协议间端口相同被拒", c == 400 and "独立端口" in b.decode("utf-8", "replace"),
              b[:200])

        c, b = get(url + "/panel-api/settings",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"tcp_port": "19098"}).encode())
        check("与面板端口冲突被拒", c == 400, b[:160])

        c, b = get(url + "/panel-api/settings",
                   {"X-Panel-Token": token, "Content-Type": "application/json"},
                   "PUT", json.dumps({"quic_port": "", "tcp_port": "",
                                      "ws_port": ""}).encode())
        check("可重置为全部复用", c == 200, b[:160])

        print("\n[12] 控制台反代（核心未运行）")
        c, b = get(url + "/console/")
        check("未运行时返回提示", c == 502 and ("控制台未运行" in b.decode("utf-8", "replace")), c)

        print("\n[13] 自检")

        c, b = get(url + "/panel-api/selfcheck", {"X-Panel-Token": token})
        check("自检可调用", c == 200, c)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(base, ignore_errors=True)

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (PASS, FAIL))
    return 1 if FAIL else 0


def io_bytes(b):
    import io
    return io.BytesIO(b)


if __name__ == "__main__":
    sys.exit(main())
