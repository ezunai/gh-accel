"""配置与常量。

所有可调参数集中在此，环境变量前缀统一为 ``GH_ACCEL_``。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = [
    "Source",
    "Config",
    "load_config",
    "DEFAULT_SOURCES",
    "CHAINS",
    "RELEASE_PATH_RE",
    "RAW_PATH_RE",
    "API_PATH_RE",
    "GH_PATH_RE",
    "GIT_PATH_RE",
    "MITM_ROUTES",
]

#: 下载链路。源可以声明适用于其中一条或多条。
CHAINS = ("release", "page", "raw", "api")


@dataclass(frozen=True)
class Source:
    """一个下载源。

    同一个加速服务在不同链路上的 URL 形态不同（如 release 用
    ``{owner}/{repo}/{tag}/{file}``，页面用 ``{path}``），因此按链路
    分别给出模板。源名全局唯一。

    ``priority`` 越小越靠前（默认 100）。
    """

    name: str
    templates: dict[str, str]
    priority: int = 100
    builtin: bool = True

    @property
    def chains(self) -> tuple[str, ...]:
        return tuple(self.templates)

    def url(self, chain: str, **kwargs: str) -> str:
        return self.templates[chain].format(**kwargs)

    def serves(self, chain: str) -> bool:
        return chain in self.templates


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
    #: 用户自定义的源配置文件
    sources_file: str
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
    # 容器 registry：没有对应的加速链路（各加速服务对 registry 的 blob
    # 域名返回 403/404），走 passthrough 直连即可。
    "ghcr.io": "passthrough",
    "quay.io": "passthrough",
}


# ---- 内置源注册表 ----------------------------------------------------------
def _src(name: str, priority: int, **templates: str) -> Source:
    """构造内置源。``templates`` 的键是链路名。"""
    return Source(name=name, templates=dict(templates), priority=priority, builtin=True)


_GH = "https://{host}/{owner}/{repo}/releases/download/{tag}/{file}"
_GH_PATH = "https://{host}/{path}"

#: 内置源。中科大镜像最快（10-40MB/s）但只覆盖部分仓库；各加速服务作为
#: 兜底；direct 永远排最后。
DEFAULT_SOURCES: tuple[Source, ...] = (
    _src(
        "ustc",
        10,
        release="https://mirrors.ustc.edu.cn/github-release/{owner}/{repo}/{tag}/{file}",
    ),
    _src(
        "gh-proxy.cc",
        20,
        release="https://gh-proxy.cc/https://github.com"
        "/{owner}/{repo}/releases/download/{tag}/{file}",
        page="https://gh-proxy.cc/https://github.com/{path}",
        raw="https://gh-proxy.cc/https://raw.githubusercontent.com/{path}",
        api="https://gh-proxy.cc/https://api.github.com/{path}",
    ),
    _src(
        "ghfast.top",
        30,
        release="https://ghfast.top/https://github.com"
        "/{owner}/{repo}/releases/download/{tag}/{file}",
        page="https://ghfast.top/https://github.com/{path}",
        raw="https://ghfast.top/https://raw.githubusercontent.com/{path}",
        api="https://ghfast.top/https://api.github.com/{path}",
    ),
    _src(
        "gh-proxy.com",
        40,
        release="https://gh-proxy.com/https://github.com"
        "/{owner}/{repo}/releases/download/{tag}/{file}",
        page="https://gh-proxy.com/https://github.com/{path}",
        raw="https://gh-proxy.com/https://raw.githubusercontent.com/{path}",
    ),
    _src(
        "ghproxy.imciel.com",
        50,
        release="https://ghproxy.imciel.com/https://github.com"
        "/{owner}/{repo}/releases/download/{tag}/{file}",
    ),
    _src(
        "ghproxy.cfd",
        60,
        release="https://ghproxy.cfd/https://github.com"
        "/{owner}/{repo}/releases/download/{tag}/{file}",
    ),
    _src(
        "ghproxy.net",
        70,
        release="https://ghproxy.net/https://github.com"
        "/{owner}/{repo}/releases/download/{tag}/{file}",
    ),
    _src(
        "gitclone.com",
        80,
        page="https://gitclone.com/github.com/{path}",
    ),
    _src(
        "direct",
        999,
        release="https://github.com/{owner}/{repo}/releases/download/{tag}/{file}",
        page="https://github.com/{path}",
        raw="https://raw.githubusercontent.com/{path}",
        api="https://api.github.com/{path}",
    ),
)


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
    config_home = os.path.expanduser(os.environ.get("GH_ACCEL_CONFIG", "~/.config/gh-accel"))
    return Config(
        host=os.environ.get("GH_ACCEL_HOST", "127.0.0.1"),
        port=_env_int("GH_ACCEL_PORT", 19770),
        cache_dir=os.path.expanduser(os.environ.get("GH_ACCEL_CACHE", "~/.cache/gh-accel")),
        certs_dir=os.path.join(config_home, "certs"),
        sources_file=os.path.join(config_home, "sources.json"),
        timeout=_env_float("GH_ACCEL_TIMEOUT", 25.0),
        probe_timeout=_env_float("GH_ACCEL_PROBE_TIMEOUT", 12.0),
        probe_max_duration=_env_float("GH_ACCEL_PROBE_MAX", 5.0),
        probe_bytes=_env_int("GH_ACCEL_PROBE_BYTES", 1024 * 1024),
        max_cache_bytes=_env_int("GH_ACCEL_MAX_CACHE", 2 * 1024**3),
        race_limit=_env_int("GH_ACCEL_RACE_LIMIT", 4),
    )
