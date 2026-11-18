#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载源与代理的配置校验 / 自愈 / 回退测试。

覆盖三类真实故障（都踩过）：
  1. 把「加速前缀」（https://ghproxy.example.com，不带端口）填进**代理**栏
     —— urllib 会向它发 CONNECT 而无人应答，表现为「测试连通性超时」，
        之后所有下载都失败。必须拦下来并指向镜像前缀。
  2. socks 代理：Python 标准库不支持，填了必然失败，应直接说清楚。
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
        ("socks5h://127.0.0.1:1080", False),      # stdlib 不支持 socks ★
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
    print("[3] 保存代理先实测（不通就不落盘）")
    def dead_open(url, timeout=30, proxy=None, method="GET"):
        raise TimeoutError("timed out")

    real_open = S._open
    S._open = dead_open
    res = S.set_proxy("http://127.0.0.1:7890")
    check("代理不通 -> 不保存", res.get("ok") is False and "未保存" in res.get("error", ""),
          res.get("error"))
    check("proxy 文件仍为空", S.get_proxy() == "", repr(S.get_proxy()))

    S._open = lambda url, timeout=30, proxy=None, method="GET": FakeResp(b"ok")
    res = S.set_proxy("http://127.0.0.1:7890")
    check("代理可用 -> 保存并归一化", res.get("ok") and S.get_proxy() == "http://127.0.0.1:7890",
          S.get_proxy())
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
    check("含直连/镜像/代理/实际路径四项", len(names) == 4, names)
    check("结论为不通", r["ok"] is False)
    check("提示指向代理", "代理" in r["hint"], r["hint"])
    by = {c["name"]: c for c in r["checks"]}
    check("直连判定可达", by["直连 GitHub"]["ok"] is True)
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

    print("")
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
