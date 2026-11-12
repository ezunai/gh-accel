"""配置与常量。

所有可调参数集中在此，环境变量前缀统一为 ``GH_ACCEL_``。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = [
    "Host",
    "Source",
    "Config",
    "load_config",
    "RELEASE_PATH_RE",
    "RAW_PATH_RE",
    "API_PATH_RE",
    "GH_PATH_RE",
    "GIT_PATH_RE",
    "MITM_ROUTES",
]


@dataclass(frozen=True)
class Source:
    """一个下载源。

    ``template`` 里的占位符依链路而定：

    * release: ``{owner} {repo} {tag} {file}``
    * 其它:     ``{path}``
    """

    name: str
    template: str

    def url(self, **kwargs: str) -> str:
        return self.template.format(**kwargs)


@dataclass(frozen=True)
class Host:
    """一个 MITM 路由目标。"""

    host: str
    kind: str  # release | raw | api


# ---- 路径正则 --------------------------------------------------------------
RELEASE_PATH_RE = re.compile(
    r"^/gh/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/"
    r"(?P<tag>[^/]+)/(?P<file>.+)$"
)
RAW_PATH_RE = re.compile(r"^/raw/(?P<path>.+)$")
API_PATH_RE = re.compile(r"^/api/(?P<path>.+)$")
GH_PATH_RE = re.compile(r"^/gh/(?P<path>.+)$")
GIT_PATH_RE = re.compile(r"^/git/(?P<path>.+)$")

#: MITM（CONNECT 隧道）模式下，哪些 host 走加速链路
MITM_ROUTES = {
    "github.com": "release",
    "raw.githubusercontent.com": "raw",
    "api.github.com": "api",
}


# ---- 源池 ------------------------------------------------------------------
#: GitHub release 资产。中科大镜像是最快的（10-40MB/s），但只覆盖部分仓库。
RELEASE_SOURCES: tuple[Source, ...] = (
    Source("ustc", "https://mirrors.ustc.edu.cn/github-release/{owner}/{repo}/{tag}/{file}"),
    Source(
        "gh-proxy.cc",
        "https://gh-proxy.cc/https://github.com/{owner}/{repo}/releases/download/{tag}/{file}",
    ),
    Source(
        "ghfast.top",
        "https://ghfast.top/https://github.com/{owner}/{repo}/releases/download/{tag}/{file}",
    ),
    Source(
        "gh-proxy.com",
        "https://gh-proxy.com/https://github.com/{owner}/{repo}/releases/download/{tag}/{file}",
    ),
    Source(
        "ghproxy.imciel.com",
        "https://ghproxy.imciel.com/https://github.com/{owner}/{repo}/releases/download/{tag}/{file}",
    ),
    Source(
        "ghproxy.cfd",
        "https://ghproxy.cfd/https://github.com/{owner}/{repo}/releases/download/{tag}/{file}",
    ),
    Source(
        "ghproxy.net",
        "https://ghproxy.net/https://github.com/{owner}/{repo}/releases/download/{tag}/{file}",
    ),
    Source("direct", "https://github.com/{owner}/{repo}/releases/download/{tag}/{file}"),
)

#: github.com 的其它路径（git smart http 的 info/refs、网页等）
PAGE_SOURCES: tuple[Source, ...] = (
    Source("ghfast.top", "https://ghfast.top/https://github.com/{path}"),
    Source("gh-proxy.com", "https://gh-proxy.com/https://github.com/{path}"),
    Source("gh-proxy.cc", "https://gh-proxy.cc/https://github.com/{path}"),
    Source("gitclone.com", "https://gitclone.com/github.com/{path}"),
    Source("direct", "https://github.com/{path}"),
)

#: raw.githubusercontent.com 文本
RAW_SOURCES: tuple[Source, ...] = (
    Source("gh-proxy.cc", "https://gh-proxy.cc/https://raw.githubusercontent.com/{path}"),
    Source("ghfast.top", "https://ghfast.top/https://raw.githubusercontent.com/{path}"),
    Source("gh-proxy.com", "https://gh-proxy.com/https://raw.githubusercontent.com/{path}"),
    Source("direct", "https://raw.githubusercontent.com/{path}"),
)

#: api.github.com
API_SOURCES: tuple[Source, ...] = (
    Source("gh-proxy.cc", "https://gh-proxy.cc/https://api.github.com/{path}"),
    Source("ghfast.top", "https://ghfast.top/https://api.github.com/{path}"),
    Source("direct", "https://api.github.com/{path}"),
)


@dataclass(frozen=True)
class Config:
    """代理的运行期配置。"""

    host: str
    port: int
    #: 可随时删除的缓存（下载的文件、源偏好）
    cache_dir: str
    #: 持久凭据（CA 私钥与证书）。不能放在 cache_dir —— 缓存被清理工具
    #: 删除后，客户端（curl/wget/git）的信任会同时失效。
    certs_dir: str
    #: 上游请求超时（秒）
    timeout: float
    #: 竞速探测的单源超时（秒）
    probe_timeout: float
    #: 竞速探测的总时长上限（秒）
    probe_max_duration: float
    #: 探测时读取的样本字节数
    probe_bytes: int
    #: 下载缓存总大小上限（字节）
    max_cache_bytes: int
    #: 并发竞速时同时启动的源数量
    race_limit: int

    @property
    def file_cache_dir(self) -> str:
        return os.path.join(self.cache_dir, "files")

    @property
    def cert_dir(self) -> str:
        return self.certs_dir

    @property
    def source_cache_file(self) -> str:
        return os.path.join(self.cache_dir, "sources.json")

    @property
    def ca_pem(self) -> str:
        return os.path.join(self.certs_dir, "ca.pem")

    @property
    def ca_key(self) -> str:
        return os.path.join(self.certs_dir, "ca.key")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def load_config() -> Config:
    """从环境变量读取配置。"""
    return Config(
        host=os.environ.get("GH_ACCEL_HOST", "127.0.0.1"),
        port=_env_int("GH_ACCEL_PORT", 19770),
        cache_dir=os.path.expanduser(os.environ.get("GH_ACCEL_CACHE", "~/.cache/gh-accel")),
        certs_dir=os.path.expanduser(os.environ.get("GH_ACCEL_CERTS", "~/.config/gh-accel/certs")),
        timeout=_env_float("GH_ACCEL_TIMEOUT", 25.0),
        probe_timeout=_env_float("GH_ACCEL_PROBE_TIMEOUT", 12.0),
        probe_max_duration=_env_float("GH_ACCEL_PROBE_MAX", 5.0),
        probe_bytes=_env_int("GH_ACCEL_PROBE_BYTES", 1024 * 1024),
        max_cache_bytes=_env_int("GH_ACCEL_MAX_CACHE", 2 * 1024**3),
        race_limit=_env_int("GH_ACCEL_RACE_LIMIT", 4),
    )
