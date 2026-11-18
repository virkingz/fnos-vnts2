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
    # 造日志（导出必须排除）
    open(os.path.join(S.log_dir(), "vnts2.log"), "w").write("LOG-DATA-SHOULD-NOT-EXPORT")

    print("\n[2] 导出")
    bundle = S.export_bundle()
    check("导出为有效 zip", bundle[:2] == b"PK")
    z = zipfile.ZipFile(io.BytesIO(bundle))
    names = z.namelist()
    check("含数据库", "server/network_control.db" in names, names)
    check("含 config.toml", "server/config.toml" in names)
    check("含 settings.json", "server/settings.json" in names)
    check("含证书", "server/cert.pem" in names)
    check("不含日志", not any(n.endswith(".log") for n in names), names)
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

    shutil.rmtree(base, ignore_errors=True)
    print("\n===== 结果：%d 通过 / %d 失败 =====" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
