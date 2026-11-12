# Changelog

本文件记录 gh-accel 的重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-11-13

### 新增

- **本地 registry mirror（Docker 镜像地址统一收进项目）**。代理在同一端口上
  同时提供 HTTP 代理和 registry mirror，dockerd 的 `registry-mirrors` 只需
  指向 `http://127.0.0.1:19770`。上游列表存在
  `~/.config/gh-accel/registries.json`，由 `gh-accel registry` 管理。
  - 上游自动故障转移：连接失败或 5xx 换下一个；404 视为确定性结果。
  - 认证懒加载：先不带 token 请求，只有 401 才去换；换不到 token 时跳过该
    上游而不是把 401 传给 daemon。
  - `referrers` 查询返回空 index，避免 Docker 24+ 回退到被 DNS 污染的
    `registry-1.docker.io` 白等 60 秒（实测 pull 63s → 1.1s）。
  - 新增 CLI：`gh-accel registry [--test|--add-upstream|--remove-upstream|
    --reset|--daemon-config]`。
- **Docker 兜底支持**：`scripts/docker-proxy.sh` 可让 dockerd 的 registry
  流量走本代理（默认不启用）。MITM 新增 ghcr.io / quay.io 路由，走 passthrough。
- **CA bundle**：新增「系统 CA + 本地 CA」的合集
  （`~/.config/gh-accel/certs/ca-bundle.pem`），由 `gh-accel install` 生成。

### 修复

- **服务无法正常停止**。`SIGTERM` 的处理函数里直接调用 `server.shutdown()`，
  而信号处理就在 `serve_forever()` 的主线程里执行 —— 自己等自己，必然死锁，
  只能等 systemd 的 90 秒超时后 SIGKILL。改为另起线程调用。
- **透传大响应体改为流式**。此前 `_forward` 把上游响应全量读进内存，且用
  `read(n)`（读满 n 字节的语义），导致 Docker 的镜像层这类上百 MB 的
  响应在经代理转发时卡死、内存占用随响应体线性增长。
- **Authorization 头不再被丢弃**。容器 registry 的流程是「先换 token，再带
  Authorization 拉 blob」，头丢了 blob 请求直接 401。
- **绝对 URI 支持**（`GET http://host/path`），标准 HTTP 代理语义，明文模式
  下也能代理任意站点。

### 重要说明

`cacert` / `SSL_CERT_FILE` 都是**替换**默认 CA 列表而非追加。只配置本地 CA
会导致直连任何其它站点都因缺根证书而校验失败（表现为连接卡死或
certificate verify failed），很容易被误判成网络问题。因此客户端一律使用
CA bundle。

- **源池可配置**：源从写死改为注册表 + 用户配置文件
  （`~/.config/gh-accel/sources.json`）。可用 `gh-accel sources` 增删源、
  停用/启用内置源、调整优先级。一个源可同时服务多条链路
  （release / page / raw / api），各链路 URL 模板分别给出。
- **源配置热重载**：代理每次取源前检查配置文件是否变化，改完立即生效，
  无需重启 systemd 服务。

## [1.0.0] - 2026-11-13

首个正式版本。

### 新增

- **GitHub release 加速**：国内镜像优先（中科大 `github-release`），多源并发
  竞速选最快，下载后做长度校验，写入本地缓存。
- **MITM 模式**：支持 `CONNECT` 隧道，用本地 CA 签发的证书终结 TLS，从而能把
  加速能力开放给 curl / wget / git / python / uv 等任意支持 `https_proxy` 的
  客户端，而不只是 mise。
- **明文模式**：mise 通过 `url_replacements` 把 URL 改写到 `/gh/`、`/raw/`、
  `/api/` 前缀。
- **`gh-accel` CLI**：`status` / `test` / `sources` / `clean` / `logs` 五个子命令。
- **`gh-accel-get`**：命令行下载 GitHub release 资产与 raw 文件。
- **`gh-accel-env`**：导出 `https_proxy`、`SSL_CERT_FILE` 等环境变量。
- **systemd user unit**：常驻运行、开机自启、崩溃自动重启。
- **其它生态的国内镜像**：Node.js / npm / PyPI / Go / Conda / Rust。

### 实现要点

- 部分加速服务会**截断大文件**（返回 `206` 但 `Content-Range` 声明的总大小
  远大于实际收到的字节数）。下载后必须校验长度，不符则换源，避免下游
  checksum 校验失败。
- 用「**完整下载竞速**」而非「先测速再下载」：测速阶段连接开销占比高且速度
  波动剧烈，曾出现探测 2.4MB/s、实际下载 60KB/s 的情况。
- CA 必须带 `basicConstraints` / `keyUsage` / `subjectKeyIdentifier`，否则
  Python 的 OpenSSL 会以 `Missing Authority Key Identifier` 拒绝整条证书链。
- `openssl -extfile` 必须写在具名 section 里并用 `-extensions` 指定，否则整个
  扩展文件被忽略，证书将没有 SAN。
- CA 在服务启动时预生成：客户端在 TLS 握手前就要读取 CA 文件。
- 证书签发加锁 + 原子替换：并发请求同一 host 时会互相覆盖，客户端可能读到
  半成品证书。
- 转发上游响应必须补 `Content-Length`：上游常 chunked 编码，urllib 解码后剥离
  了 `Transfer-Encoding`，不补长度且不关连接会让客户端一直等到超时。
- 只宣告 `http/1.1` 的 ALPN：代理按 HTTP/1.1 处理请求。
- CONNECT 请求要读完整头部：只读 requestline 会让残留的明文 HTTP 头被 TLS
  握手当成协议数据。

### 安全

- CA 私钥与证书存放在 `~/.config/gh-accel/certs/`，**不放在** `~/.cache/` ——
  XDG 语义下后者是可被清理工具删除的非重要数据，CA 一旦被清理，所有客户端的
  信任会同时失效。
- CA 只写入用户级配置（`~/.gitconfig`、`~/.curlrc`、`~/.config/wget/wgetrc`），
  **不会**装进系统信任库，因此系统级其它程序不受影响。

[1.1.0]: https://github.com/ezunai/gh-accel/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/ezunai/gh-accel/releases/tag/v1.0.0
