# Docker 部署

不装飞牛也能用。同一套 `app/shell` 代码直接跑在容器里。

## 快速开始

```bash
cd docker
docker compose up -d
```

访问 `http://<主机>:19098` → 「核心更新」→ 下载并启动核心 → 「控制台」组网。

## 端口

| 容器端口 | 用途 | 是否必须映射 |
|----------|------|--------------|
| `19098/tcp` | 管理页（含内嵌控制台） | 是（局域网访问） |
| `29872/tcp` | 组网服务 TCP over TLS | **是** |
| `29872/udp` | 组网服务 QUIC | **是** |
| `29071/tcp` | 控制台内部端口（仅容器内回环） | 否 |

> 组网服务端口 **TCP 与 UDP 都要映射**，否则客户端打洞失败。

## 对外暴露与安全

管理页**没有登录页**，鉴权靠「入口令牌」（首页注入，之后请求带上）。
所以**访问控制要由外层负责** —— 常见做法是反代 + Basic Auth / 登录页。

> ⚠️ **反代认证只有在「19098 不直接对外」时才有效。**
> compose 默认写的是 `"19098:19098"`，即绑所有网卡。若你的主机有公网 IP
> 且防火墙放行了它，别人直连 `http://<IP>:19098` 就能**绕过反代**直接进管理页。

只让反代访问的话，把它改成绑本机：

```yaml
ports:
  - "127.0.0.1:19098:19098"   # 只有宿主机上的反代能连
  - "29872:29872/tcp"          # 组网端口保持对外
  - "29872:29872/udp"
```

> 注意：容器内 `PANEL_BIND` 必须是 `0.0.0.0`（否则反代连不进去），
> 对外暴露面由上面的**端口映射**控制。

## 日志与配置分离（本编排的核心设计）

**迁移时不想带日志**，因此日志与配置**分属两个卷**：

| 卷 | 挂载点 | 内容 | 迁移是否要带 |
|----|--------|------|--------------|
| `vnts-data` | `/data` | config.toml、数据库、核心二进制 | ✅ 只备份这个 |
| `vnts-logs` | `/data/log` | 日志 | ❌ 不需要 |

配置来自 `docker-compose.yml`：

```yaml
environment:
  LOG_DIR: "/data/log"     # 日志独立目录
volumes:
  - vnts-data:/data        # 配置 + 数据库 + 核心
  - vnts-logs:/data/log    # 日志，独立卷
```

环境变量 `LOG_DIR` 一旦设置，管理页的「日志目录」输入框会被锁定（避免两处配置打架），
并在界面上提示当前值来自环境变量。

### 迁移流程

```bash
# 旧机器：只导出数据卷（不含日志）
docker run --rm -v fnos-vnts2_vnts-data:/data -v "$PWD":/backup alpine \
    tar czf /backup/vnts-data.tgz -C /data .

# 或者更简单：用管理页「配置导入导出 → 导出为 zip」（只含配置与数据库）

# 新机器：恢复
docker run --rm -v fnos-vnts2_vnts-data:/data -v "$PWD":/backup alpine \
    sh -c "tar xzf /backup/vnts-data.tgz -C /data"
```

> 管理页导出的 zip 与数据卷内容的区别：zip 含 `server/` 下的配置、数据库与证书，
> **不含核心二进制**（新机器下载即可）。两者都不含日志。

### 不想要独立日志卷？

删掉 `LOG_DIR` 环境变量与 `vnts-logs` 卷即可。此时日志落在 `/data/log`，
**依然与配置分离**（`server/` 目录里始终没有日志），只是日志也进了数据卷。
迁移时如果想跳过它，加 `--exclude=./log` 即可：

```bash
tar czf vnts-data.tgz --exclude=./log -C /data .
```

## 环境变量

全部可选。**留空/注释 = 不覆盖**，会沿用你在管理页里改过的值。

| 变量 | 默认 | 说明 |
|------|------|------|
| `PANEL_PORT` | 19098 | 管理页端口 |
| `SERVICE_PORT` | 29872 | 组网服务端口（TCP+UDP） |
| `CONSOLE_PORT` | 29071 | 控制台内部端口 |
| `PANEL_TOKEN` | 随机 | 管理页令牌，留空则首次启动生成 |
| `LOG_DIR` | 空 | **日志目录**（独立于配置） |
| `MIRROR` | 空 | 下载镜像前缀，如 `https://ghfast.top` |
| `PROXY` | 空 | 网络代理，如 `http://192.168.1.10:7890` 或 `socks5h://192.168.1.10:1080`（支持 http/https/socks5/socks5h/socks4，由 curl 处理） |
| `AUTO_DOWNLOAD_CORE` | 1 | 启动时缺核心则自动下载 |
| `PANEL_BIND` | 0.0.0.0 | 管理页绑定地址（容器内必须是 0.0.0.0） |
| `TZ` | Asia/Shanghai | 时区 |

> `PROXY` 里的 `127.0.0.1` 指**容器自己**。用宿主机的代理要写
> `host.docker.internal` 或宿主机局域网 IP。

## 直接跑镜像（不用 compose）

```bash
docker run -d --name fnos-vnts2 \
  -p 19098:19098 -p 29872:29872/tcp -p 29872:29872/udp \
  -e LOG_DIR=/data/log \
  -v vnts-data:/data \
  -v vnts-logs:/data/log \
  --restart unless-stopped \
  ghcr.io/virkingz/fnos-vnts2:latest
```

## 多架构

`vnts2` 核心是运行时按 `uname -m` 下载的静态 musl 二进制，壳是纯 Python。
因此同一个镜像可在 amd64 / arm64 / armv7 上运行，无需区分构建。
（CI 会同时构建三种架构的镜像。）

## 排障

| 现象 | 处理 |
|------|------|
| 管理页打不开 | `docker logs fnos-vnts2`；确认 19098 已映射 |
| 客户端连不上 | 确认 29872 **TCP 与 UDP** 都映射了 |
| 核心下载失败 | 加 `MIRROR` 或 `PROXY` 后重启容器 |
| 想改端口 | 改 compose 的 ports 与对应环境变量，重启 |
| 日志太大 | 在 `LOG_DIR` 里删；或改挂载到更大的盘 |
