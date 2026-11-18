# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-11-18

首个版本。

### 功能

- **配置生成**：安装向导只问两个端口（管理页 / 组网服务），其余全部在应用内完成
- **核心托管**：自动下载、启动、停止、崩溃自动拉起 `vnts2`
- **在线更新**：从 GitHub Releases 按架构下载核心，带进度显示，失败自动回滚
- **手动上传**：网络不通时可自行下载核心后上传安装（裸二进制，带 ELF 校验）
- **网络代理与镜像**：支持 SOCKS5 / SOCKS5H / HTTP / HTTPS 代理；可配镜像前缀加速
- **服务端参数配置**：虚拟网段、白名单、租期、管理员账号，写入 `config.toml`
- **控制台同源内嵌**：vnts2 控制台反代在 `/console/` 下，iframe 内嵌、地址栏保持飞牛域名
- **服务器配置导入导出**：打包 `config.toml`、`settings.json`、数据库与证书；导入前自动备份、失败自动回滚
- **日志目录分离**：日志写独立目录，`server/` 内只有日志配置、没有日志数据；迁移不必带日志
- **桌面入口**：一个图标「VNT 服务端」，所有操作都在其中完成
- **Docker 部署**：同一套 `app/shell` 代码可直接跑在容器里，数据卷与日志卷分离

### 技术要点

- **一个 fpk 通吃 x86_64 与 ARM**：vnts2 是运行时按 `uname -m` 下载的静态 musl 二进制
- **升级不丢数据**：核心存于 `var/bin/`，重装 fpk 不影响
- **子路径托管 SPA**：处理 `<base>`、`Content-Type` 大小写、Vite `__vite__mapDeps`
  分块表（**不加前导斜杠**，否则 Vite 的 `assetsURL` 会造出 `//` 丢域名）、
  API 前缀由运行时 fetch/XHR 拦截器处理
- **并发安全**：进程操作整体持可重入锁（避免 restart 与巡检线程竞态）；
  前端所有关键操作加全局忙碌锁
- **HTTP 细节**：HEAD 的 `Content-Length` 置 0（否则连接失步）；
  监听队列放宽到 128（控制台切路由会并行拉取多个分块）

### 测试

- `scripts/test_e2e.py`：端到端 29 项
- `scripts/test_import_export.py`：导入导出 21 项（含 zip-slip 防护）
- `scripts/test_console.py`：控制台反代 41 项
- `scripts/test_console_real.py`：真实前端结构全链路 24 项（素材取自 vnts 二进制）
- `scripts/test_concurrency.py`：并发与连接复用 8 项
- `scripts/probe_vite_url.js`：用真实 Vite 算法验证改写产物
