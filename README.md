# VNT 服务端（飞牛 fnOS 插件）

把 [vnt-dev/vnts](https://github.com/vnt-dev/vnts) 的服务端核心 `vnts2` 封装成飞牛 fnOS 的 `.fpk` 应用。

**这是一个「壳」**：不含任何 vnts 二进制，只负责五件事——托管 vnts2 服务端进程、在线更新核心、配置服务端参数、导入导出服务器配置、把日志写到独立目录。**网络的创建、设备白名单与查看由 vnts2 自带控制台完成**（同源内嵌，用控制台自带登录页登录）。

> 与「VNT 组网」（`vnt-nas`，客户端版）的区别：本应用是**服务端**。客户端连的是对方的服务端地址；装了本应用的那台机器就是**被连接的组网节点服务**。

> **命名说明**：仓库与制品名为 `fnos-vnts2`（对应 `fnos-vnts2.fpk`、
> Docker 镜像 `ghcr.io/virkingz/fnos-vnts2`）；飞牛包名 `appname = vnts-nas`，
> 故安装路径为 `/var/apps/vnts-nas/`、网关前缀为 `/app/vnts-nas`。

## 特性

- **安装极简**：向导只问两个端口（均有默认值），其余全部应用内设置
- **核心自动更新**：从 GitHub Releases 按架构拉取，带进度、失败自动回滚
- **手动上传兜底**：网络不通时自行下载核心上传安装
- **控制台同源内嵌**：vnts2 控制台内嵌在管理页里，地址栏保持飞牛域名
- **服务器配置导入导出**：一键打包 config.toml + 数据库，迁移/备份都方便
- **日志与配置分离**：日志写独立目录，**迁移时不用管日志，导出也不含日志**
- **一个包通吃 x86_64 与 ARM**：二进制在运行时按架构下载
- **升级不丢数据**：已下载的核心、配置与数据库不受 fpk 升级影响
- **Docker 一键部署**：默认拉取 GHCR 多架构镜像 `:latest`，也可本地构建

## 下载

到 [Releases](https://github.com/virkingz/fnos-vnts2/releases) 下载最新的 `fnos-vnts2.fpk`。

## Docker 部署

不装飞牛也能用。同一套 `app/shell` 代码可直接跑在 Docker 里：

```bash
cd docker
docker compose up -d
```

默认从 GHCR 拉取多架构官方镜像 `ghcr.io/virkingz/fnos-vnts2:latest`
（打 tag 时自动更新）。想本地构建：注释掉 compose 里的 `image:`、
取消 `build:` 注释，再 `docker compose up -d --build`。

访问 `http://<主机>:19098` 即为管理页。完整说明见 **[docker/README.md](docker/README.md)**。

**日志卷分离**（本编排已默认配置）：数据卷 `vnts-data`（配置+数据库+核心）与
日志卷 `vnts-logs` 分开挂载，迁移时只备份 `vnts-data` 即可。

## 分工

| 谁 | 负责什么 |
|----|----------|
| **本插件** | 安装；设置端口、网段、白名单、管理员账号；启动/停止核心；检查并更新核心；导入导出配置；查看日志 |
| **vnts2 控制台** | 创建网络编号、管理设备与白名单、查看节点与路由、下发订阅配置 |

## 安装

飞牛桌面 → 应用中心 → 右上角菜单 → 安装本地应用 → 选择 `fnos-vnts2.fpk`。

或命令行：

```bash
appcenter-cli install-fpk fnos-vnts2.fpk
```

**安装时填两个端口**（都有默认值，通常直接下一步即可）：

| 端口 | 默认 | 用途 |
|------|------|------|
| 管理页端口 | 19098 | 打开「VNT 服务端」时访问的端口 |
| 组网服务端口 | 29872 | 客户端连接本服务端所用的端口（TCP+UDP 同号） |

安装过程**不下载任何二进制**，所以装得很快，也不受网络影响。

## 使用（四步）

**第一步：下载核心**

从飞牛桌面点「VNT 服务端」打开管理页 → 「核心更新」标签 → 点「检查更新」。
确认版本后点「下载核心」，进度条走完即完成。

> 下载源不可用时，先到同页下方「下载源与代理」填镜像前缀，再重试。

**第二步：配置服务端**

「服务端设置」→ 设置默认虚拟网段、白名单、控制台管理员账号。
首次安装已生成默认配置（网段 `10.26.0.0/24`，账号 `admin/admin`，**建议立即改密码**）。

**第三步：启动核心**

「核心更新」→ 点「启动核心」，状态变为「核心运行中」。

**第四步：组网**

「控制台」→ 已内嵌打开 vnts2 控制台（自动登录，无需填账号）。
在控制台里创建网络编号、添加设备、查看节点与路由。

客户端（装了「VNT 组网」的设备）填入：

- **服务端地址**：`你的NAS地址:29872`（或 `quic://你的NAS地址:29872`）
- **组网编号**：与你在控制台创建的一致
- **组网密码**：建议填写，节点间端到端加密

所有需要互联的设备填**相同**的这三项，启动后即组成虚拟局域网。

## 端口与访问方式

| 端口 | 默认 | 用途 | 是否需要对外 |
|------|------|------|--------------|
| 管理页端口 | 19098 | 核心更新、服务端设置、导入导出、日志、内嵌控制台 | 局域网访问即可 |
| 组网服务端口 | 29872 | 客户端连接（TCP over TLS / QUIC / WSS，TCP+UDP 同号） | **需路由器/防火墙放行 TCP+UDP** |
| 控制台内部端口 | 29071 | vnts2 的 web_bind，仅回环，由管理页反代 | 否 |

> **务必放行 29872 的 TCP 与 UDP** —— 少放一个会导致客户端打洞失败或连不上。

### 控制台如何内嵌

管理页把 vnts2 控制台反代在 `/console/` 下。子路径托管 SPA 需处理四处，
缺一都会导致「控制台空白」「弹下载框」或「侧栏可见但内容区空白」：

1. **`Content-Type` 必须正确透传** —— 上游（axum）发的是**小写** `content-type`，
   若用大小写敏感的方式取值会取不到，退化成 `application/octet-stream`，
   浏览器就会**把 HTML 当文件下载**（表现为：页面空白 + 弹出保存框）。
2. **HTML 资源路径改写** —— 入口 `/assets/index-*.js`、`/assets/index-*.css`、
   `/favicon.svg` 都需加 `/console` 前缀（`<base>` 对绝对路径无效）。
3. **Vite 预加载分块表改写（最易错）** —— Vite 把各视图分块列在
   `__vite__mapDeps` 里，路径形如 **`assets/SettingsView-*.js`（不带前导斜杠）**。
   这些由 Vite 的 modulepreload 机制动态插入 `<link>`，**不经过 fetch/XHR 拦截器**。
   改写时**绝不能加前导斜杠**，因为 Vite 运行时会经 `assetsURL` 处理，其实现为
   `dd = e => '/' + e`（**无条件加一个前导斜杠**）。若写成 `"/console/assets/x.js"`，
   `dd()` 后变成 `//console/assets/x.js` —— 浏览器按**协议相对 URL** 解析，
   主机名成了 `console` → **丢域名、DNS 失败、分块全部加载不了**。
   正确写法是补**不带前导斜杠**的前缀：`console/assets/x.js`，
   `dd()` 正好把斜杠补回来。

   同时注意：**HTML 里的** `src="/assets/index-*.js"` 是绝对路径、不经 `dd()`，
   那类仍需补**带前导斜杠**的前缀。同一份响应里两种形态并存，必须分别处理。
   若漏了这一步，典型症状是：**主 JS 能加载（侧栏可见），
   但点进各视图时内容区空白**（Network 面板里那些分块显示 failed）。
4. **API 前缀由运行时拦截器处理** —— vnts2 前端是 `fetch('/api' + path)`，
   `path` 字面量形如 `"/networks"`。**若在文本层改写 `/api`，会拼出
   `/console/api/console/networks` 这种错误地址**。因此文本层只碰完整资源路径，
   `/api` 交给注入的 fetch/XHR 拦截器 —— 运行时拿到的是完整 URL，不会误伤片段。

进入控制台时会显式落在 `#/networks`（网络管理），
避免沿用上次停留的视图。

反代层的两个服务端细节也已加固（否则会在 Network 面板表现为
「(failed) + 200」成对记录，点进某些视图偶发空白）：

- **HEAD 请求的 `Content-Length` 必须为 0** —— HEAD 要返回与 GET 相同的响应头、
  但不带 body。若按 GET 的长度声明却一个字节都不写，客户端会一直等那些字节，
  导致连接失步/超时，浏览器报 `net::ERR_*` 后重试。Vite 的 modulepreload
  会对 `<link>` 发 `fetch`，这类探测请求会受影响。
- **监听队列放宽到 128** —— 控制台切换路由会**并行**拉取约 6 个懒加载分块
  （js/css），Python 默认 `request_queue_size` 仅 5，容易让多余连接被内核拒掉。

### 控制台登录

控制台有**自带的登录页**，用「服务端设置」里的管理员账号登录：

- 默认 `admin` / `admin`（**建议立即修改**）
- 登录态由控制台自己管（localStorage，key `jwt_token`），本应用不介入身份

> 设计取舍：反代层只负责「路径」，不掺和「身份」。这样边界清晰、问题面小。
> 控制台页面的顶栏会显示当前应使用的登录账号，方便对照。

### 控制台打不开时

「控制台」标签页会**自动检测就绪状态**：

| 情形 | 页面表现 |
|------|----------|
| 核心未下载 | 提示去「核心更新」下载，并给出一键跳转 |
| 核心已下载未运行 | 提示点「启动核心」（页内直接有按钮） |
| 核心运行中但端口未就绪 | 显示「正在就绪…」，**几秒后自动加载**，并附核心日志尾部 |
| 就绪 | 自动加载控制台，绿色圆点 + 「控制台已加载」 |

> iframe 一旦加载就不会因核心后来启动而自动刷新，因此这里用**轮询等待 + 就绪后自动加载**，
> 而不是固定 `src`。

## 服务器配置导入导出

「配置导入导出」标签（也可在 Docker 下用）：

- **导出**：打包 `config.toml`、`settings.json`、`network_control.db`（网络/设备记录）、
  证书文件为一个 zip。**不含日志**，也不含核心二进制。用于迁移到新机器或定期备份。
- **导入**：选择之前导出的 zip。导入前**自动备份**现有 `server/` 目录，失败**自动回滚**。
  导入后核心自动重启。

### 迁移到新机器的推荐流程

1. 旧机器 →「配置导入导出」→ 导出 zip
2. 新机器装好本应用 → 先下载核心
3. 新机器 →「配置导入导出」→ 导入 zip
4. **日志无需迁移**（本来就在独立目录，且可能很大）

## 日志与配置分离

| | 位置 | 迁移是否要带 |
|---|---|---|
| **配置** | `$TRIM_PKGVAR/server/`（config.toml、数据库、证书） | ✅ 用导出功能带走 |
| **日志** | 独立日志目录（默认 `$TRIM_PKGVAR/log`，可改到任意路径） | ❌ 不需要 |

设计要点：vnts2 自身的日志用 log4rs 写入「工作目录/logs」。本应用**预先写好**
`server/logs/log4rs.yaml`，把日志 appender 指向日志目录（绝对路径），因此：

- 日志数据**不会**留在 `server/` 里 —— 导出 `server/` 自然不含日志
- `server/logs/` 里只有一份几百字节的 `log4rs.yaml`（配置，随导出带走）
- 想把日志挪到别的盘，改「配置导入导出 → 日志目录」即可，立即生效

日志单个超过 **512 KB** 自动原地截断（保留最近 256 KB）。用「原地截断」而非「改名轮转」，
是因为 vnts2 以追加方式持有文件句柄，改名会让它继续写旧 inode。

## 核心更新

vnts 官方发版频繁。管理页「核心更新」标签：

1. 点「检查更新」→ 显示当前版本与最新版本，并给出建议动作
2. 按提示点「下载核心」或「更新到 vx.y.z」

更新失败会**自动回滚**到上一个版本，不会把环境弄坏。

### 手动上传核心

网络不通或想指定版本时使用：

1. 从 [Releases](https://github.com/vnt-dev/vnts/releases) 下载对应架构的
   `vnts2-<架构>-unknown-linux-musl-v<版本>` 文件（**注意：无扩展名**）
2. 在「手动上传核心」区域选择文件 → 点「上传并安装」

上传会做 ELF 魔数校验，不是 Linux 可执行文件会明确提示。

> vnts 发布的是**裸二进制**（不是 zip），文件名形如
> `vnts2-x86_64-unknown-linux-musl-v2.0.9`，约 6–8 MB。
> 架构映射：`x86_64→x86_64-unknown-linux-musl`，
> `aarch64→aarch64-unknown-linux-musl`，`armv7l→armv7-unknown-linux-musleabihf`。

## 下载源与网络代理

「下载源与代理」里有**两种互相独立**的出网配置，可只配其一，也可同时使用：

| 配置 | 作用层 | 说明 |
|------|--------|------|
| **镜像前缀** | URL 层 | 把下载地址改写到镜像站，如 `https://ghfast.top` |
| **网络代理** | 网络层 | 让请求经代理隧道出去，适合本机需要代理才能上外网 |

代理格式：`socks5h://` / `socks5://` / `http://` / `https://`，省略协议按 `http` 处理。
点「测试连通性」会实际访问 GitHub 并报告耗时。

> 配置代理后所有出网请求统一走 `curl`（Python 标准库的 urllib 不支持 SOCKS5）；
> 未配置代理时用 urllib，不额外启动进程。

## 打包与测试

```bash
bash scripts/pack.sh            # 产出 fnos-vnts2.fpk
bash scripts/pack.sh 1.0.1      # 指定版本

python scripts/test_e2e.py            # 端到端（29 项）
python scripts/test_import_export.py  # 导入导出专项（21 项）
python scripts/test_console.py        # 控制台反代与控制台联动（39 项）
python scripts/test_console_real.py   # 真实前端结构全链路（21 项，素材取自 vnts 二进制）
python scripts/test_concurrency.py    # 并发与连接复用（8 项）
```

> **Windows 打包注意**：`pack.sh` 会把 `cmd/*` 强制设为 755 并转 LF，跨平台更稳。

## 目录结构

```
vnts-nas/
├── manifest                  # 应用元数据
├── ICON.PNG / ICON_256.PNG
├── app/                      # → 安装后的 TRIM_APPDEST (target/)
│   ├── ui/config             # 桌面入口
│   └── shell/                # 壳本体
│       ├── lib.sh            # 架构映射/下载/端口/进程/日志目录
│       ├── vnts_config.py    # config.toml 与 log4rs.yaml 生成
│       ├── shell_main.py     # 服务入口
│       ├── shell_server.py   # 进程托管、更新器、导入导出
│       ├── shell_http.py     # HTTP 路由、控制台反代
│       └── web/              # 管理页前端
├── cmd/                      # 生命周期脚本
├── config/                   # privilege（root）+ resource
├── wizard/                   # 安装/配置/升级/卸载向导
├── docker/                   # Docker 部署（复用同一份 app/shell）
├── scripts/                  # gen_icons / pack / test_e2e
└── .github/workflows/        # CI：打包 fpk 与构建多架构镜像
```

## 运行时数据

```
$TRIM_PKGVAR/            # /vol{n}/@appdata/vnts-nas
├── bin/
│   ├── vnts2             # 核心（在线更新，不属于 fpk）
│   ├── panel_token       # 管理页令牌（0600）
│   └── .vnts_version     # 当前核心版本
├── server/               # vnts2 工作目录（CWD）
│   ├── config.toml       # 服务端配置
│   ├── settings.json     # 管理页表单的持久化形式
│   ├── network_control.db# 网络与设备记录（SQLite）
│   └── logs/log4rs.yaml  # 仅日志配置，无日志数据
├── cache/                # 下载缓存 + 回滚备份 + 导入前备份
├── log/                  # 默认日志目录（可用 LOG_DIR 改到别处）
└── ports.env             # PANEL_PORT / SERVICE_PORT / CONSOLE_PORT
```

## 为什么做成壳

- 核心下载到 `var/bin/`，**升级插件不会覆盖它**
- vnts 发新版时，管理页点一下即可，不用重装 fpk
- 一个 fpk 通吃 x86 与 ARM（二进制在运行时按架构下载）

## 权限说明

绑定 500/4500 之类的特权端口或使用 IKEv2 需要 root；`config/privilege` 用 `run-as: root`。
若只做常规组网（29872），理论上普通用户即可，但为保证 IKEv2/WireGuard 可选功能可用，
默认给 root（与同类应用一致）。

## 排障

| 现象 | 处理 |
|------|------|
| 核心启动失败 | 「日志」看 `vnts2.log`；管理页会提示原因（如二进制架构不符） |
| 检查更新报网络错误 | 在「下载源与代理」填镜像前缀后重试 |
| 客户端连不上 | 确认 29872 的 **TCP 与 UDP 都已放行**；组网编号/服务端地址一致 |
| 控制台空白/弹下载框 | 旧版本曾因 Content-Type 取值问题触发（已修）；升级到本版即可 |
| 控制台要求登录 | 用「服务端设置」里的管理员账号（默认 `admin`/`admin`） |
| 更新后组网不通 | 更新失败会自动回滚；备份在 `cache/vnts2.bak` |
| 想换日志盘 | 「配置导入导出 → 日志目录」改路径，立即生效 |

## 上游

- vnts 项目：https://github.com/vnt-dev/vnts
- vnt 客户端：https://github.com/vnt-dev/vnt
- 飞牛开发文档：https://developer.fannas.com/
