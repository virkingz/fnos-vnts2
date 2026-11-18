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

# python3   壳本体与配置生成
# bash      lib.sh / entrypoint 依赖 bash 语法
# curl      下载核心（SOCKS 代理只能靠 curl）
# openssl   令牌生成；核心会生成自签证书
# tini      PID 1，回收僵尸并转发信号
RUN apk add --no-cache \
        python3 \
        bash \
        curl \
        openssl \
        ca-certificates \
        tzdata \
        tini \
    && ln -sf /usr/bin/python3 /usr/local/bin/python3

WORKDIR /app

COPY app/shell /app/shell
COPY app/ui    /app/ui
COPY scripts/gen_icons.py    /app/scripts/gen_icons.py
COPY docker/entrypoint.sh    /app/docker/entrypoint.sh
COPY docker/healthcheck.sh   /app/docker/healthcheck.sh

# 图标是构建产物（.gitignore 忽略 app/ui/images/icon_*.png 与 app/shell/web/icon.png），
# 仓库里根本没有这两组文件。fpk 由 pack.sh 现场生成，Docker 必须在此补上，
# 否则管理页 logo 的 /icon.png 会 404（网页左上角图标空白）。
# 纯标准库，alpine 无需额外依赖。
RUN python3 /app/scripts/gen_icons.py /app/ui/images \
    && cp -f /app/ui/images/icon_256.png /app/shell/web/icon.png

RUN sed -i 's/\r$//' /app/docker/entrypoint.sh /app/docker/healthcheck.sh /app/shell/lib.sh \
    && chmod 755 /app/docker/entrypoint.sh /app/docker/healthcheck.sh /app/shell/lib.sh

RUN find /app/shell -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true

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
