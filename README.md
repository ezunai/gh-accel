# mise-gh-proxy

本地 GitHub 下载加速代理。

本机直连 GitHub 极慢（release 资产实测 0 B/s、12 秒超时），本项目把 GitHub
下载导向本地代理，按「**国内镜像优先 → 多源并发竞速 → 长度校验 → 本地缓存**」
工作，并通过 **MITM（CONNECT 隧道）** 把加速能力开放给 curl / wget / git /
python 等客户端。

## 效果

| 场景 | 优化前 | 优化后 |
| --- | --- | --- |
| `mise install age@latest` | 11 分钟未完成 | 5-9 秒 |
| `mise install uv@latest` | 120 秒超时 | 6-9 秒 |
| curl 下载 19.4MB 安装包 | 0 B/s | 9.8 MB/s |
| `git clone`（octocat/Hello-World） | 极慢 / 失败 | 3.4 秒 |
| 下载缓存命中 | — | 2.1 GB/s |

产物 sha256 与 GitHub 官方 attestation 一致。

## 项目结构

```
mise-gh-proxy/
├── README.md
├── LICENSE                     MIT
├── Makefile                    开发辅助（install/test/lint/service）
├── pyproject.toml              包元数据与工具配置
├── systemd/
│   └── mise-gh-proxy.service   systemd user unit
├── scripts/
│   ├── mise-gh-get             命令行下载（走代理）
│   └── mise-gh-env             导出代理与 CA 环境变量
├── src/mise_gh_proxy/
│   ├── __init__.py
│   ├── __main__.py             入口：python -m mise_gh_proxy
│   ├── config.py               配置常量与源池
│   ├── certificates.py          本地 CA 与按 SNI 动态签发
│   ├── cache.py                源偏好缓存 + 文件缓存
│   ├── fetcher.py              探测、完整下载、并发竞速
│   ├── server.py               HTTP 服务（明文路由 + MITM）
│   └── cli.py                  mise-gh-mirror 的实现
└── tests/                      47 个测试
```

**源码与运行时数据分离**：源码在本目录；CA、下载缓存、源偏好记录在
`~/.cache/mise-gh-proxy/`。重建安装只需拷源码，不会丢 CA。

## 安装

```bash
# 开发依赖（pytest / ruff）
make dev

# 测试与静态检查
make check
```

命令行入口不依赖 pip 安装，用 wrapper 直接调用源码：

```bash
install -Dm755 scripts/mise-gh-get ~/.local/bin/mise-gh-get
install -Dm755 scripts/mise-gh-env ~/.local/bin/mise-gh-env
cat > ~/.local/bin/mise-gh-mirror <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ROOT="${MISE_GH_PROXY_HOME:-$HOME/Code/personal/mise-gh-proxy}"
exec env PYTHONPATH="$ROOT/src" python3 -m mise_gh_proxy.cli "$@"
EOF
chmod +x ~/.local/bin/mise-gh-mirror
```

服务安装：

```bash
make service      # 安装 unit + daemon-reload + restart
```

## 两种接入模式

### 模式一：明文 HTTP（mise 内部）

mise 的 `url_replacements` 把 URL 改写到 `/gh/`、`/raw/`、`/api/` 前缀：

```toml
# ~/.config/mise/config.toml
[settings.url_replacements]
'regex:^https://github\.com/' = "http://127.0.0.1:19770/gh/"
'regex:^https://raw\.githubusercontent\.com/' = "http://127.0.0.1:19770/raw/"
```

规则用 `regex:^` 锚定，避免误伤代理自身的 URL（替换不链式）。

**没有**重定向 `api.github.com`：重定向会丢弃 `Authorization` 头，且使 mise
无法确认仓库改名/转移，会破坏 GitHub attestations 与 SLSA provenance 校验。
实测 `api.github.com` 直连约 1.9 秒 / 19KB/s，元数据体量小，可接受。

### 模式二：MITM / CONNECT（外部客户端）

代理支持 `CONNECT host:443`，用本地 CA 签发的证书终结 TLS，因此能改写
URL 路径。客户端需信任该 CA：

```bash
eval "$(mise-gh-env)"     # 导出 https_proxy / SSL_CERT_FILE 等全套变量
```

MITM 下的路由：

| 目标 | 走向 |
| --- | --- |
| `github.com/<o>/<r>/releases/download/...` | release 加速链路（竞速 + 缓存 + 校验） |
| `github.com/...` 其它路径（git smart http、网页） | 页面加速链路 |
| `raw.githubusercontent.com/...` | raw 加速链路 |
| `api.github.com/...` | API 加速链路 |
| 其它 host | 原样转发（passthrough） |

## 各客户端配置

| 客户端 | 配置 | 说明 |
| --- | --- | --- |
| mise | `~/.config/mise/config.toml` | 模式一，已配置 |
| git | `~/.gitconfig`：`http.https://github.com/.proxy` + `http.sslCAInfo` | 按 URL 匹配，**仅 github.com** 走代理 |
| curl | `~/.curlrc`：`cacert` + `proxy` | 全局代理 |
| wget | `~/.config/wget/wgetrc`：`ca_certificate` + `https_proxy` | 全局代理 |
| python / uv / pip | `eval "$(mise-gh-env)"` | 按需启用 |
| npm / node / cargo / go | 各自的国内镜像 | 见下表 |

注意 wget 1.25 读的是 XDG 路径 `~/.config/wget/wgetrc`，不是 `~/.wgetrc`。
git 用按 URL 匹配的 `http.<url>.proxy`，所以只有 github.com 走代理，其它站点
完全不受影响。

curl / wget 的代理是全局的（非 GitHub 流量经代理 passthrough 转发，只多一跳
本地延迟）。要临时关闭：`curl --noproxy '*'`。

## 工作原理

```
请求 /gh/<owner>/<repo>/releases/download/<tag>/<file>
   │
   ├─ 本地缓存命中（按原始 GitHub URL 作键，跨源通用）→ 直接回放
   │
   └─ 未命中 → 并发向多个源发起**完整下载**
         ustc (mirrors.ustc.edu.cn/github-release)   国内镜像站, 16MB/s, 覆盖部分仓库
         gh-proxy.cc                                   2-9MB/s
         ghfast.top
         gh-proxy.com
         ghproxy.imciel.com
         ghproxy.cfd
         ghproxy.net
         direct (github.com)                          最后兜底
              │
              ├─ 首个「下载完成 + 长度校验通过」的源胜出 → 写入缓存 → 回放
              └─ 其余源被丢弃
```

### 为什么必须做长度校验

部分加速服务会**截断大文件**：返回 `206` 但 `Content-Range` 声明的总大小远大于
实际收到的字节数。例如 `gh-proxy.com` 对 19.4MB 的 age 安装包只返回 2.0MB。
若不做校验，mise 侧的 checksum 校验会失败（表现为安装报 digest mismatch）。
代理据此判定该源不可用并换源。

### 为什么用「完整下载竞速」而不是「先测速再下载」

测速阶段连接建立开销占比高、且各加速服务速度波动剧烈，实测出现过探测时
2.4MB/s、下载时只有 60KB/s 的情况。改为并发完整下载后，总耗时由最快源决定，
不再依赖探测的准确性。

### MITM 的几个坑（已处理）

- **CA 必须带 AKI/SKI 扩展** —— 部分客户端（如 Python 的 OpenSSL）会校验
  Authority Key Identifier，缺失直接拒绝整个证书链。
- **`-extfile` 必须写在具名 section 里**，并用 `-extensions <section>` 指定，
  否则 OpenSSL 会忽略整个扩展文件，证书会没有 SAN。
- **CA 要在服务启动时预生成** —— 客户端在 TLS 握手前就要读取 CA 文件。
- **证书签发要加锁 + 原子替换** —— 并发请求同一 host 时会互相覆盖，
  客户端可能读到半成品证书。
- **转发上游响应必须补 Content-Length** —— 上游常 chunked 编码，urllib
  解码后剥离了 `Transfer-Encoding`，不补长度且不关连接会让客户端一直等到超时。
- **只宣告 http/1.1 的 ALPN** —— 代理按 HTTP/1.1 处理请求。
- **CONNECT 请求要读完整头部** —— 只读 requestline 会让残留的明文 HTTP 头
  被 TLS 握手当成协议数据。

## 其它生态的国内镜像

| 生态 | 配置位置 | 镜像 |
| --- | --- | --- |
| Node.js | `~/.config/mise/config.toml` | `npmmirror.com` |
| npm | `~/.config/npm/npmrc` | `registry.npmmirror.com` |
| PyPI | `~/.config/mise/config.toml` | `pypi.tuna.tsinghua.edu.cn` |
| Go | `~/.config/go/env` | `goproxy.cn` |
| Conda | `~/.condarc` | `mirrors.tuna.tsinghua.edu.cn` |
| Rust | `~/.rustup/settings.toml` | `mirrors.ustc.edu.cn/rust-static` |

### 两处刻意保留直连

- **crates.io**：cargo 1.96 禁止替换 crates-io 源（无论 `replace-with` 还是直接改
  `registry`），配置后会直接报错。cargo 走稀疏索引本身可用。
- **NuGet**：华为云 Artifactory 镜像的 search 端点持续返回 429 限流，比直连
  `api.nuget.org` 更不可用，故保留默认直连。

## 命令行

```bash
mise-gh-mirror status     # 服务状态、缓存占用、各仓库上次可用源
mise-gh-mirror test       # 实测各候选源速度
mise-gh-mirror sources    # 列出源池与优先级
mise-gh-mirror clean      # 清理下载缓存
mise-gh-mirror logs       # 查看代理日志

mise-gh-get <github-url>  # 命令行走代理下载 release / raw 文件
eval "$(mise-gh-env)"      # 给当前 shell 注入代理与 CA 变量

systemctl --user status mise-gh-proxy.service
systemctl --user restart mise-gh-proxy.service
```

## 配置项

全部通过环境变量调整，前缀 `MISE_GH_PROXY_`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MISE_GH_PROXY_HOST` | `127.0.0.1` | 监听地址 |
| `MISE_GH_PROXY_PORT` | `19770` | 监听端口 |
| `MISE_GH_PROXY_CACHE` | `~/.cache/mise-gh-proxy` | 运行时数据目录 |
| `MISE_GH_PROXY_TIMEOUT` | `25` | 上游请求超时（秒） |
| `MISE_GH_PROXY_PROBE_TIMEOUT` | `12` | 竞速探测单源超时（秒） |
| `MISE_GH_PROXY_PROBE_MAX` | `5` | 探测总时长上限（秒） |
| `MISE_GH_PROXY_PROBE_BYTES` | `1048576` | 探测样本字节数 |
| `MISE_GH_PROXY_MAX_CACHE` | `2147483648` | 缓存总大小上限（字节） |
| `MISE_GH_PROXY_RACE_LIMIT` | `4` | 并发竞速的源数量 |

## 安全说明

MITM 意味着代理能看到并改写这些客户端的 HTTPS 明文。本地 CA 只被写入
`~/.gitconfig`、`~/.curlrc`、`~/.config/wget/wgetrc` 等**用户级**配置，
**没有**装进系统信任库，因此系统级的其它程序不受影响。若要装进系统信任库
（这样所有程序免配置即可使用），需要 root：

```bash
sudo cp ~/.cache/mise-gh-proxy/certs/ca.pem \
     /usr/local/share/ca-certificates/mise-gh-proxy.crt
sudo update-ca-certificates
```

不建议这么做：装进系统信任库后，所有程序的 HTTPS 流量都会经过这个代理。

## License

MIT
