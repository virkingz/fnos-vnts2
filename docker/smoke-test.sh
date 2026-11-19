#!/bin/sh
# 镜像冒烟测试：验证「精简后的 Python 环境」在容器里功能完好。
#
# 为什么需要它：Dockerfile 删掉了 ensurepip / unittest / multiprocessing 等
# 标准库模块。这类删除在构建期不会报错，风险都在**运行时**才暴露 ——
# 比如某个模块被间接 import 才发现缺失。所以在容器里实跑一遍最可靠。
#
# 用法（在能跑 Docker 的机器上，仓库根目录）：
#   docker build -f docker/Dockerfile -t fnos-vnts2:test .
#   docker run --rm -v "$PWD/docker:/t:ro" fnos-vnts2:test sh /t/smoke-test.sh
#
# 或者直接用已构建好的镜像：
#   docker run --rm -v "$PWD/docker:/t:ro" fnos-vnts2:edge sh /t/smoke-test.sh
#
# 退出码 0 = 全部通过。
set -u

APP=/app
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "  [PASS] $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  [FAIL] $1"; }
check(){ if [ "$1" = "0" ]; then ok "$2"; else bad "$2"; fi; }

echo "================================================================"
echo "镜像冒烟测试"
echo "================================================================"

# ---------------------------------------------------------------------------
echo ""
echo "[1] 基础环境"
echo "----------------------------------------------------------------"
python3 -c 'import sys; print("    python3:", sys.version.split()[0])' 2>/dev/null
check $? "python3 可执行"
python3 -c 'exit(0)' 2>/dev/null
check $? "python3 能正常退出"

for c in curl sed awk grep find chmod; do
    command -v "$c" >/dev/null 2>&1
    check $? "命令可用: $c"
done

# ---------------------------------------------------------------------------
echo ""
echo "[2] 我们真正 import 的标准库（全部必须能导入）"
echo "----------------------------------------------------------------"
# 这是精简的正确性核心：只要这些还在，删别的就不影响功能。
python3 - <<'PY'
import sys
mods = [
    # app/shell/*.py 实际用到的
    "argparse", "hashlib", "http.server", "io", "json", "math", "os", "re",
    "secrets", "shutil", "signal", "socket", "socketserver", "ssl", "struct",
    "subprocess", "sys", "tarfile", "threading", "time", "traceback",
    "urllib.request", "urllib.parse", "urllib.error", "zipfile", "zlib",
    # 运行时间接用到
    "base64", "binascii", "csv", "datetime", "email.utils", "encodings.idna",
    "fnmatch", "glob", "hmac", "http.client", "ipaddress", "logging",
    "mimetypes", "platform", "posixpath", "random", "select", "sqlite3",
    "string", "tempfile", "textwrap", "unicodedata", "uuid", "warnings",
    "weakref", "xml.etree.ElementTree",
]
missing = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        missing.append("%s (%s)" % (m, e))
if missing:
    print("缺失模块：")
    for m in missing:
        print("   ", m)
    sys.exit(1)
print("    %d 个模块全部可导入" % len(mods))
PY
check $? "标准库完整"

# 关键：https 能力（下载核心依赖）
python3 -c '
import ssl, urllib.request
ctx = ssl.create_default_context()
assert ctx.get_ca_certs(), "无根证书"
print("    OpenSSL:", ssl.OPENSSL_VERSION)
' 2>/dev/null
check $? "ssl 可用且加载了根证书（下载核心必需）"

# ---------------------------------------------------------------------------
echo ""
echo "[3] 被删模块：确认真的用不到"
echo "----------------------------------------------------------------"
# 这些是我们精简掉的。若它们仍能被 import，说明删除没生效（体积没省下来）；
# 若「删了但某处会用到」，则 [2] 会先失败。这里只验证删除确实生效。
for m in ensurepip lib2to3 unittest multiprocessing tkinter; do
    if python3 -c "import $m" 2>/dev/null; then
        bad "模块 $m 仍存在（删除未生效，体积没省下来）"
    else
        ok "模块 $m 已移除"
    fi
done

# ---------------------------------------------------------------------------
echo ""
echo "[4] 核心文件齐备"
echo "----------------------------------------------------------------"
for f in \
    "$APP/shell/shell_server.py" \
    "$APP/shell/shell_http.py" \
    "$APP/shell/shell_main.py" \
    "$APP/shell/lib.sh" \
    "$APP/shell/dns_peers.py" \
    "$APP/shell/vnts_config.py" \
    "$APP/shell/web/index.html" \
    "$APP/shell/web/app.js" \
    "$APP/shell/web/icon.png" \
    "$APP/docker/entrypoint.sh" \
    "$APP/docker/healthcheck.sh" \
    "$APP/ui/images/icon_256.png"
do
    [ -f "$f" ]
    check $? "$(basename "$f")"
done

# icon.png 必须非空（空文件会让网页图标空白）
[ -s "$APP/shell/web/icon.png" ]
check $? "icon.png 非空"

# 构建残留不该在镜像里
[ ! -d "$APP/scripts" ]
check $? "构建残留 /app/scripts 已清理"

# 入口脚本可执行
[ -x "$APP/docker/entrypoint.sh" ]
check $? "entrypoint.sh 可执行"

# ---------------------------------------------------------------------------
echo ""
echo "[5] 壳能起来并响应健康检查"
echo "----------------------------------------------------------------"
# 不启动核心（那要联网下载），只验证管理页后端本身。
export VNT_PKGVAR="${VNT_PKGVAR:-/tmp/smoketest-data}"
mkdir -p "$VNT_PKGVAR"
python3 "$APP/shell/shell_main.py" >/tmp/shell.log 2>&1 &
SHELL_PID=$!
sleep 3

PANEL_PORT=19098
if [ -f "$VNT_PKGVAR/ports.env" ]; then
    v="$(grep -E '^PANEL_PORT=' "$VNT_PKGVAR/ports.env" | head -n1 | cut -d= -f2 | tr -d '[:space:]')"
    [ -n "$v" ] && PANEL_PORT="$v"
fi

if curl -sf --max-time 5 "http://127.0.0.1:${PANEL_PORT}/panel-api/health" >/dev/null 2>&1; then
    ok "管理页 /panel-api/health 返回 200"
else
    bad "管理页健康检查失败"
    echo "    --- shell.log 尾部 ---"
    tail -20 /tmp/shell.log 2>/dev/null | sed 's/^/    /'
fi

# 首页能取到
if curl -sf --max-time 5 "http://127.0.0.1:${PANEL_PORT}/" >/dev/null 2>&1; then
    ok "管理页首页可访问"
else
    bad "管理页首页不可访问"
fi

# 图标不 404
if curl -sf --max-time 5 "http://127.0.0.1:${PANEL_PORT}/icon.png" >/dev/null 2>&1; then
    ok "图标 /icon.png 可访问"
else
    bad "图标 /icon.png 404（网页左上角会空白）"
fi

kill "$SHELL_PID" 2>/dev/null
sleep 1
kill -9 "$SHELL_PID" 2>/dev/null
rm -rf "$VNT_PKGVAR"

# ---------------------------------------------------------------------------
echo ""
echo "================================================================"
printf "结果：%d 通过 / %d 失败\n" "$PASS" "$FAIL"
echo "================================================================"
[ "$FAIL" = "0" ] || exit 1
exit 0
