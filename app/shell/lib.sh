#!/bin/bash
# vnts-nas 公共函数库：路径、日志、架构映射、核心下载、进程管理、端口模型。
# 由 cmd/* 生命周期脚本与管理页后端 source。
#
# 路径模型（关键）：
#   $TRIM_PKGVAR/              数据目录，迁移/导出只需这一个
#   ├── bin/                   核心二进制（在线更新，不属于 fpk）
#   ├── server/                vnts2 工作目录（config.toml / 数据库 / 证书）
#   ├── cache/                 下载缓存与回滚备份
#   └── ports.env              端口等运行期参数
#   <LOG_DIR>/                 日志目录 —— 独立可配，默认 $TRIM_PKGVAR/log
#
# 日志与配置分离：日志永远写 LOG_DIR，不写进 server/。
# 迁移数据卷时不带日志；导出配置也不含日志。

set -u

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
VNT_REPO="vnt-dev/vnts"
VNT_GITHUB_API="https://api.github.com/repos/${VNT_REPO}/releases/latest"
VNT_GITHUB_DL="https://github.com/${VNT_REPO}/releases/download"

PANEL_PORT_DEFAULT=19098     # 管理页端口（本壳）
SERVICE_PORT_DEFAULT=29872   # 组网服务端口（vnts2 的 tcp/quic/ws，TCP+UDP 同号）
CONSOLE_PORT_DEFAULT=29071   # 控制台内部端口（vnts2 的 web_bind，仅回环）

# ---------------------------------------------------------------------------
# 路径（全部基于 TRIM_* 环境变量，禁止硬编码）
# ---------------------------------------------------------------------------
vnt_paths() {
    : "${TRIM_APPDEST:=/var/apps/vnts-nas/target}"
    : "${TRIM_PKGVAR:=/var/apps/vnts-nas/var}"
    : "${TRIM_PKGETC:=/var/apps/vnts-nas/etc}"
    : "${TRIM_PKGTMP:=/var/apps/vnts-nas/tmp}"
    : "${TRIM_TEMP_LOGFILE:=${TRIM_PKGTMP}/error.log}"
    : "${TRIM_APPNAME:=vnts-nas}"
}

vnt_var_dir()    { echo "${TRIM_PKGVAR}"; }
vnt_bin_dir()    { echo "${TRIM_PKGVAR}/bin"; }
vnt_server_dir() { echo "${TRIM_PKGVAR}/server"; }
vnt_cache_dir()  { echo "${TRIM_PKGVAR}/cache"; }
vnt_pid_file()   { echo "${TRIM_PKGVAR}/core.pid"; }
vnt_shell_pid()  { echo "${TRIM_PKGVAR}/shell.pid"; }
vnt_port_file()  { echo "${TRIM_PKGVAR}/ports.env"; }
vnt_logdir_file(){ echo "${TRIM_PKGVAR}/logdir"; }

# 日志目录：LOG_DIR 环境变量 > logdir 文件 > 默认
vnt_log_dir() {
    if [ -n "${LOG_DIR:-}" ]; then
        printf '%s' "${LOG_DIR}"
        return 0
    fi
    local f v
    f="$(vnt_logdir_file)"
    if [ -f "$f" ]; then
        v="$(head -n 1 "$f" 2>/dev/null | tr -d '\r\n')"
        if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
    fi
    printf '%s' "${TRIM_PKGVAR}/log"
    return 0
}

vnt_save_logdir() {
    local d
    d="$(printf '%s' "${1:-}" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [ -z "$d" ]; then
        rm -f "$(vnt_logdir_file)"
    else
        printf '%s' "$d" > "$(vnt_logdir_file)"
        chmod 600 "$(vnt_logdir_file)" 2>/dev/null || true
    fi
    return 0
}

vnt_main_log()   { echo "$(vnt_log_dir)/main.log"; }
vnt_update_log() { echo "$(vnt_log_dir)/update.log"; }
vnt_shell_log()  { echo "$(vnt_log_dir)/shell.log"; }

vnt_core_bin()      { echo "$(vnt_bin_dir)/vnts2"; }
vnt_core_ver_file() { echo "$(vnt_bin_dir)/.vnts_version"; }
vnt_core_src_file() { echo "$(vnt_bin_dir)/.vnts_source"; }

vnt_server_config() { echo "$(vnt_server_dir)/config.toml"; }
vnt_server_db()     { echo "$(vnt_server_dir)/network_control.db"; }

vnt_config_py() { echo "${TRIM_APPDEST}/shell/vnts_config.py"; }

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
VNT_LOG_MAX_BYTES="${VNT_LOG_MAX_BYTES:-524288}"    # 512 KB
VNT_LOG_KEEP_BYTES="${VNT_LOG_KEEP_BYTES:-262144}"  # 256 KB

vnt_trim_log() {
    local f="$1" size tmp
    [ -f "$f" ] || return 0
    size="$(wc -c < "$f" 2>/dev/null | tr -d ' ')" || return 0
    [ -n "$size" ] || return 0
    [ "$size" -gt "$VNT_LOG_MAX_BYTES" ] 2>/dev/null || return 0
    tmp="${f}.trim"
    { printf '[log trimmed: 仅保留最近 %d KB]\n' "$((VNT_LOG_KEEP_BYTES / 1024))"
      tail -c "$VNT_LOG_KEEP_BYTES" "$f" 2>/dev/null | tail -n +2; } > "$tmp" 2>/dev/null || {
        rm -f "$tmp"; return 0; }
    cat "$tmp" > "$f" 2>/dev/null
    rm -f "$tmp"
}

vnt_log() {
    local f
    f="$(vnt_main_log)"
    mkdir -p "$(dirname "$f")" 2>/dev/null
    vnt_trim_log "$f"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$$] $*" >> "$f"
}

vnt_ulog() {
    local f
    f="$(vnt_update_log)"
    mkdir -p "$(dirname "$f")" 2>/dev/null
    vnt_trim_log "$f"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$$] $*" >> "$f"
}

vnt_fail() {
    vnt_log "ERROR: $*"
    echo "$*" > "${TRIM_TEMP_LOGFILE}" 2>/dev/null || true
    exit 1
}

# ---------------------------------------------------------------------------
# 初始化目录
# ---------------------------------------------------------------------------
vnt_ensure_dirs() {
    mkdir -p "$(vnt_bin_dir)" "$(vnt_server_dir)" "$(vnt_cache_dir)" "$(vnt_log_dir)"
    chmod 700 "$(vnt_var_dir)" 2>/dev/null || true
    chmod 700 "$(vnt_server_dir)" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 架构映射：fnOS 架构 -> vnts release asset 三元组
# vnts 发布的是【裸二进制】，文件名无扩展名
# ---------------------------------------------------------------------------
vnt_asset_name() {
    local arch="${1:-$(uname -m)}"
    local triple
    case "$arch" in
        x86_64|amd64|x64)   triple="x86_64-unknown-linux-musl" ;;
        aarch64|arm64)      triple="aarch64-unknown-linux-musl" ;;
        armv7l|armv7|armhf) triple="armv7-unknown-linux-musleabihf" ;;
        armv6l|arm)         triple="arm-unknown-linux-musleabihf" ;;
        mips)               triple="mips-unknown-linux-musl" ;;
        mipsel)             triple="mipsel-unknown-linux-musl" ;;
        i386|i686)          triple="" ;;
        *)                  triple="" ;;
    esac
    [ -n "$triple" ] || return 1
    echo "vnts2-${triple}"
    return 0
}

# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------
vnt_load_ports() {
    if [ -f "$(vnt_port_file)" ]; then
        # shellcheck disable=SC1090
        . "$(vnt_port_file)"
    fi
    : "${PANEL_PORT:=${PANEL_PORT_DEFAULT}}"
    : "${SERVICE_PORT:=${SERVICE_PORT_DEFAULT}}"
    : "${CONSOLE_PORT:=${CONSOLE_PORT_DEFAULT}}"
}

vnt_save_ports() {
    local f
    f="$(vnt_port_file)"
    {
        echo "PANEL_PORT=${PANEL_PORT:-$PANEL_PORT_DEFAULT}"
        echo "SERVICE_PORT=${SERVICE_PORT:-$SERVICE_PORT_DEFAULT}"
        echo "CONSOLE_PORT=${CONSOLE_PORT:-$CONSOLE_PORT_DEFAULT}"
    } > "$f"
    chmod 600 "$f"
}

# ---------------------------------------------------------------------------
# 镜像前缀 / 网络代理
# ---------------------------------------------------------------------------
vnt_conf_dir() { echo "$(vnt_server_dir)"; }

vnt_mirror_prefix() {
    local f="$(vnt_server_dir)/mirror"
    if [ -f "$f" ]; then
        head -n 1 "$f" | tr -d '\r\n' | sed 's:/*$::'
    fi
    return 0
}

vnt_url() {
    local prefix
    prefix="$(vnt_mirror_prefix)"
    if [ -n "$prefix" ]; then
        echo "${prefix}/$1"
    else
        echo "$1"
    fi
}

vnt_save_mirror() {
    local m
    m="$(printf '%s' "$1" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s:/*$::')"
    printf '%s' "$m" > "$(vnt_server_dir)/mirror"
    return 0
}

vnt_proxy_file() { echo "$(vnt_server_dir)/proxy"; }

vnt_get_proxy() {
    local f
    f="$(vnt_proxy_file)"
    if [ -f "$f" ]; then
        head -n 1 "$f" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
    fi
    return 0
}

vnt_save_proxy() {
    local p
    p="$(printf '%s' "$1" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [ -n "$p" ]; then
        case "$p" in
        *://*) ;;
        *) p="http://${p}" ;;
        esac
    fi
    printf '%s' "$p" > "$(vnt_proxy_file)"
    chmod 600 "$(vnt_proxy_file)" 2>/dev/null || true
    return 0
}

vnt_curl_proxy_args() {
    local p
    p="$(vnt_get_proxy)"
    if [ -n "$p" ]; then
        printf '%s' "--proxy ${p}"
    fi
    return 0
}

vnt_curl() {
    local proxy_args
    proxy_args="$(vnt_curl_proxy_args)"
    if [ -n "$proxy_args" ]; then
        # shellcheck disable=SC2086
        curl $proxy_args "$@"
    else
        curl "$@"
    fi
    return $?
}

# ---------------------------------------------------------------------------
# 版本查询
# ---------------------------------------------------------------------------
vnt_latest_version() {
    local tmp body code tag
    tmp="$(mktemp)"
    code="$(vnt_curl -sSL --max-time 20 -o "$tmp" -w '%{http_code}' \
        -H 'Accept: application/vnd.github+json' \
        -H 'User-Agent: vnts-nas-fpk' \
        "$(vnt_url "$VNT_GITHUB_API")" 2>/dev/null)" || code=000
    if [ "$code" = "200" ] && [ -s "$tmp" ]; then
        body="$(cat "$tmp")"
        tag="$(printf '%s' "$body" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
        rm -f "$tmp"
        if [ -n "$tag" ]; then
            printf '%s' "${tag#v}"
            return 0
        fi
    fi
    rm -f "$tmp"
    local eff tag2
    eff="$(vnt_curl -sSI --max-time 20 -o /dev/null -w '%{url_effective}' \
        "$(vnt_url "https://github.com/${VNT_REPO}/releases/latest")" 2>/dev/null)" || return 1
    eff="$(printf '%s' "$eff" | sed 's/%3A/:/g;s/%2F/\//g')"
    tag2="${eff##*/}"
    tag2="${tag2#v}"
    case "$tag2" in
        ''|latest|*[!0-9.]*) return 1 ;;
    esac
    printf '%s' "$tag2"
    return 0
}

vnt_installed_version() {
    local f
    f="$(vnt_core_ver_file)"
    if [ -f "$f" ]; then
        head -n 1 "$f" | tr -d '\r\n'
    else
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# 下载并安装核心（裸二进制，无 zip）
# ---------------------------------------------------------------------------
vnt_install_core() {
    local ver="$1"
    local stem asset dl url cur
    stem="$(vnt_asset_name)" || return 2
    asset="${stem}-v${ver}"
    dl="$(vnt_cache_dir)/${asset}.download"
    url="$(vnt_url "${VNT_GITHUB_DL}/v${ver}/${asset}")"

    vnt_ulog "install core ${ver} asset=${asset}"
    mkdir -p "$(vnt_cache_dir)" "$(vnt_bin_dir)"

    vnt_ulog "download ${url}"
    if ! vnt_curl -sSL --max-time 600 --retry 3 --retry-delay 2 -o "$dl" "$url"; then
        vnt_ulog "download failed"
        return 3
    fi
    if [ ! -s "$dl" ]; then
        vnt_ulog "downloaded file empty"
        return 3
    fi
    if [ "$(head -c 4 "$dl" | od -An -tx1 | tr -d ' \n')" != "7f454c46" ]; then
        vnt_ulog "downloaded file is not an ELF binary"
        rm -f "$dl"
        return 5
    fi

    cur="$(vnt_core_bin)"
    if [ -f "$cur" ]; then
        cp -f "$cur" "$(vnt_cache_dir)/vnts2.bak" 2>/dev/null || true
    fi
    chmod 0755 "$dl"
    if ! cp -f "$dl" "${cur}.new" 2>/dev/null; then
        vnt_ulog "copy new binary failed"
        return 6
    fi
    chmod 0755 "${cur}.new"
    mv -f "${cur}.new" "$cur" || { vnt_ulog "atomic replace failed"; return 6; }

    echo "$ver" > "$(vnt_core_ver_file)"
    {
        echo "version=$ver"
        echo "asset=$asset"
        echo "url=$url"
        echo "installed_at=$(date '+%Y-%m-%d %H:%M:%S')"
    } > "$(vnt_core_src_file)"

    rm -f "$dl"
    vnt_ulog "core ${ver} installed ok"
    return 0
}

vnt_rollback_core() {
    local bak="$(vnt_cache_dir)/vnts2.bak"
    [ -f "$bak" ] || return 1
    cp -f "$bak" "$(vnt_core_bin)" && chmod 0755 "$(vnt_core_bin)"
    vnt_ulog "rolled back to previous core"
    return 0
}

# ---------------------------------------------------------------------------
# 渲染 vnts2 的 config.toml 与 log4rs.yaml（日志分离抓手）
# ---------------------------------------------------------------------------
vnt_render_server_config() {
    local py cfgp rc
    py="$(vnt_shell_python)"
    cfgp="$(vnt_config_py)"
    [ -f "$cfgp" ] || { vnt_log "缺少配置渲染脚本: $cfgp"; return 1; }
    "$py" "$cfgp" render \
        --server-dir "$(vnt_server_dir)" \
        --log-dir "$(vnt_log_dir)" \
        --ports-file "$(vnt_port_file)" >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || vnt_log "渲染 config.toml 失败 (rc=$rc)"
    return $rc
}

# ---------------------------------------------------------------------------
# 进程管理
# ---------------------------------------------------------------------------
vnt_core_running() {
    local pf pid
    pf="$(vnt_pid_file)"
    [ -f "$pf" ] || return 1
    pid="$(head -n 1 "$pf" | tr -d '[:space:]')"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

vnt_shell_running() {
    local pf pid
    pf="$(vnt_shell_pid)"
    [ -f "$pf" ] || return 1
    pid="$(head -n 1 "$pf" | tr -d '[:space:]')"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

vnt_stop_core() {
    local pf pid i
    pf="$(vnt_pid_file)"

    if vnt_core_running; then
        pid="$(head -n 1 "$pf" | tr -d '[:space:]')"
        vnt_log "stopping core pid=${pid}"
        kill -TERM "$pid" 2>/dev/null || true
        i=0
        while vnt_core_running && [ $i -lt 15 ]; do
            sleep 1; i=$((i + 1))
        done
        if vnt_core_running; then
            kill -KILL "$pid" 2>/dev/null || true
            sleep 1
        fi
    fi

    # 关键兜底：pid 文件会丢（卸载删数据、异常退出、容器被杀），
    # 但进程可能还活着并占着端口。只信 pid 文件就会留下孤儿进程，
    # 重装后新核心起不来，报 Address in use。这里按**进程名 + 端口**再清一遍。
    vnt_kill_core_stragglers

    rm -f "$pf"
    return 0
}

vnt_kill_core_stragglers() {
    local pat p kind found=0
    # 1) 按进程名（核心二进制名可能是 vnts2 或带架构后缀）
    for pat in vnts2 vnts; do
        if pgrep -f "$pat" >/dev/null 2>&1; then
            vnt_log "清理残留核心进程（匹配 $pat）"
            pkill -TERM -f "$pat" 2>/dev/null || true
            found=1
        fi
    done
    if [ "$found" = "1" ]; then
        sleep 2
        for pat in vnts2 vnts; do
            pkill -KILL -f "$pat" 2>/dev/null || true
        done
    fi

    # 2) 按端口兜底：核心监听组网端口（TCP+UDP）与控制台端口。
    #    quic 走 UDP，只杀 tcp 会漏。
    for p in ${SERVICE_PORT:-29872} ${CONSOLE_PORT:-29071}; do
        [ -n "$p" ] || continue
        for kind in tcp udp; do
            if command -v fuser >/dev/null 2>&1; then
                fuser -k "${p}/${kind}" >/dev/null 2>&1 || true
            fi
        done
    done
    # 3) 等端口真正释放
    i=0
    while [ $i -lt 5 ]; do
        if ! vnt_port_listening "${SERVICE_PORT:-29872}" && \
           ! vnt_port_listening "${CONSOLE_PORT:-29071}"; then
            return 0
        fi
        sleep 1; i=$((i + 1))
    done
    return 0
}

vnt_port_listening() {
    local p="${1:-}"
    [ -n "$p" ] || return 1
    if command -v ss >/dev/null 2>&1; then
        ss -lntu 2>/dev/null | grep -q "[:.]${p}[[:space:]]" && return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Python 运行时探测
# ---------------------------------------------------------------------------
vnt_shell_python() {
    local c
    for c in \
        /var/apps/python312/target/bin/python3 \
        /var/apps/python312/target/bin/python \
        /usr/local/bin/python3 \
        /usr/bin/python3 \
        python3 python
    do
        if command -v "$c" >/dev/null 2>&1; then
            echo "$c"
            return 0
        fi
    done
    echo "python3"
    return 0
}

vnt_shell_port_up() {
    vnt_load_ports
    local port="${PANEL_PORT:-$PANEL_PORT_DEFAULT}"
    if command -v curl >/dev/null 2>&1; then
        curl -sf --max-time 2 "http://127.0.0.1:${port}/panel-api/health" >/dev/null 2>&1 && return 0
    fi
    vnt_port_in_use "$port"
}

vnt_port_in_use() {
    local port="${1:-}"
    [ -n "$port" ] || return 1
    if command -v ss >/dev/null 2>&1; then
        ss -tln 2>/dev/null | grep -qE ":${port}[[:space:]]" && return 0
    fi
    if command -v netstat >/dev/null 2>&1; then
        netstat -an 2>/dev/null | grep -E "LISTEN" | grep -qE "[:.]${port}[[:space:]]" && return 0
    fi
    return 1
}

vnt_kill_by_port() {
    local port="$1" pids p
    [ -n "$port" ] || return 0

    if command -v fuser >/dev/null 2>&1; then
        fuser -k -TERM "${port}/tcp" >/dev/null 2>&1 || true
        sleep 1
        fuser -k -KILL "${port}/tcp" >/dev/null 2>&1 || true
        return 0
    fi

    if command -v lsof >/dev/null 2>&1; then
        pids="$(lsof -t -i:"${port}" 2>/dev/null || true)"
        if [ -n "$pids" ]; then
            # shellcheck disable=SC2086
            kill -TERM $pids 2>/dev/null || true
            sleep 1
            # shellcheck disable=SC2086
            kill -KILL $pids 2>/dev/null || true
        fi
        return 0
    fi

    if command -v netstat >/dev/null 2>&1 && command -v taskkill >/dev/null 2>&1; then
        pids="$(netstat -ano 2>/dev/null \
            | grep -E "LISTENING" \
            | grep -E "[:.]${port}[[:space:]]" \
            | awk '{print $NF}' | sort -u || true)"
        for p in $pids; do
            [ -n "$p" ] && [ "$p" != "0" ] && taskkill //F //PID "$p" >/dev/null 2>&1 || true
        done
    fi
    return 0
}
