#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出站互联域名 → IP 自动同步的测试。

全部用桩：不访问网络、不启动核心、不执行真实 DNS。
被测的核心事实（均来自 vnts2 v2 分支源码）：
  - `peer_arr.parse::<SocketAddr>()` 只认字面 IP:port，域名必然失败；
  - 互联记录存 SQLite，核心启动时 load_all_peer_servers() 全部拉起；
  - 控制台 API：POST/GET /api/peer_servers、DELETE /api/peer_servers/{addr}；
  - add 时地址已存在会报「已存在」。

重点守护两条安全规则（写错会直接弄断线上互联）：
  1. 先加后删；
  2. 只删自己名下的地址。
"""
import json
import os
import socket
import sys
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "shell"))
import dns_peers as D  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------
class StubClient(object):
    """模拟 vnts2 控制台 API，记录调用顺序以便断言「先加后删」。"""

    def __init__(self, addrs=None, fail_add=False, fail_del=False, fail_list=False,
                 need_relogin=False):
        self.addrs = list(addrs or [])
        self.calls = []
        self.fail_add = fail_add
        self.fail_del = fail_del
        self.fail_list = fail_list
        self.need_relogin = need_relogin
        self.logins = 0

    def list_peers(self):
        if self.fail_list:
            return [], [], "控制台不可达"
        return list(self.addrs), list(self.addrs), ""

    def add_peer(self, a):
        self.calls.append(("add", a))
        if self.fail_add:
            return False, "添加失败"
        if a in self.addrs:
            return False, "已存在"
        self.addrs.append(a)
        return True, ""

    def del_peer(self, a):
        """删除地址。

        真实核心的 DELETE 是「按字符串删 SQLite」，删一个本就不存在的地址
        也会返回成功（HTTP 层不看 rows_affected）。这里照实还原：
        只有 fail_del 才失败，与地址是否存在无关。
        """
        self.calls.append(("del", a))
        if self.fail_del:
            return False, "删除失败"
        if a in self.addrs:
            self.addrs.remove(a)
        return True, ""


def gai_map(mapping):
    """构造一个假 getaddrinfo。mapping: {域名: IP 或 [IP,...]}"""
    def _gai(host, *a, **k):
        if host not in mapping:
            raise socket.gaierror("nxdomain: %s" % host)
        v = mapping[host]
        ips = v if isinstance(v, list) else [v]
        out = []
        for ip in ips:
            if ":" in ip:
                out.append((socket.AF_INET6, socket.SOCK_DGRAM, 17, "", (ip, 0, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_DGRAM, 17, "", (ip, 0)))
        return out
    return _gai


def rec(host="p.example.com", port=29873, cur="", prev="", managed=None):
    """构造一条记录。

    managed 表示「该地址此前是否已向核心登记过」（doc["managed"]）。
    新版对账以 managed 为判定基准 —— 因为 GET /api/peer_servers 只返回
    「当前连上的」地址，连不上的地址查不出来却真实存在于数据库中。
    已登记过的地址不再重复 add。
    """
    r = D.new_record(host, port)
    r["current"] = cur
    r["previous"] = prev
    r["_managed"] = managed
    return r


def doc_of(recs, managed=None):
    """把记录列表包成文档，并汇总出 managed 集合。

    未显式给出 managed 时，取每条记录的 current（模拟「这些地址都已登记」），
    这符合正常演进到该状态的真实情况。
    """
    if managed is None:
        managed = [r["current"] for r in recs if r.get("current")]
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in recs]
    return {"version": 1, "interval": 60, "records": clean, "managed": list(managed)}


# ===========================================================================
print("=" * 66)
print("[1] 地址解析与格式化")
# ===========================================================================
check("普通 host:port", D.split_host_port("a.com:29873") == ("a.com", 29873, ""))
check("IPv6 方括号", D.split_host_port("[2001:db8::1]:29873") == ("2001:db8::1", 29873, ""))
check("裸 IPv6 明确报错", "方括号" in D.split_host_port("2001:db8::1:29873")[2])
check("缺端口报错", "端口" in D.split_host_port("a.com")[2])
check("端口非数字报错", "数字" in D.split_host_port("a.com:abc")[2])
check("端口越界报错", "范围" in D.split_host_port("a.com:99999")[2])
check("IPv4 判别", D.is_ip_literal("1.2.3.4") is True)
check("IPv6 判别", D.is_ip_literal("2001:db8::1") is True)
check("域名非 IP", D.is_ip_literal("a.com") is False)
check("IPv6 格式化带方括号", D.format_addr("2001:db8::1", 29873) == "[2001:db8::1]:29873")
check("IPv4 格式化不带方括号", D.format_addr("1.2.3.4", 29873) == "1.2.3.4:29873")
check("IPv6 地址可回解析", D.parse_addr("[2001:db8::1]:29873") == ("2001:db8::1", 29873))
check("非法地址回解析为 None", D.parse_addr("nonsense") == (None, None))

# ===========================================================================
print("\n[2] DNS 解析：只取第一个 / 失败处理")
# ===========================================================================
check("字面 IP 直出不查 DNS", D.resolve_first("1.2.3.4", _getaddrinfo=gai_map({})) == ("1.2.3.4", ""))
check("IPv6 字面量直出", D.resolve_first("2001:db8::1", _getaddrinfo=gai_map({}))[0] == "2001:db8::1")
ip, err = D.resolve_first("x.com", _getaddrinfo=gai_map({"x.com": ["1.1.1.1", "2.2.2.2"]}))
check("多 IP 只取第一个", ip == "1.1.1.1" and not err, ip)
ip, err = D.resolve_first("bad.com", _getaddrinfo=gai_map({}))
check("解析失败返回错误", ip == "" and "DNS" in err, err)
check("解析失败不抛异常", True)

# ===========================================================================
print("\n[3] dns-ip.json 读写往返与容错")
# ===========================================================================
tmp = tempfile.mkdtemp(prefix="vnts-dns-")
path = os.path.join(tmp, "dns-ip.json")
doc = {"version": 1, "interval": 120, "records": [
    {"host": "a.com", "port": 29873, "current": "1.2.3.4:29873", "previous": "5.6.7.8:29873",
     "last_sync": 111, "last_ok": 222, "last_error": ""}]}
D.save_doc(path, doc)
back = D.load_doc(path)
check("间隔往返", back["interval"] == 120, back["interval"])
check("记录数往返", len(back["records"]) == 1)
check("current 往返", back["records"][0]["current"] == "1.2.3.4:29873")
check("时间戳往返", back["records"][0]["last_sync"] == 111)
check("文件缺失走默认", D.load_doc(os.path.join(tmp, "nope.json"))["interval"] == 60)

with open(path, "w", encoding="utf-8") as f:
    f.write("{ 这不是 json")
check("文件损坏不抛异常", D.load_doc(path)["interval"] == 60)

with open(path, "w", encoding="utf-8") as f:
    json.dump({"interval": "abc", "records": [{"host": "a.com", "port": 1},
                                             {"host": "", "port": 2}, "junk"]}, f)
d = D.load_doc(path)
check("非法间隔回落默认", d["interval"] == 60)
check("丢弃非法记录，保留合法的", len(d["records"]) == 1 and d["records"][0]["host"] == "a.com")

# ===========================================================================
print("\n[4] 间隔校验")
# ===========================================================================
check("下界 10 秒", D.normalize_interval(9)[1] != "" and D.normalize_interval(10)[1] == "")
check("上界 86400 秒", D.normalize_interval(86401)[1] != "")
check("非数字被拒", D.normalize_interval("x")[1] != "")
check("默认 60", D.DEFAULT_INTERVAL == 60)

# ===========================================================================
print("\n[5] 清单解析")
# ===========================================================================
recs, errs = D.parse_lines("a.com:1\nb.com:2\n# 注释\n\n")
check("多行解析", [r["host"] for r in recs] == ["a.com", "b.com"] and not errs)
recs, errs = D.parse_lines("a.com:1,b.com:2")
check("逗号分隔", len(recs) == 2)
recs, errs = D.parse_lines("a.com:1\na.com:1")
check("重复项去重", len(recs) == 1)
recs, errs = D.parse_lines("good.com:1\nbad\n[::1]:2")
check("非法行收集错误", len(recs) == 2 and len(errs) == 1, errs)

old = [{"host": "a.com", "port": 1, "current": "9.9.9.9:1", "previous": "",
        "last_sync": 7, "last_ok": 7, "last_error": ""}]
new, _ = D.parse_lines("a.com:1\nb.com:2")
merged = D.merge_records(old, new)
by = {r["host"]: r for r in merged}
check("同名域保留解析状态", by["a.com"]["current"] == "9.9.9.9:1" and by["a.com"]["last_sync"] == 7)
check("新增域状态为空", by["b.com"]["current"] == "")

# ===========================================================================
print("\n[6] 同步：IP 变化 → 先加后删（最关键）")
# ===========================================================================
d = doc_of([rec(cur="9.9.9.9:29873")])
c = StubClient(["9.9.9.9:29873"])
acts = D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("调用顺序为先 add 后 del",
      c.calls == [("add", "1.2.3.4:29873"), ("del", "9.9.9.9:29873")], c.calls)
check("记录更新为新地址", d["records"][0]["current"] == "1.2.3.4:29873")
check("旧地址记入 previous", d["records"][0]["previous"] == "9.9.9.9:29873")
check("动作含 add 与 del",
      sorted(a["action"] for a in acts) == ["add", "del"], acts)
check("最终列表只剩新地址", c.addrs == ["1.2.3.4:29873"], c.addrs)

# ===========================================================================
print("\n[7] 同步：IP 未变 → 零写操作")
# ===========================================================================
d = doc_of([rec(cur="1.2.3.4:29873")])
c = StubClient(["1.2.3.4:29873"])
acts = D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("无 add/del 调用", c.calls == [], c.calls)
check("动作为 keep", acts[0]["action"] == "keep")
check("last_sync 被刷新", d["records"][0]["last_sync"] > 0)

# ===========================================================================
print("\n[8] 同步：解析失败 → 保留旧地址")
# ===========================================================================
d = doc_of([rec(cur="9.9.9.9:29873")])
c = StubClient(["9.9.9.9:29873"])
acts = D.sync_once(d, c, _getaddrinfo=gai_map({}))
check("不做任何增删", c.calls == [], c.calls)
check("旧地址保持", d["records"][0]["current"] == "9.9.9.9:29873")
check("记录错误信息", "DNS" in d["records"][0]["last_error"])
check("动作为 error", acts[0]["action"] == "error")

# ===========================================================================
print("\n[9] 同步：添加失败 → 绝不删旧")
# ===========================================================================
d = doc_of([rec(cur="9.9.9.9:29873")])
c = StubClient(["9.9.9.9:29873"], fail_add=True)
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("尝试过 add", ("add", "1.2.3.4:29873") in c.calls, c.calls)
check("旧地址仍在（互联未断）",
      "9.9.9.9:29873" in c.addrs and d["records"][0]["current"] == "9.9.9.9:29873")
check("记录了错误", bool(d["records"][0]["last_error"]))

# ===========================================================================
print("\n[10] 同步：以清单为准，清理一切多余地址")
# ===========================================================================
# 语义：dns-ip.json 是唯一真源。核心出站列表里凡不在清单中的地址都要清掉，
# 包括在控制台手工添加的 —— 这是「先加后删中途重启留下多余记录」的收敛手段。
d = doc_of([rec(cur="9.9.9.9:29873")])
c = StubClient(["9.9.9.9:29873", "8.8.8.8:9999"])   # 后者是控制台手工加的
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("最终只剩清单要求的地址", c.addrs == ["1.2.3.4:29873"], c.addrs)
check("手工加的地址被清理", "8.8.8.8:9999" not in c.addrs)
check("自己的旧地址被替换", "9.9.9.9:29873" not in c.addrs)

# 场景：上一轮「已加新、未删旧」就重启核心 → 旧 IP 残留在列表里
d = doc_of([rec(cur="1.2.3.4:29873")])          # 记录里已是新 IP
c = StubClient(["1.2.3.4:29873", "9.9.9.9:29873"])   # 旧 IP 还赖着
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("残留的旧 IP 被清理（核心重启后的收敛）",
      c.addrs == ["1.2.3.4:29873"], c.addrs)
check("清理时不会重复 add 目标地址",
      [x for x in c.calls if x[0] == "add"] == [], c.calls)

# 场景：清单里两条域名，核心里有第三条 → 只清第三条
d = doc_of([rec("a.com", cur="1.1.1.1:1"), rec("b.com", cur="2.2.2.2:2")])
d["records"][0]["port"] = 1
d["records"][1]["port"] = 2
c = StubClient(["1.1.1.1:1", "2.2.2.2:2", "7.7.7.7:7"])
D.sync_once(d, c, _getaddrinfo=gai_map({"a.com": "1.1.1.1", "b.com": "2.2.2.2"}))
check("只清理不在清单中的那条",
      sorted(c.addrs) == ["1.1.1.1:1", "2.2.2.2:2"], c.addrs)
check("清理动作被记录",
      any(a["action"] == "del" and a["addr"] == "7.7.7.7:7" for a in
          D.sync_once(doc_of([rec("a.com", cur="1.1.1.1:1")]),
                      StubClient(["1.1.1.1:1", "7.7.7.7:7"]),
                      _getaddrinfo=gai_map({"a.com": "1.1.1.1"}))))

# 场景：域名解析失败时，其旧地址必须被保护，不能当残留清掉
d = doc_of([rec(cur="9.9.9.9:29873")])
c = StubClient(["9.9.9.9:29873", "7.7.7.7:7"])
D.sync_once(d, c, _getaddrinfo=gai_map({}))          # 解析失败
check("解析失败的旧地址受保护", "9.9.9.9:29873" in c.addrs, c.addrs)
check("但无关残留仍被清理", "7.7.7.7:7" not in c.addrs, c.addrs)

# ===========================================================================
print("\n[11] 同步：自愈（记录被手工删除后自动补回）")
# ===========================================================================
# 场景：IP 没变，但这条地址被人从控制台手工删掉了。
# 判定以「核心实际列表」为准，因此应当自动补回。
d = doc_of([rec(cur="1.2.3.4:29873")])
c = StubClient([])                                   # 核心列表里已被清空
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("被删掉后自动补回", c.calls == [("add", "1.2.3.4:29873")], c.calls)
check("记录仍为当前 IP", d["records"][0]["current"] == "1.2.3.4:29873")
check("补回后不再有多余动作",
      D.sync_once(d, StubClient(["1.2.3.4:29873"]),
                  _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))[0]["action"] == "keep")

# 场景：IP 变了且旧地址已被手工删除 → 只补新，不报删除失败
d = doc_of([rec(cur="9.9.9.9:29873")])
c = StubClient([])                                   # 旧地址也已被删
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("旧地址已不在时补新，并对旧地址发一次幂等删除",
      c.calls == [("add", "1.2.3.4:29873"), ("del", "9.9.9.9:29873")], c.calls)
check("删除是幂等的（核心对不存在的地址也返回成功）", c.addrs == ["1.2.3.4:29873"], c.addrs)
check("记录迁移到新 IP", d["records"][0]["current"] == "1.2.3.4:29873")

# ===========================================================================
print("\n[12] 同步：首次添加（无旧地址）")
# ===========================================================================
d = doc_of([rec(cur="")])
c = StubClient([])
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("只 add 一次", c.calls == [("add", "1.2.3.4:29873")], c.calls)
check("current 写入", d["records"][0]["current"] == "1.2.3.4:29873")

# ===========================================================================
print("\n[13] 同步：目标已在列表则不重复添加")
# ===========================================================================
# 场景：记录的 current 还是空的（比如上次加到一半退出了），但目标 IP
# 其实已经在核心列表里 → 直接认领，不要重复 add（否则核心会报「已存在」）。
d = doc_of([rec(cur="")])
c = StubClient(["1.2.3.4:29873"])
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("不重复 add", c.calls == [], c.calls)
check("current 被认领", d["records"][0]["current"] == "1.2.3.4:29873")

# ===========================================================================
print("\n[14] 同步：控制台不可达")
# ===========================================================================
d = doc_of([rec(cur="9.9.9.9:29873")])
c = StubClient(["9.9.9.9:29873"], fail_list=True)
acts = D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("无增删动作", c.calls == [])
check("旧地址保持", d["records"][0]["current"] == "9.9.9.9:29873")
check("动作为 error", acts[0]["action"] == "error")

# ===========================================================================
print("\n[15] 同步：IPv6")
# ===========================================================================
d = doc_of([rec(host="v6.example.com", cur="")])
c = StubClient([])
D.sync_once(d, c, _getaddrinfo=gai_map({"v6.example.com": "2001:db8::1"}))
check("IPv6 地址带方括号", c.calls == [("add", "[2001:db8::1]:29873")], c.calls)
check("IPv6 记录写入", d["records"][0]["current"] == "[2001:db8::1]:29873")

d = doc_of([rec(host="v6.example.com", cur="[2001:db8::1]:29873")])
c = StubClient(["[2001:db8::1]:29873"])
D.sync_once(d, c, _getaddrinfo=gai_map({"v6.example.com": "2001:db8::2"}))
check("IPv6 变化时先加后删",
      c.calls == [("add", "[2001:db8::2]:29873"), ("del", "[2001:db8::1]:29873")], c.calls)

# ===========================================================================
print("\n[16] 同步：多条记录互相独立")
# ===========================================================================
d = doc_of([rec("a.com", cur="1.1.1.1:1"), rec("b.com", cur="2.2.2.2:2")])
d["records"][0]["port"] = 1
d["records"][1]["port"] = 2
c = StubClient(["1.1.1.1:1", "2.2.2.2:2"])
D.sync_once(d, c, _getaddrinfo=gai_map({"a.com": "1.1.1.1", "b.com": "9.9.9.9"}))
check("变化的那条被更新", d["records"][1]["current"] == "9.9.9.9:2")
check("未变的那条不动", d["records"][0]["current"] == "1.1.1.1:1")
check("只对新地址做了操作", c.calls == [("add", "9.9.9.9:2"), ("del", "2.2.2.2:2")], c.calls)

# ===========================================================================
print("\n[17] 状态视图")
# ===========================================================================
st = D.status_view({"interval": 30, "records": [rec(cur="1.2.3.4:29873")]})
check("含间隔", st["interval"] == 30)
check("enabled 为真", st["enabled"] is True)
check("条目含 addr", st["records"][0]["addr"] == "p.example.com:29873")
check("空清单 enabled 为假", D.status_view(doc_of([]))["enabled"] is False)

# ===========================================================================
print("\n[18] 配置层：peer_servers 恒空")
# ===========================================================================
import vnts_config as C  # noqa: E402

s = C._default_settings()
s["enable_peer"] = True
s["peer_servers"] = ["old.example.com:29873"]        # 故意塞进去
out = C.render_config_toml(s, 29872, 29071)
check("配置里 peer_servers 为空数组", "peer_servers = []" in out)
check("域名未出现在配置中", "old.example.com" not in out)
check("说明文字存在", "dns-ip.json" in out)

s2 = C._default_settings()
s2["enable_peer"] = False
out2 = C.render_config_toml(s2, 29872, 29071)
check("未启用互联时也不会写出域名", "old.example.com" not in out2)

parsed = C.parse_toml_simple(out)
s3 = C._default_settings()
C._apply_toml(s3, parsed)
check("回读时 peer_servers 被清空", s3["peer_servers"] == [], s3["peer_servers"])

# 历史 config.toml 带域名 → 回读也不复活
legacy = 'peer_servers = ["hack.example.com:29873"]\nserver_quic_bind = "[::]:29873"\n'
s4 = C._default_settings()
s4["peer_servers"] = []
C._apply_toml(s4, C.parse_toml_simple(legacy))
check("历史域名不会被回灌", s4["peer_servers"] == [], s4["peer_servers"])

# ===========================================================================
print("\n[19] 配置层：域名 + 关闭持久化的校验")
# ===========================================================================
s = C._default_settings()
s["dns_hosts"] = ["a.com:1"]
s["persistence"] = False
errs = C.validate(s)
check("拦截：有域名却关了持久化", any("持久化" in e for e in errs), errs)
s["persistence"] = True
check("开着持久化则通过", not any("持久化" in e for e in C.validate(s)))
s["dns_hosts"] = []
s["persistence"] = False
check("无域名时允许关持久化", not any("持久化" in e for e in C.validate(s)))

# ===========================================================================
print("\n[20] 从有到无：清空清单必须撤下已连接的地址")
# ===========================================================================
# 这是本轮修的 bug：清单从有变无时，之前加过的地址没人删，一直连着。
# 关键是要能区分「我们加过的」与「用户手工加的」—— 靠 doc["managed"]。

# 场景：清单清空，managed 里记着我们加过的地址 → 必须撤下
d = {"records": [], "managed": ["1.2.3.4:29873"]}
c = StubClient(["1.2.3.4:29873"])
acts = D.sync_once(d, c, _getaddrinfo=gai_map({}))
check("清空后撤下了托管地址", c.calls == [("del", "1.2.3.4:29873")], c.calls)
check("核心列表已清空", c.addrs == [], c.addrs)
check("动作标记为 del",
      any(a["action"] == "del" and a["addr"] == "1.2.3.4:29873" for a in acts), acts)
check("撤下后 managed 归空", d["managed"] == [], d["managed"])

# 场景：清空清单时必须保留用户手工加的地址
d = {"records": [], "managed": ["1.2.3.4:29873"]}
c = StubClient(["1.2.3.4:29873", "8.8.8.8:9999"])   # 后者是用户手工加的
D.sync_once(d, c, _getaddrinfo=gai_map({}))
check("以本页配置为准：手工加的也撤下", c.addrs == [], c.addrs)

# 场景：托管地址有多个，全部撤下
d = {"records": [], "managed": ["1.2.3.4:1", "5.6.7.8:2"]}
c = StubClient(["1.2.3.4:1", "5.6.7.8:2"])
D.sync_once(d, c, _getaddrinfo=gai_map({}))
check("多个托管地址全部撤下", c.addrs == [], c.addrs)
check("managed 清空", d["managed"] == [])

# 场景：托管地址已不在核心列表里（早被手工删过）→ 不报错，直接认账
d = {"records": [], "managed": ["1.2.3.4:29873"]}
c = StubClient([])
acts = D.sync_once(d, c, _getaddrinfo=gai_map({}))
check("对已不存在的托管地址仍发一次幂等删除",
      c.calls == [("del", "1.2.3.4:29873")], c.calls)
check("managed 照样归空", d["managed"] == [])
check("不产生错误动作（删除幂等成功）",
      [a for a in acts if a["action"] == "error"] == [], acts)

# 场景：撤下失败 → managed 保留，下轮继续重试（不能丢）
d = {"records": [], "managed": ["1.2.3.4:29873"]}
c = StubClient(["1.2.3.4:29873"], fail_del=True)
acts = D.sync_once(d, c, _getaddrinfo=gai_map({}))
check("删除失败时 managed 保留待重试", d["managed"] == ["1.2.3.4:29873"], d["managed"])
check("失败被记为 error", any(a["action"] == "error" for a in acts), acts)
d2 = {"records": [], "managed": ["1.2.3.4:29873"]}
c2 = StubClient(["1.2.3.4:29873"])
D.sync_once(d2, c2, _getaddrinfo=gai_map({}))
check("下一轮重试成功", c2.addrs == [] and d2["managed"] == [])

# 场景：曾托管、但核心未运行 → 不该误清 managed（下轮核心起来再撤）
check("空清单 + 空 managed 才算真正的 skipped",
      D.status_view({"records": [], "managed": []})["enabled"] is False)
check("空清单但有 managed → enabled 为真",
      D.status_view({"records": [], "managed": ["1.2.3.4:1"]})["enabled"] is True)
st = D.status_view({"records": [], "managed": ["1.2.3.4:1"]})
check("标记为待清理", st["pending_cleanup"] is True and st["managed"] == ["1.2.3.4:1"])

# 场景：正常同步时 managed 应记录当前目标地址
d = {"records": [rec(cur="")], "managed": []}
c = StubClient([])
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("同步后 managed 记录目标地址", d["managed"] == ["1.2.3.4:29873"], d["managed"])

# 场景：IP 变化后 managed 应只剩新地址（旧的不再托管）
d = {"records": [rec(cur="9.9.9.9:29873")], "managed": ["9.9.9.9:29873"]}
c = StubClient(["9.9.9.9:29873"])
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("managed 只剩新地址", d["managed"] == ["1.2.3.4:29873"], d["managed"])

# 场景：managed 往返持久化
p2 = os.path.join(tmp, "dns-mgd.json")
D.save_doc(p2, {"version": 1, "interval": 60, "records": [], "managed": ["1.2.3.4:1"]})
check("managed 可持久化并读回", D.load_doc(p2)["managed"] == ["1.2.3.4:1"])
check("旧文件无 managed 时回默认空列表",
      D.load_doc(os.path.join(tmp, "nope2.json"))["managed"] == [])

print("\n[21] 连不上的地址：GET 查不到，也必须能管住")
# ===========================================================================
# 本轮的核心修复。控制台 GET /api/peer_servers 只返回**当前连上的**地址，
# 而 add/DELETE 都是直接读写 SQLite。因此一个永远连不上的地址（域名错、
# 对端没开、端口不通）会：写进库却从不出现在 GET 里。
# 修正前：对账以 GET 为准 → 每轮重复 add → 永久刷「已存在」错误，
#         且清理看不见它 → 永远删不掉、清空清单也删不掉。


class CoreLike(object):
    """还原核心真实行为：

    - add → 写库；若已存在则报「已存在」
    - GET → **只返回已连上的**（连不上的查不出来）
    - DELETE → 按字符串删库，删不存在的也返回成功
    """

    def __init__(self, connected=True):
        self.db = set()
        self.conn = set()
        self.calls = []
        self.on = connected

    def list_peers(self):
        return sorted(self.conn), sorted(self.conn), ""

    def add_peer(self, a):
        self.calls.append(("add", a))
        if a in self.db:
            return False, "服务器 '%s' 已存在" % a
        self.db.add(a)
        if self.on:
            self.conn.add(a)
        return True, ""

    def del_peer(self, a):
        self.calls.append(("del", a))
        self.db.discard(a)
        self.conn.discard(a)
        return True, ""


def _adds(c):
    return [x for x in c.calls if x[0] == "add"]


# 场景：地址永远连不上 → 不会重复入数据库，也不会刷错误日志
d = doc_of([rec(cur="")])
c = CoreLike(connected=False)
errors_seen = 0
for i in range(4):
    c.calls.clear()
    acts = D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
    errors_seen += len([a for a in acts if a["action"] == "error"])
    if i == 0:
        check("首轮登记（连不上也入数据库）", len(_adds(c)) == 1, c.calls)
        check("已登记进 managed", d["managed"] == ["1.2.3.4:29873"], d["managed"])
    else:
        # 每轮会发一次 add 作「是否还在库里」的探测 —— 这是自愈的代价，
        # 但核心回「已存在」，不会重复入库、也不会产生 error 动作。
        check("第 %d 轮不产生错误动作" % (i + 1), errors_seen == 0, acts)
check("核心库中始终只有一条", c.db == {"1.2.3.4:29873"}, c.db)
check("四轮下来零错误", errors_seen == 0)

# 场景：连不上的地址，IP 变化 → 登记新地址，并删掉旧地址
d = doc_of([rec(cur="1.2.3.4:29873")], managed=["1.2.3.4:29873"])
c = CoreLike(connected=False)
c.db.add("1.2.3.4:29873")                     # 旧的已在库（连不上）
acts = D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "5.6.7.8"}))
check("换了新 IP 会登记新地址",
      ("add", "5.6.7.8:29873") in c.calls, c.calls)
check("连不上的旧地址被删掉", "1.2.3.4:29873" not in c.db, c.db)
check("库中只剩新地址", c.db == {"5.6.7.8:29873"}, c.db)
check("managed 更新为新地址", d["managed"] == ["5.6.7.8:29873"], d["managed"])

# 场景：连不上的地址，清空清单 → 必须能删掉（本轮修复的要点）
d = doc_of([rec(cur="1.2.3.4:29873")], managed=["1.2.3.4:29873"])
c = CoreLike(connected=False)
c.db.add("1.2.3.4:29873")
d["records"] = []
acts = D.sync_once(d, c, _getaddrinfo=gai_map({}))
check("清空清单能删掉连不上的残留", c.db == set(), c.db)
check("删除被记录为 del",
      any(a["action"] == "del" and a["addr"] == "1.2.3.4:29873" for a in acts), acts)
check("managed 归空", d["managed"] == [], d["managed"])

# 场景：连不上，且地址被人从库里删掉 → 下轮应重新登记（自愈）
d = doc_of([rec(cur="1.2.3.4:29873")], managed=["1.2.3.4:29873"])
c = CoreLike(connected=False)                 # db 与 conn 都空 → 已被删
c.calls.clear()
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("被删除后能自愈重新登记", c.db == {"1.2.3.4:29873"}, c.db)

# 场景：核心对 add 回「已存在」应视为成功，不记为错误
d = doc_of([rec(cur="")])
c = CoreLike(connected=False)
c.db.add("1.2.3.4:29873")                     # 已在库但连不上
acts = D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "1.2.3.4"}))
check("「已存在」不产生 error 动作",
      [a for a in acts if a["action"] == "error"] == [], acts)
check("「已存在」动作为 keep（已登记未连上）",
      [a for a in acts if a["addr"] == "1.2.3.4:29873"][0]["action"] == "keep", acts)
check("「已存在」后仍纳入 managed", d["managed"] == ["1.2.3.4:29873"], d["managed"])

# 场景：核心重启后从 SQLite 恢复记录；连不上的仍查不到，但 IP 变化时能换掉
d = doc_of([rec(cur="1.2.3.4:29873")], managed=["1.2.3.4:29873"])
c = CoreLike(connected=False)
c.db.add("1.2.3.4:29873")                     # 重启后从库里恢复（连不上）
check("连不上的记录不在 GET 列表里", c.list_peers()[0] == [], c.list_peers()[0])
D.sync_once(d, c, _getaddrinfo=gai_map({"p.example.com": "7.7.7.7"}))
check("核心重启后仍能换掉连不上的旧地址",
      c.db == {"7.7.7.7:29873"}, c.db)

# 场景：域名解析失败时，其已登记地址绝不能被清理阶段误删
d = doc_of([rec(cur="5.5.5.5:29873")], managed=["5.5.5.5:29873"])
c = CoreLike()
c.db.add("5.5.5.5:29873")
c.calls.clear()
acts = D.sync_once(d, c, _getaddrinfo=gai_map({}))     # 解析失败
check("解析失败时不删除其地址", c.db == {"5.5.5.5:29873"}, c.db)
check("解析失败时不发起写操作", c.calls == [], c.calls)
check("解析失败记为 error", any(a["action"] == "error" for a in acts), acts)
check("解析失败时 managed 保持", d["managed"] == ["5.5.5.5:29873"], d["managed"])

print("\n[22] 文档层：managed 的持久化与合并")
# ===========================================================================
# managed 是「从有到无」能生效的唯一依据，必须保证它不被各条写入路径弄丢。
p3 = os.path.join(tmp, "dns-mgd2.json")
D.save_doc(p3, {"version": 1, "interval": 60, "records": [], "managed": ["1.2.3.4:1"]})
doc = D.load_doc(p3)
check("managed 往返持久化", doc["managed"] == ["1.2.3.4:1"], doc)
check("records 为空但 managed 非空时状态正确",
      D.status_view(doc)["pending_cleanup"] is True)

# 老版本文件（无 managed 键）应回默认空列表，不报错
with open(p3, "w", encoding="utf-8") as f:
    json.dump({"version": 1, "interval": 60, "records": []}, f)
check("老文件缺 managed 键不报错", D.load_doc(p3)["managed"] == [])

# merged 时 managed 不该被 merge_records 影响（它只处理 records）
merged = D.merge_records([], [])
check("merge_records 只返回记录列表", isinstance(merged, list) and merged == [])

print("\n[23] 控制台客户端：登录 / 401 重登 / 地址转义")
# ===========================================================================
import urllib.error  # noqa: E402


class FakeHTTPResp(object):
    def __init__(self, payload, status=200):
        self._b = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self, n=None):
        return self._b


def make_client(responses, record):
    """构造 ConsoleClient，_open 按脚本依次返回（可抛 HTTPError）。"""
    seq = list(responses)

    def _open(url, data=None, headers=None, method="GET"):
        record.append({"url": url, "method": method,
                       "auth": (headers or {}).get("Authorization", ""),
                       "body": (json.loads(data.decode()) if data else None)})
        item = seq.pop(0)
        if isinstance(item, int):                      # 只给状态码 → 抛 HTTPError
            raise urllib.error.HTTPError(url, item, "err", {}, None)
        return FakeHTTPResp(item)

    return D.ConsoleClient("http://127.0.0.1:29071", "admin", "pw",
                           _open=_open, timeout=1)


calls = []
# 第一次查列表返回 401，自动重登后再成功
cl = make_client([
    401,
    {"code": 200, "msg": "success", "data": {"token": "TOK"}},
    {"code": 200, "msg": "success", "data": {"outbound": [{"addr": "1.2.3.4:29873"}],
                                             "inbound": []}},
], calls)
out, alladdr, err = cl.list_peers()
check("401 后自动重登并成功", out == ["1.2.3.4:29873"] and not err, (out, err))
check("重登请求为 /api/login", calls[1]["url"].endswith("/api/login"), calls[1]["url"])
check("重登后带上了 Bearer 令牌", calls[2]["auth"] == "Bearer TOK", calls[2]["auth"])
check("首次请求未带令牌", calls[0]["auth"] == "", calls[0]["auth"])

calls = []
# 惰性登录：无令牌时不预登录，直接发请求（本次成功）
cl = make_client([{"code": 200, "msg": "添加成功", "data": None}], calls)
ok, err = cl.add_peer("[2001:db8::1]:29873")
check("add 成功", ok and not err, err)
check("只发了一次请求（未多余登录）", len(calls) == 1, len(calls))
check("add 请求体为 server_addr",
      calls[0]["body"] == {"server_addr": "[2001:db8::1]:29873"}, calls[0]["body"])
check("add 走 POST /api/peer_servers",
      calls[0]["method"] == "POST" and calls[0]["url"].endswith("/api/peer_servers"),
      calls[0]["url"])

calls = []
cl = make_client([{"code": 200, "msg": "删除成功", "data": None}], calls)
ok, err = cl.del_peer("[2001:db8::1]:29873")
check("del 成功（IPv6）", ok and not err, err)
check("del 的 IPv6 地址被百分号转义",
      "%5B2001%3Adb8%3A%3A1%5D%3A29873" in calls[0]["url"], calls[0]["url"])
check("del 走 DELETE 方法", calls[0]["method"] == "DELETE", calls[0]["method"])

calls = []
cl = make_client([{"code": 400, "msg": "服务器 'x' 已存在", "data": None}], calls)
ok, err = cl.add_peer("1.2.3.4:29873")
check("业务错误码被判为失败", ok is False and "已存在" in err, err)

calls = []
cl = make_client([{"code": 200, "msg": "删除成功", "data": None}], calls)
ok, err = cl.del_peer("1.2.3.4:29873")
check("del 成功", ok and not err, err)

calls = []
cl = make_client([401], calls)
ok, err = cl.login()
check("登录 HTTP 401 有明确提示", ok is False and "登录失败" in err, err)

calls = []
cl = make_client([
    401,
    {"code": 200, "msg": "success", "data": {"token": "T5"}},
    401,
], calls)
ok, err = cl.add_peer("1.2.3.4:29873")
check("重登后仍 401 则如实报错", ok is False and "401" in err, err)
check("只重登一次（不无限循环）", len(calls) == 3, len(calls))

calls = []
cl = make_client([{"code": 200, "msg": "success", "data": {}}], calls)
ok, err = cl.login()
check("登录响应无 token 时判失败", ok is False and "令牌" in err, err)


def _boom(url, data=None, headers=None, method="GET"):
    raise OSError("connection refused")


cl2 = D.ConsoleClient("http://127.0.0.1:29071", "a", "b", _open=_boom, timeout=1)
out, alladdr, err = cl2.list_peers()
check("控制台不可达时返回错误而非抛异常", out == [] and "无法连接" in err, err)

# ===========================================================================
# 读核心数据库 + 以库为基准的清理（用户报的场景）
# ===========================================================================
# 背景：v2.0.9 实测 —— POST 一个连不上的地址会「添加成功」并写进 SQLite，
# 但 GET /api/peer_servers 的 outbound 只含当前连上的，所以它**永远不出现
# 在接口里**（重启核心也不出现：日志有 "Starting outbound connection to"，
# 但 API 仍为空）。于是「只认本页配置」的对账清不掉这些残留。
# 解法：只读数据库拿到完整名单，删除仍走 DELETE API。

# 本段需要一个临时目录来验证「库文件不存在」的分支
tmpdir = tempfile.mkdtemp(prefix="vnts-dnspeers-")

print("\n[19] 只读数据库拿全部已注册地址（用真实 SQLite）")

# 建一个与核心结构完全一致的库：
#   CREATE TABLE peer_servers (id INTEGER PRIMARY KEY AUTOINCREMENT,
#       server_addr TEXT NOT NULL UNIQUE, source INTEGER NOT NULL DEFAULT 0,
#       created_at INTEGER NOT NULL)
#   —— 取自 vnts2 v2.0.9 的真实建表语句
db_path = os.path.join(tmpdir, "network_control.db")
_c = sqlite3.connect(db_path)
_c.execute("""CREATE TABLE peer_servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_addr TEXT NOT NULL UNIQUE,
        source INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL)""")
_c.executemany("INSERT INTO peer_servers (server_addr, source, created_at) VALUES (?,?,?)",
               [("1.2.3.4:29873", 1, 100), ("5.6.7.8:29873", 1, 200),
                ("9.9.9.9:29873", 0, 300)])
_c.commit()
_c.close()

addrs, err = D.db_peer_addrs(db_path)
check("读出全部已注册地址（含连不上的）",
      addrs == ["1.2.3.4:29873", "5.6.7.8:29873", "9.9.9.9:29873"], addrs)
check("按 created_at 排序", addrs[0] == "1.2.3.4:29873", addrs)
check("成功时无错误", err == "", err)

# 只读保证：即使代码写错也改不了库（mode=ro 由 SQLite 强制）
_c = sqlite3.connect(db_path)
before = _c.execute("SELECT COUNT(*) FROM peer_servers").fetchone()[0]
_c.close()
_c = sqlite3.connect(db_path)
after = _c.execute("SELECT COUNT(*) FROM peer_servers").fetchone()[0]
_c.close()
check("读取不改变数据（只读语义）", before == after == 3, (before, after))

# 降级路径
addrs, err = D.db_peer_addrs("")
check("库路径为空时安全返回", addrs == [] and err, err)
addrs, err = D.db_peer_addrs(os.path.join(tmpdir, "nope.db"))
check("库文件不存在时安全返回", addrs == [] and err, err)

# 表结构异常（如空库）也要安全降级，绝不抛异常
blank = os.path.join(tmpdir, "blank.db")
_c = sqlite3.connect(blank)
_c.execute("CREATE TABLE other (x INTEGER)")
_c.commit()
_c.close()
addrs, err = D.db_peer_addrs(blank)
check("无 peer_servers 表时安全降级", addrs == [] and err, err)

print("\n[20] 清单为空：连不上的残留也能清掉（用户报的核心场景）")


class GhostClient(StubClient):
    """模拟真实核心：库里有的地址，GET 却看不见（连不上）。"""

    def __init__(self, db_only=None, connected=None):
        # connected 才出现在 list_peers（GET）里
        super(GhostClient, self).__init__(addrs=list(connected or []))
        self._db_only = list(db_only or [])

    def del_peer(self, a):
        self.calls.append(("del", a))
        # 真实核心按字符串删库：删不存在的也返回成功
        if a in self.addrs:
            self.addrs.remove(a)
        if a in self._db_only:
            self._db_only.remove(a)
        return True, ""


# 场景：清单已清空，库里还有 2 条连不上的残留
gc = GhostClient(db_only=["9.9.9.9:29873", "8.8.8.8:29873"])
d = D.default_doc()
d["records"] = []
d["managed"] = []          # managed 也空了 —— 旧逻辑正是卡在这里
acts = D.sync_once(d, gc, db_addrs=["9.9.9.9:29873", "8.8.8.8:29873"])
deleted = sorted(a["addr"] for a in acts if a["action"] == "del")
check("库里连不上的残留被清掉", deleted == ["8.8.8.8:29873", "9.9.9.9:29873"], deleted)
check("残留已被真正删除（客户端侧）", gc._db_only == [], gc._db_only)

# 对照：不给 db_addrs（模拟旧行为）就清不掉 —— 说明读取库是必需的
gc2 = GhostClient(db_only=["9.9.9.9:29873"])
d2 = D.default_doc()
d2["records"] = []
d2["managed"] = []
acts2 = D.sync_once(d2, gc2)
check("不传 db_addrs 时确实清不掉（证明必须读库）",
      not [a for a in acts2 if a["action"] == "del"], acts2)

print("\n[21] 以库为基准：保留清单内的，清掉清单外的")

# 清单要 a.com -> 1.1.1.1；库里有这三条，其中只有目标该留
gc3 = GhostClient(db_only=["7.7.7.7:29873"])
d3 = D.default_doc()
d3["records"] = [rec("a.com", "29873")]
d3["managed"] = ["1.1.1.1:29873"]        # 上轮已托管
acts3 = D.sync_once(d3, gc3, _getaddrinfo=gai_map({"a.com": "1.1.1.1"}),
                    db_addrs=["1.1.1.1:29873", "7.7.7.7:29873"])
kept = [a for a in acts3 if a["action"] in ("add", "keep")]
killed = [a["addr"] for a in acts3 if a["action"] == "del"]
check("目标地址被登记", any(a["addr"] == "1.1.1.1:29873" for a in kept), kept)
check("清单外的库记录被清掉", "7.7.7.7:29873" in killed, killed)

# 用户手工加的地址（在库里、清单里没有）也一并清掉 —— 这正是「只认本页」
gc4 = GhostClient(connected=["3.3.3.3:29873"])
d4 = D.default_doc()
d4["records"] = [rec("a.com", "29873")]
acts4 = D.sync_once(d4, gc4, _getaddrinfo=gai_map({"a.com": "1.1.1.1"}),
                    db_addrs=["3.3.3.3:29873"])
killed4 = [a["addr"] for a in acts4 if a["action"] == "del"]
check("控制台手工加的（清单外）被撤下", "3.3.3.3:29873" in killed4, killed4)

print("\n[22] 安全约束：解析失败时绝不动旧地址")

gc5 = GhostClient(db_only=["1.1.1.1:29873", "6.6.6.6:29873"])
d5 = D.default_doc()
d5["records"] = [rec("a.com", "29873", cur="1.1.1.1:29873")]
d5["managed"] = ["1.1.1.1:29873"]
# 域名解析失败 -> 目标沿用 current(1.1.1.1) -> 它必须保留
acts5 = D.sync_once(d5, gc5, _getaddrinfo=gai_map({}),
                    db_addrs=["1.1.1.1:29873", "6.6.6.6:29873"])
killed5 = [a["addr"] for a in acts5 if a["action"] == "del"]
check("解析失败时旧地址不被删", "1.1.1.1:29873" not in killed5, killed5)
check("同轮仍会清掉无关残留", "6.6.6.6:29873" in killed5, killed5)

print("\n[23] add 失败时保护旧地址（不被误删）")

gc6 = GhostClient()
gc6.fail_add = True
d6 = D.default_doc()
d6["records"] = [rec("a.com", "29873", cur="1.1.1.1:29873")]
d6["managed"] = ["1.1.1.1:29873"]
acts6 = D.sync_once(d6, gc6, _getaddrinfo=gai_map({"a.com": "2.2.2.2"}),
                    db_addrs=["1.1.1.1:29873"])
killed6 = [a["addr"] for a in acts6 if a["action"] == "del"]
check("add 失败时不删旧地址（互联不断）", "1.1.1.1:29873" not in killed6, killed6)

# ===========================================================================
print("\n" + "=" * 66)

print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)


