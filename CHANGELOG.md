# Changelog

本文件记录 gh-accel 的重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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

[Unreleased]: https://github.com/osmos/gh-accel/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/osmos/gh-accel/releases/tag/v1.0.0
