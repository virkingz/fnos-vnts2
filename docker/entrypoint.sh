#!/bin/bash
# vnts-nas Docker 入口脚本
#
# 设计：不改动原有 shell 代码，只在外部复用 lib.sh 的现成能力，
# 自行设定飞牛原本会注入的 TRIM_* 环境变量，然后拉起 shell_main.py。
#
# 环境变量（全部可选，括号内为默认值）：
#   PANEL_PORT     (19098) 管理页对外端口
#   SERVICE_PORT   (29872) 组网服务端口（vnts2 的 tcp/quic/ws，TCP+UDP 同号）
#   CONSOLE_PORT   (29071) 控制台内部端口（仅回环）
#   PANEL_TOKEN    (随机)  管理页访问令牌
#   LOG_DIR        (空)    日志目录 —— 【独立于配置】。设了它，日志就不落数据目录。
#   MIRROR         (空)    下载镜像前缀
#   PROXY          (空)    网络代理
#   AUTO_DOWNLOAD_CORE (1) 启动时若缺核心则自动下载
#   TZ             (UTC)   时区
#
# 重要：数据目录 /data 只放「配置 + 数据库 + 核心」；
#       日志请用 LOG_DIR 挂到独立卷（默认 /data/log，但仍建议独立挂载），
#       这样迁移时只拷 /data 即可，日志不会被带走。

set -u

APP_DIR="${VNT_APPDEST:-/app}"
DATA_DIR="${VNT_PKGVAR:-/data}"

# 记下用户显式传入的端口：lib.sh 的 vnt_load_ports 会用文件覆盖同名变量
_ENV_PANEL_PORT="${PANEL_PORT:-}"
_ENV_SERVICE_PORT="${SERVICE_PORT:-}"
_ENV_LOG_DIR="${LOG_DIR:-}"

SHELL_PID_FILE=""
shutting_down=0

on_stop() {
    [ "$shutting_down" = "1" ] && return 0
    shutting_down=1
    trap '' TERM INT
    echo "[vnts] 收到停止信号，正在退出..."

    _pid=""
    if [ -n "$SHELL_PID_FILE" ] && [ -f "$SHELL_PID_FILE" ]; then
        _pid="$(head -n 1 "$SHELL_PID_FILE" 2>/dev/null | tr -d '[:space:]')"
        [ -n "$_pid" ] && kill -TERM "$_pid" 2>/dev/null
    fi
    _i=0
    while [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null && [ $_i -lt 5 ]; do
        sleep 1; _i=$((_i + 1))
    done
    type vnt_stop_core >/dev/null 2>&1 && vnt_stop_core 2>/dev/null
    rm -f "${SHELL_PID_FILE:-/dev/null}" 2>/dev/null
    echo "[vnts] 已停止"
    exit 0
}
trap on_stop TERM INT

# 1) 还原飞牛环境变量
export TRIM_APPDEST="$APP_DIR"
export TRIM_PKGVAR="$DATA_DIR"
export TRIM_PKGETC="${DATA_DIR}/etc"
export TRIM_PKGTMP="/tmp"
export TRIM_APPNAME="vnts-nas"
export TRIM_TEMP_LOGFILE="${TRIM_PKGTMP}/error.log"

export VNT_APPDEST="$TRIM_APPDEST"
export VNT_PKGVAR="$TRIM_PKGVAR"
export VNT_PKGETC="$TRIM_PKGETC"
export VNT_PKGTMP="$TRIM_PKGTMP"
export VNT_APPNAME="$TRIM_APPNAME"

# 管理页绑定：容器内必须 0.0.0.0
export PANEL_BIND="${PANEL_BIND:-0.0.0.0}"

# 2) 载入函数库
# shellcheck disable=SC1091
. "${APP_DIR}/shell/lib.sh"

vnt_paths
vnt_ensure_dirs
vnt_load_ports

[ -n "${_ENV_PANEL_PORT}" ] && PANEL_PORT="${_ENV_PANEL_PORT}"
[ -n "${_ENV_SERVICE_PORT}" ] && SERVICE_PORT="${_ENV_SERVICE_PORT}"

if [ "${PANEL_PORT}" = "${SERVICE_PORT}" ]; then
    echo "[vnts] 警告：两端口相同，组网服务端口回退为 ${SERVICE_PORT_DEFAULT}"
    SERVICE_PORT="${SERVICE_PORT_DEFAULT}"
fi

# 日志目录：环境变量优先（lib.sh 的 vnt_log_dir 会读 LOG_DIR）
if [ -n "${_ENV_LOG_DIR}" ]; then
    mkdir -p "${_ENV_LOG_DIR}" 2>/dev/null || true
fi

echo "[vnts] 数据目录: ${DATA_DIR}"
echo "[vnts] 日志目录: $(vnt_log_dir)$([ -n "${_ENV_LOG_DIR}" ] && echo ' （来自 LOG_DIR）' || echo ' （数据目录内，建议用 LOG_DIR 独立挂载）')"
echo "[vnts] 管理页端口: ${PANEL_PORT}  |  组网服务端口: ${SERVICE_PORT}"

vnt_save_ports

# 3) 各项设置写入（显式设定了才写）
if [ -n "${PANEL_TOKEN:-}" ]; then
    printf '%s' "${PANEL_TOKEN}" > "$(vnt_bin_dir)/panel_token"
    chmod 600 "$(vnt_bin_dir)/panel_token" 2>/dev/null || true
fi

if [ -n "${MIRROR:-}" ]; then
    vnt_save_mirror "${MIRROR}"
    echo "[vnts] 镜像前缀: ${MIRROR}"
fi
if [ -n "${PROXY:-}" ]; then
    vnt_save_proxy "${PROXY}"
    echo "[vnts] 网络代理: $(vnt_get_proxy)"
fi

# 4) 渲染服务端配置（含 logs/log4rs.yaml 指向独立日志目录）
vnt_render_server_config && echo "[vnts] config.toml 已生成" \
    || echo "[vnts] 警告：config.toml 生成失败"

# 5) 可选：启动时自动下载核心
CORE_BIN="$(vnt_core_bin)"
if [ ! -f "$CORE_BIN" ] && [ "${AUTO_DOWNLOAD_CORE:-1}" != "0" ]; then
    echo "[vnts] 未发现核心，尝试自动下载（架构: $(uname -m)）..."
    VER="$(vnt_latest_version 2>/dev/null || true)"
    if [ -n "$VER" ]; then
        echo "[vnts] 最新版本: v${VER}"
        if vnt_install_core "$VER"; then
            echo "[vnts] 核心安装完成: v${VER}"
        else
            echo "[vnts] 核心自动下载失败，可在管理页手动下载或上传"
        fi
    else
        echo "[vnts] 无法获取最新版本号（网络受限？），可在管理页填镜像前缀后重试"
    fi
fi

if [ -f "$CORE_BIN" ]; then
    echo "[vnts] 当前核心版本: $(vnt_installed_version)"
else
    echo "[vnts] 核心尚未安装，请打开管理页下载"
fi

# 6) 拉起后端
PYTHON_BIN="$(vnt_shell_python)"
SHELL_PY="${APP_DIR}/shell/shell_main.py"
SHELL_PID_FILE="$(vnt_shell_pid)"
SHELL_LOG="$(vnt_shell_log)"

if [ ! -f "$SHELL_PY" ]; then
    echo "[vnts] 缺少后端入口: $SHELL_PY" >&2
    exit 1
fi

echo "[vnts] 启动管理页后端: ${PYTHON_BIN} ${SHELL_PY}"
cd "${APP_DIR}/shell" || exit 1

"$PYTHON_BIN" "$SHELL_PY" >> "$SHELL_LOG" 2>&1 &
child=$!
printf '%s' "$child" > "$SHELL_PID_FILE"

i=0
while [ $i -lt 30 ]; do
    if ! kill -0 "$child" 2>/dev/null; then
        echo "[vnts] 后端进程已退出，日志尾部如下："
        tail -n 40 "$SHELL_LOG" 2>/dev/null
        rm -f "$SHELL_PID_FILE"
        exit 1
    fi
    if command -v curl >/dev/null 2>&1 \
       && curl -sf --max-time 2 "http://127.0.0.1:${PANEL_PORT}/panel-api/health" >/dev/null 2>&1; then
        echo "[vnts] 管理页就绪: http://<主机>:${PANEL_PORT}"
        echo "[vnts] 控制台内嵌于管理页「控制台」标签"
        break
    fi
    sleep 1; i=$((i + 1))
done

wait "$child"
code=$?
rm -f "$SHELL_PID_FILE"
echo "[vnts] 后端已退出（code=${code}）"
vnt_stop_core 2>/dev/null
exit "$code"
