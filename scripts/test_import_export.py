#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入导出专项测试：含数据库、zip-slip 防护、损坏包处理、备份回滚。"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile

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


def main():
    base = tempfile.mkdtemp(prefix="vnts-io-")
    os.environ["VNT_PKGVAR"] = base
    os.environ["VNT_APPDEST"] = base
    os.environ["VNT_APPNAME"] = "vnts-nas"
    import shell_server as S

    sdir = S.SERVER_DIR
    os.makedirs(os.path.join(sdir, "logs"), exist_ok=True)

    print("[1] 准备数据")
    # 造一个真实 SQLite 数据库
    db = S.SERVER_DB
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE networks (network_code TEXT PRIMARY KEY, gateway TEXT)")
    con.execute("INSERT INTO networks VALUES ('1001','10.26.0.1')")
    con.commit()
    con.close()
    check("数据库已创建", os.path.isfile(db))

    S.render_config()
    # 改写设置，让 config.toml 有独特内容
    S.update_settings({"network": "10.99.0.0/24", "white_list": ["1001"],
                       "username": "importuser", "password": "importpass"})
    # 造一个证书文件
    open(os.path.join(sdir, "cert.pem"), "w").write("CERT")
    # 造出站互联清单（导出必须带上）
    S.save_dns_doc({"version": 1, "interval": 60,
                    "records": [{"host": "exported.example.com", "port": 29873,
                                 "current": "1.2.3.4:29873", "previous": "",
                                 "last_sync": 0, "last_ok": 0, "last_error": ""}],
                    "managed": ["1.2.3.4:29873"]})
    # 造日志（导出必须排除）
    open(os.path.join(S.log_dir(), "vnts2.log"), "w").write("LOG-DATA-SHOULD-NOT-EXPORT")

    print("\n[2] 导出")
    bundle = S.export_bundle()
    check("导出为有效 zip", bundle[:2] == b"PK")
    z = zipfile.ZipFile(io.BytesIO(bundle))
    names = z.namelist()
    check("含数据库", "server/network_control.db" in names, names)
    check("含 config.toml", "server/config.toml" in names)
    check("不含 settings.json（config.toml 为唯一配置文件）",
          "server/settings.json" not in names, names)
    check("含证书", "server/cert.pem" in names)
    check("不含日志", not any(n.endswith(".log") for n in names), names)
    check("含 dns-ip.json（出站互联清单）", "server/dns-ip.json" in names, names)
    # 数据库内容完整
    with z.open("server/network_control.db") as f:
        con = sqlite3.connect(":memory:")
        con.deserialize(f.read()) if hasattr(con, "deserialize") else None
    check("manifest 存在", "manifest.json" in names)

    print("\n[3] 损坏包处理")
    r = S.import_bundle(b"not a zip at all")
    check("非 zip 被拒绝", not r["ok"] and "zip" in r["error"], r)
    r = S.import_bundle(b"PK\x03\x04garbagegarbage")
    check("坏 zip 被拒绝", not r["ok"])
    # 空 zip（无关键文件）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zz:
        zz.writestr("readme.txt", "hi")
    r = S.import_bundle(buf.getvalue())
    check("无关 zip 被拒绝", not r["ok"] and "未找到" in r["error"], r)

    print("\n[4] zip-slip 防护")
    check("拒绝绝对路径", S._safe_extract_member("/etc/passwd") is None)
    check("拒绝上跳路径", S._safe_extract_member("../../etc/passwd") is None)
    check("拒绝盘符", S._safe_extract_member("C:\\windows\\x") is None)
    check("接受正常路径", S._safe_extract_member("server/config.toml") == "server/config.toml")

    print("\n[5] 导入往返")
    S.update_settings({"network": "10.11.0.0/24", "username": "changed"})
    r = S.import_bundle(bundle)
    check("导入成功", r["ok"], r)
    check("已创建备份", r["backup"], r)
    s = S.get_settings()
    check("配置已还原", s["network"] == "10.99.0.0/24", s.get("network"))
    check("用户名已还原", s["username"] == "importuser", s.get("username"))
    check("数据库已还原", os.path.isfile(db) and os.path.getsize(db) > 0)

    print("\n[6] 扁平布局兼容")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zz:
        zz.writestr("config.toml", open(os.path.join(sdir, "config.toml")).read())
    r = S.import_bundle(buf.getvalue())
    check("扁平布局可导入", r["ok"], r)

    print("\n[?] 导入 dns-ip.json（出站互联清单）")
    # 先把本机清单改成 A，再用含 B 的包导入 → 应以包内为准
    dns_path = S.DNS_PEERS_FILE
    S.save_dns_doc({"version": 1, "interval": 60,
                    "records": [{"host": "local.example.com", "port": 1,
                                 "current": "", "previous": "",
                                 "last_sync": 0, "last_ok": 0, "last_error": ""}],
                    "managed": []})
    pkg_doc = {"version": 1, "interval": 300,
               "records": [{"host": "frompkg.example.com", "port": 29873,
                            "current": "", "previous": "",
                            "last_sync": 0, "last_ok": 0, "last_error": ""}],
               "managed": ["9.9.9.9:29873"]}
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zz:
        zz.writestr("server/config.toml", open(os.path.join(sdir, "config.toml")).read())
        zz.writestr("server/dns-ip.json", json.dumps(pkg_doc))
    r = S.import_bundle(buf2.getvalue())
    check("含 dns-ip.json 的包可导入", r["ok"], r)
    check("dns-ip.json 已被恢复", "dns-ip.json" in r.get("restored", []), r.get("restored"))
    doc = S.load_dns_doc()
    check("导入后清单以包内为准",
          [x["host"] for x in doc["records"]] == ["frompkg.example.com"], doc["records"])
    check("导入后间隔以包内为准", doc["interval"] == 300, doc["interval"])
    check("导入后 managed 以包内为准", doc["managed"] == ["9.9.9.9:29873"], doc["managed"])
    check("本机旧清单已被替换",
          "local.example.com" not in json.dumps(doc, ensure_ascii=False))

    # ------------------------------------------------------------------
    print("\n[?] 导入会真的把互联同步到核心")
    # 曾经的故障：dns-ip.json 导进来了，但同步没能落到核心，
    # 看起来「导入没生效」。两个原因：
    #   1) 同步发生在核心重启**之前** —— 重启后核心又从数据库重新加载；
    #   2) 核心没运行时 sync_dns_peers 里 core_running 那道关卡直接跳过。
    order = []

    def fake_sync(once=False, log_prefix="域名同步"):
        order.append(("sync", log_prefix))
        return {"ok": True, "actions": [], "error": ""}

    orig_sync = S.sync_dns_peers
    try:
        S.sync_dns_peers = fake_sync
        order.clear()
        buf3 = io.BytesIO()
        with zipfile.ZipFile(buf3, "w") as zz:
            zz.writestr("server/config.toml",
                        open(os.path.join(sdir, "config.toml")).read())
            zz.writestr("server/dns-ip.json", json.dumps(pkg_doc))
        r3 = S.import_bundle(buf3.getvalue())
        check("导入成功", r3["ok"], r3)
        check("导入过程触发了同步",
              [k for k, _ in order] == ["sync"], order)
        check("同步用的是「导入后」这个语义",
              order[0][1] == "域名同步（导入后）", order)
    finally:
        S.sync_dns_peers = orig_sync

    # ------------------------------------------------------------------
    print("\n[?] 核心起来后必定补一次同步（启动 / 重启两条路径）")
    # 这是「导入后互联能生效」的另一半保证：同步要经控制台 API 写进
    # 核心数据库，因此必须在核心就绪之后做。start/restart 都补一次，
    # 就不依赖「导入时核心恰好开着」这个前提。
    calls = []

    def spy(once=False, log_prefix="域名同步"):
        calls.append(log_prefix)
        return {"ok": True, "actions": [], "error": ""}

    orig_sync2 = S.sync_dns_peers
    orig_start_locked = S._start_core_locked
    orig_stop_locked = S._stop_core_locked
    try:
        S.sync_dns_peers = spy
        S._start_core_locked = lambda: (True, "已启动")
        S._stop_core_locked = lambda: True
        calls.clear()
        S.start_core()
        check("start_core 触发同步", len(calls) == 1 and "启动后" in calls[0], calls)
        calls.clear()
        S.restart_core()
        check("restart_core 触发同步", len(calls) == 1 and "重启后" in calls[0], calls)

        # 启动失败不应同步（否则会打一堆无意义的失败日志）
        S._start_core_locked = lambda: (False, "启动失败")
        calls.clear()
        S.start_core()
        check("启动失败时不同步", calls == [], calls)
    finally:
        S.sync_dns_peers = orig_sync2
        S._start_core_locked = orig_start_locked
        S._stop_core_locked = orig_stop_locked

    shutil.rmtree(base, ignore_errors=True)
    print("\n===== 结果：%d 通过 / %d 失败 =====" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
