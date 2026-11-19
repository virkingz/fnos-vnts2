# fnos-vnts2 —— VNT 服务端（vnts2 壳）Docker 镜像
#
# 与 fpk 版共用同一套 app/shell 代码，只是把飞牛注入的 TRIM_* 换成容器内自定，
# 因此不需要修改任何原有源码。
#
# 构建（仓库根目录）：
#   docker build -f docker/Dockerfile -t fnos-vnts2:latest .
#
# 多架构：vnts2 核心是运行时按 uname -m 下载的静态 musl 二进制，
# 壳本身是纯 Python，同一个 Dockerfile 可直接构建 amd64 / arm64 / armv7。
FROM alpine:3.20

LABEL org.opencontainers.image.title="fnos-vnts2" \
      org.opencontainers.image.description="VNT 服务端（vnts2 壳）：核心在线更新、服务端配置、配置导入导出、独立日志目录" \
      org.opencontainers.image.source="https://github.com/virkingz/fnos-vnts2" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# ---------------------------------------------------------------------------
# 单层完成：装包 → 精简 Python → 拷代码 → 生成图标 → 修脚本 → 清理。
#
# **为什么必须合成一层**：Docker 的删除只是在联合文件系统里打一个
# whiteout 标记，**底层数据仍留在上一层**。若把 `apk add` 与 `rm -rf`
# 分在两条 RUN，删掉的 ensurepip 等依然占着镜像体积（实测：分两层时
# 精简只让镜像小了 0.34MB，合成一层后才真正省下 3.3MB）。
#
# 代码用 `--mount=type=bind` 从构建上下文读入，不占层 ——
# 于是「改代码」不会让 apk 安装层失效，重建依然很快。
#
# 依赖取舍（每项都核过是否真用得上）：
#   python3          壳本体与配置生成
#   bash             lib.sh / entrypoint.sh 用 bash 语法
#   curl             下载核心（SOCKS 代理只能靠它）
#   ca-certificates  curl 走 https 需要根证书
#   tzdata           TZ=Asia/Shanghai 生效，日志时间戳显示为 +08:00
#   tini             PID 1，回收僵尸并转发信号
#
# 不再安装的：
#   openssl(CLI)  原本的注释说用它「生成令牌」，但令牌实际由 Python 的
#                 secrets.token_hex 生成，全项目没有一处调用 openssl 命令。
#                 删掉 CLI 省 0.77MB；ssl **库**（libssl3/libcrypto3）仍会
#                 随 python3 依赖带入 —— 下载核心走 https 必须它，能力不减。
# ---------------------------------------------------------------------------
RUN --mount=type=bind,source=app/shell,target=/src/shell \
    --mount=type=bind,source=app/ui,target=/src/ui \
    --mount=type=bind,source=scripts/gen_icons.py,target=/src/gen_icons.py \
    --mount=type=bind,source=docker/entrypoint.sh,target=/src/entrypoint.sh \
    --mount=type=bind,source=docker/healthcheck.sh,target=/src/healthcheck.sh \
    set -eux; \
    apk add --no-cache \
        python3 \
        bash \
        curl \
        ca-certificates \
        tzdata \
        tini; \
    ln -sf /usr/bin/python3 /usr/local/bin/python3; \
    \
    # --- 精简 Python 标准库 ---
    # 这些都是运行时用不到的（已用 sys.modules 实测：导入本项目全部模块后
    # 它们一个都没被加载）：
    #   ensurepip       自带 pip wheel（本项目不用 pip）
    #   lib2to3         已废弃的 py2→3 转换器
    #   pydoc_data      pydoc 帮助文本
    #   idlelib/tkinter IDLE 与 GUI 工具包
    #   unittest        单元测试框架
    #   multiprocessing 多进程
    #   distutils       构建工具（py3.12 已移除）
    #   config-3.12-*   编译头文件（只有要编译 C 扩展才需要）
    #   test            自测套件
    # 只删纯 Python 部分；lib-dynload 下的 C 扩展一律保留。
    PD=/usr/lib/python3.12; \
    rm -rf \
        "$PD/ensurepip" \
        "$PD/lib2to3" \
        "$PD/pydoc_data" \
        "$PD/idlelib" \
        "$PD/tkinter" \
        "$PD/unittest" \
        "$PD/multiprocessing" \
        "$PD/distutils" \
        "$PD/test" \
        "$PD/site-packages"; \
    rm -rf "$PD"/config-3.12*; \
    \
    # --- 拷我们的代码（从 bind mount 读，不进层）---
    mkdir -p /app/shell /app/ui /app/docker /app/scripts; \
    cp -r /src/shell/. /app/shell/; \
    cp -r /src/ui/. /app/ui/; \
    cp /src/gen_icons.py /app/scripts/gen_icons.py; \
    cp /src/entrypoint.sh /app/docker/entrypoint.sh; \
    cp /src/healthcheck.sh /app/docker/healthcheck.sh; \
    \
    # --- 图标是构建产物，仓库里没有，必须现场生成 ---
    # （.gitignore 忽略 app/ui/images/icon_*.png 与 app/shell/web/icon.png）
    # 否则管理页 logo 的 /icon.png 会 404（网页左上角图标空白）
    python3 /app/scripts/gen_icons.py /app/ui/images; \
    cp -f /app/ui/images/icon_256.png /app/shell/web/icon.png; \
    \
    # --- 统一换行符与执行位（Windows 检出可能带 CRLF）---
    sed -i 's/\r$//' /app/docker/entrypoint.sh /app/docker/healthcheck.sh /app/shell/lib.sh; \
    chmod 755 /app/docker/entrypoint.sh /app/docker/healthcheck.sh /app/shell/lib.sh; \
    \
    # --- 清理构建残留（同层删除才能真正省体积）---
    rm -rf /app/scripts; \
    find /app -name '__pycache__' -type d -prune -exec rm -rf {} + ; \
    rm -rf /var/cache/apk/* /tmp/*

# 数据目录（配置 + 数据库 + 核心）与日志目录分离：
#   /data      持久化配置与核心 —— 迁移只需拷这个
#   /data/log  默认日志目录（建议单独挂卷，迁移时不拷）
#
# 故意不设 PANEL_PORT / SERVICE_PORT / LOG_DIR：
# 一旦写进 ENV 就"永远存在"，入口会认为用户每次都在显式指定，
# 从而把管理页里改过的设置打回默认值。
ENV VNT_PKGVAR=/data \
    AUTO_DOWNLOAD_CORE=1 \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data/log

VOLUME ["/data"]

# 19098 管理页 / 29872 组网服务（TCP+UDP）/ 29071 控制台内部
EXPOSE 19098 29872/tcp 29872/udp 29071

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD /app/docker/healthcheck.sh

ENTRYPOINT ["/sbin/tini", "--", "/app/docker/entrypoint.sh"]
