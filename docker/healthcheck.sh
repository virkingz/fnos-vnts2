#!/bin/sh
# vnts-nas 健康检查：运行时读 ports.env，改端口也探得准。
set -u

PANEL_PORT=19098
PORT_FILE="${VNT_PKGVAR:-/data}/ports.env"
if [ -f "$PORT_FILE" ]; then
    v="$(grep -E '^PANEL_PORT=' "$PORT_FILE" 2>/dev/null | head -n1 | cut -d= -f2 | tr -d '[:space:]')"
    [ -n "$v" ] && PANEL_PORT="$v"
fi

if command -v curl >/dev/null 2>&1; then
    curl -sf --max-time 3 "http://127.0.0.1:${PANEL_PORT}/panel-api/health" >/dev/null 2>&1 && exit 0
fi

# 回退：用 python 探端口
python3 - "$PANEL_PORT" <<'PY' 2>/dev/null && exit 0
import socket, sys
p = int(sys.argv[1])
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("127.0.0.1", p))
    s.close()
except OSError:
    sys.exit(1)
PY
exit 1
