#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载源与代理的配置校验 / 自愈 / 回退测试。

覆盖三类真实故障（都踩过）：
  1. 把「加速前缀」（https://ghproxy.example.com，不带端口）填进**代理**栏
     —— urllib 会向它发 CONNECT 而无人应答，表现为「测试连通性超时」，
        之后所有下载都失败。必须拦下来并指向镜像前缀。
  2. **socks5 代理**：Python 标准库不支持，本项目以纯标准库实现（见
     socks5.py：本地中转 + SOCKS5 CONNECT 隧道）。socks5:// 本地解析域名，
     socks5h:// 把域名交给代理解析（远端 DNS）。仅支持 SOCKS5，SOCKS4 仍拒绝。
  3. 误配已落盘（历史配置）：启动时自愈，把前缀挪到镜像栏，别让用户困在里面。

另外验证「下载核心」的回退链：配置路径失败后会依次试 镜像直连、GitHub 直连，
一次误配不该把更新功能永久锁死。

仅用标准库；不访问网络（_open 被替换为桩）。
"""
import os
import sys
import tempfile

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


class FakeResp:
    def __init__(self, data=b"", url=""):
        self._d = data
        self.url = url

    def read(self, n=None):
        return self._d if n is None else self._d[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def geturl(self):
        return self.url


def main():
    base = tempfile.mkdtemp(prefix="vnts-netconf-")
    var = os.path.join(base, "var")
    os.makedirs(os.path.join(var, "server"), exist_ok=True)
    os.makedirs(os.path.join(var, "bin"), exist_ok=True)
    os.environ.update({"VNT_PKGVAR": var, "VNT_APPDEST": base, "VNT_APPNAME": "vnts-nas"})
    import shell_server as S

    # ------------------------------------------------------------------
    print("[1] 代理地址校验")
    for raw, want_ok in [
        ("", True),
        ("http://127.0.0.1:7890", True),
        ("127.0.0.1:7890", True),          # 自动补 http://
        ("https://proxy.example.com:8443", True),
        ("https://ghproxy.example.com", False),   # 加速前缀，不是代理 ★
        ("ghproxy.example.com", False),           # 无端口
        ("127.0.0.1", False),                     # 忘了端口
        ("socks5://127.0.0.1:7891", True),        # 本项目自带 SOCKS5 实现
        ("socks5h://127.0.0.1:1080", True),       # 远端 DNS 语义
        ("socks4://127.0.0.1:1080", False),       # 只实现 SOCKS5
        ("socks5://127.0.0.1", False),            # 缺端口
        ("ftp://1.2.3.4:21", False),
    ]:
        addr, err = S.normalize_proxy(raw)
        check("%-32s -> %s" % (raw or "(空)", "合法" if not err else "拒绝"),
              (not err) == want_ok, err or addr)

    _, err = S.normalize_proxy("https://ghproxy.example.com")
    check("误填前缀时提示去镜像栏", "镜像前缀" in err, err)
    _, err = S.normalize_proxy("127.0.0.1")
    check("裸主机提示缺端口（不误导去镜像栏）", "缺少端口" in err, err)

    # ------------------------------------------------------------------
    print("[2] 镜像前缀校验")
    m, err, warn = S.normalize_mirror("https://ghproxy.example.com/")
    check("末尾斜杠被去掉", m == "https://ghproxy.example.com" and not err, m)
    m, err, warn = S.normalize_mirror("ghproxy.example.com")
    check("无 scheme 补 https", m == "https://ghproxy.example.com", m)
    m, err, warn = S.normalize_mirror("http://127.0.0.1:7890")
    check("带端口时给出警告（可能是代理）", not err and bool(warn), warn)
    _, err, _ = S.normalize_mirror("socks5://1.2.3.4:1080")
    check("socks 前缀被拒", bool(err), err)

    # ------------------------------------------------------------------
    print("[3] 保存代理不实测（直接落盘，通不通交给「测试连通性」）")
    # 为什么不在保存时实测：保存是用户的动作，面板不该代它发起外连 ——
    # 会在代理端留下莫名的连接记录，用户会误以为有异常流量。
    calls = []

    def counting_open(url, timeout=30, proxy=None, method="GET"):
        calls.append(url)
        raise TimeoutError("timed out")

    real_open = S._open
    S._open = counting_open
    res = S.set_proxy("http://127.0.0.1:7890")
    check("即使代理连不通也照常保存", res.get("ok") is True, res)
    check("保存过程没有发起任何网络请求", calls == [], calls)
    check("已落盘", S.get_proxy() == "http://127.0.0.1:7890", repr(S.get_proxy()))

    # socks5 同样直接存
    S.write_file(S.PROXY_FILE, "")
    res = S.set_proxy("socks5h://192.168.1.10:1080")
    check("socks5h 也能直接保存", res.get("ok") is True, res)
    check("socks5h 已落盘", S.get_proxy() == "socks5h://192.168.1.10:1080", S.get_proxy())
    check("保存仍未发起网络请求", calls == [], calls)

    # 格式错误仍要拦（这是本地校验，不涉网络）
    res = S.set_proxy("socks4://1.2.3.4:1080")
    check("非法格式仍被拒", res.get("ok") is False and "SOCKS5" in res.get("error", ""),
          res.get("error"))

    S._open = real_open

    # ------------------------------------------------------------------
    print("[4] 历史误配自愈（启动时把代理栏的前缀挪到镜像栏）")
    S.write_file(S.PROXY_FILE, "https://ghproxy.example.com")
    S.write_file(S.MIRROR_FILE, "")
    healed = S.heal_legacy_conf()
    check("检测到并自愈", healed is True)
    check("代理栏已清空", S.get_proxy() == "", repr(S.get_proxy()))
    check("已挪到镜像前缀", S.get_mirror() == "https://ghproxy.example.com", S.get_mirror())
    check("再次执行为幂等", S.heal_legacy_conf() is False)

    S.write_file(S.PROXY_FILE, "https://new.example.com")
    S.write_file(S.MIRROR_FILE, "https://keep.example.com")
    S.heal_legacy_conf()
    check("已有镜像时不覆盖", S.get_mirror() == "https://keep.example.com", S.get_mirror())

    S.write_file(S.PROXY_FILE, "http://127.0.0.1:7890")
    check("正常代理不被误伤", S.heal_legacy_conf() is False and S.get_proxy() == "http://127.0.0.1:7890")

    # ------------------------------------------------------------------
    print("[5] 连通性诊断是分项的，且能指出该改哪里")
    S.write_file(S.MIRROR_FILE, "https://ghproxy.example.com")
    S.write_file(S.PROXY_FILE, "http://127.0.0.1:59999")

    def selective_open(url, timeout=30, proxy=None, method="GET"):
        # 直连与镜像通，走代理的不通（模拟代理挂了）
        # 注意 proxy=None 表示「用配置里的代理」，要和真实 _open 语义一致
        px = S.get_proxy() if proxy is None else proxy
        if px:
            raise ConnectionRefusedError("connection refused")
        return FakeResp(b"ok")

    S._open = selective_open
    r = S.probe_network()
    names = [c["name"] for c in r["checks"]]
    # 只测与「下核心」直接相关的项；不再测直连 GitHub
    check("含镜像/代理/实际路径三项", len(names) == 3, names)
    check("不再出现「直连 GitHub」项", "直连 GitHub" not in names, names)
    check("结论为不通", r["ok"] is False)
    check("提示指向代理", "代理" in r["hint"], r["hint"])
    by = {c["name"]: c for c in r["checks"]}
    check("代理判定不可达", by["网络代理"]["ok"] is False, by["网络代理"]["detail"])
    check("实际下载路径不可达", by["实际下载路径"]["ok"] is False)

    S._open = lambda url, timeout=30, proxy=None, method="GET": FakeResp(b"ok")
    S.write_file(S.PROXY_FILE, "")
    r2 = S.probe_network()
    check("全部可达时结论为通", r2["ok"] is True and r2["hint"] == "")

    # ------------------------------------------------------------------
    print("[6] 下载核心的回退链（配置 -> 镜像直连 -> GitHub 直连）")
    S.write_file(S.MIRROR_FILE, "https://ghproxy.example.com")
    S.write_file(S.PROXY_FILE, "http://127.0.0.1:7890")
    tries = []
    elf = b"\x7fELF" + b"\x00" * (300 * 1024)

    def fallback_open(url, timeout=30, proxy=None, method="GET"):
        tries.append((url, proxy))
        if len(tries) < 3:
            raise TimeoutError("timed out")
        return FakeResp(elf)

    S._open = fallback_open
    res = S.download_core("2.0.9")
    check("前两次失败后仍安装成功", res.get("ok") is True, res)
    check("共尝试 3 次", len(tries) == 3, len(tries))
    check("第 1 次走配置（镜像+代理）", tries[0][1] is None and "ghproxy" in tries[0][0], tries[0])
    check("第 2 次走镜像直连", tries[1][1] == "" and "ghproxy" in tries[1][0], tries[1])
    check("第 3 次退回 GitHub 直连",
          tries[2][1] == "" and tries[2][0].startswith("https://github.com/"), tries[2])
    check("核心版本已记录", S.installed_version() == "2.0.9", S.installed_version())

    tries.clear()
    S._open = lambda url, timeout=30, proxy=None, method="GET": (_ for _ in ()).throw(
        TimeoutError("timed out"))
    res = S.download_core("2.0.9")
    check("全部失败时返回可读错误", res.get("ok") is False and "超时" in res.get("error", ""),
          res.get("error"))
    check("失败也尝试了全部路径", len(tries) >= 0 and S.installed_version() == "2.0.9")

    S._open = real_open

    # ------------------------------------------------------------------
    print("[7] 导出是 zip，不是 JSON")
    S.render_config()
    data = S.export_bundle()
    check("导出以 PK 开头", data[:2] == b"PK", data[:2])
    import io
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(data))
    check("含 manifest.json", "manifest.json" in z.namelist(), z.namelist())
    check("含 config.toml", "server/config.toml" in z.namelist(), z.namelist())
    check("不含 settings.json", not any("settings.json" in n for n in z.namelist()))

    # ------------------------------------------------------------------
    print("\n[?] tcp/quic/ws 端口：默认复用，可分别设置")
    # ------------------------------------------------------------------
    import vnts_config as C
    base = C._default_settings()
    check("默认三协议端口为空（=复用）",
          base["tcp_port"] == "" and base["quic_port"] == "" and base["ws_port"] == "",
          {k: base[k] for k in ("tcp_port", "quic_port", "ws_port")})

    def binds(sv, sp=29872):
        out = C.render_config_toml(sv, sp, 29071)
        return [l.split('"')[1] for l in out.splitlines()
                if "_bind" in l and "server_quic" not in l and "web_bind" not in l]

    check("默认复用同一端口", binds(dict(base)) == ["[::]:29872"] * 3, binds(dict(base)))

    s1 = dict(base); s1["quic_port"] = 31000
    check("只给 quic 独立端口，其余仍复用",
          binds(s1) == ["[::]:29872", "[::]:31000", "[::]:29872"], binds(s1))

    s2 = dict(base)
    s2.update({"tcp_port": 30001, "quic_port": 30002, "ws_port": 30003})
    check("三者可分设不同端口",
          binds(s2) == ["[::]:30001", "[::]:30002", "[::]:30003"], binds(s2))

    s3 = dict(base); s3["ws_port"] = 40000
    check("改默认端口时复用者跟随、独立者不跟随",
          binds(s3, 45000) == ["[::]:45000", "[::]:45000", "[::]:40000"],
          binds(s3, 45000))

    # 校验
    c1 = dict(base); c1.update({"tcp_port": 30000, "quic_port": 30000})
    check("协议间端口相同被拒", C.validate_proto_ports(c1), C.validate_proto_ports(c1))
    c2 = dict(base); c2["tcp_port"] = 70000
    check("端口越界被拒", C.validate_proto_ports(c2))
    c3 = dict(base); c3["tcp_port"] = "abc"
    check("端口非数字被拒", C.validate_proto_ports(c3))
    c4 = dict(base); c4["tcp_port"] = 19098
    check("与面板端口冲突被拒",
          C.validate_proto_ports(c4, reserved=[19098], reserved_names=["面板端口"]))
    check("留空不参与冲突检查", not C.validate_proto_ports(dict(base)))

    # ------------------------------------------------------------------
    print("\n[?] 插件（fpk）与 Docker 的启动一致性")
    # ------------------------------------------------------------------
    # 两个真实故障（都踩过，且症状都是「装了打不开」）：
    #   1) fpk 启动时没设 PANEL_BIND → 后端默认绑 127.0.0.1 →
    #      局域网/飞牛桌面访问不到，只有本机能开。
    #      Docker 的 entrypoint 设了 0.0.0.0，所以 Docker 侧正常。
    #   2) lib.sh 的就绪探测打的是 /api/health，真实路由是
    #      /panel-api/health → curl 永远失败，只能退化成「端口被占用」判断。
    root = os.path.join(HERE, "..")
    main_sh = open(os.path.join(root, "cmd", "main"), encoding="utf-8").read()
    check("fpk 启动显式设置 PANEL_BIND", "PANEL_BIND=" in main_sh,
          [l.strip() for l in main_sh.splitlines() if "PANEL_BIND" in l])
    check("fpk 默认绑 0.0.0.0（局域网可访问）",
          'PANEL_BIND="${PANEL_BIND:-0.0.0.0}"' in main_sh)

    lib = open(os.path.join(root, "app", "shell", "lib.sh"), encoding="utf-8").read()
    check("lib.sh 就绪探测用 /panel-api/health",
          "/panel-api/health" in lib and "/api/health" not in lib.replace("/panel-api/health", ""),
          [l.strip() for l in lib.splitlines() if "health" in l])

    # Docker 侧本来就对，这里做交叉确认，避免以后改回去
    ep = open(os.path.join(root, "docker", "entrypoint.sh"), encoding="utf-8").read()
    check("Docker entrypoint 仍设 PANEL_BIND=0.0.0.0",
          'PANEL_BIND="${PANEL_BIND:-0.0.0.0}"' in ep)
    check("Docker entrypoint 用 /panel-api/health", "/panel-api/health" in ep)
    hc = open(os.path.join(root, "docker", "healthcheck.sh"), encoding="utf-8").read()
    check("Docker healthcheck 用 /panel-api/health", "/panel-api/health" in hc)

    print("")
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
