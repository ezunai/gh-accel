"""本地 registry mirror —— 给 Docker 用的镜像地址。

为什么需要它
------------

本机的 ``registry-1.docker.io`` 与 ``auth.docker.io`` 都被 DNS 污染了
（解析到 ``face:b00c`` 这类 Facebook IPv6 段），直连必然失败，所以
Docker 必须用 ``registry-mirrors`` 指向第三方镜像服务。

但第三方镜像是公益服务，随时可能挂、限速或被停。本模块提供一个**本地**
mirror 端点：daemon 只需要认一个地址（``http://127.0.0.1:19771``），
由 mirror 负责挑选上游、故障转移、缓存。

协议要点
--------

Docker daemon 与 mirror 之间只说 registry 协议：

* ``GET /v2/<name>/manifests/<ref>``  —— 必须原样透传 ``Content-Type``，
  daemon 靠它判断 manifest 的类型；
* ``GET /v2/<name>/blobs/<digest>``   —— 内容寻址，必须与 digest 一致。

mirror 不替 daemon 做认证：上游若需要 token，由 mirror 自行换取（见
``token_service``）。匿名可拉的上游（多数国内镜像）无需这一步。

注意 daemon 的 ``registry-mirrors`` **只对 Docker Hub 生效**；ghcr.io 等
其它 registry 仍需让 dockerd 走 HTTP 代理（见 ``docker-proxy.sh``）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
from dataclasses import dataclass, field

from .fetcher import Fetcher

__all__ = [
    "RegistrySpec",
    "RegistryConfig",
    "MirrorRegistry",
    "load_registry_config",
    "make_mirror",
]

log = logging.getLogger(__name__)

#: /v2/<name>/manifests/<ref> 与 /v2/<name>/blobs/<digest>
MANIFEST_RE = re.compile(r"^/v2/(?P<name>.+)/manifests/(?P<ref>.+)$")
BLOB_RE = re.compile(r"^/v2/(?P<name>.+)/blobs/(?P<digest>.+)$")
#: OCI 1.1 的 referrers 查询（签名、SBOM、attestation）
REFERRERS_RE = re.compile(r"^/v2/(?P<name>.+)/referrers/(?P<digest>[^/]+)$")

#: referrers 的空结果。Docker 24+ 拉镜像时会先查 referrers；若 mirror 回
#: 404，daemon 会**回退到原始 registry**（registry-1.docker.io）再查一次，
#: 而它在本机被 DNS 污染，会白等 60 秒。返回一个合法的空 index 即可阻止
#: 这次回退。第三方镜像本来也不保证转发完整的签名数据，返回空是合理取舍。
EMPTY_REFERRERS = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [],
    }
).encode()

#: manifest 与 blob 的请求头需要原样转发
FORWARD_HEADERS = (
    "Accept",
    "Authorization",
    "Accept-Encoding",
    "Range",
    "If-None-Match",
    "If-Modified-Since",
)

#: 响应里要透传的头
RESPONSE_HEADERS = (
    "Content-Type",
    "Docker-Content-Digest",
    "ETag",
    "Last-Modified",
    "Content-Disposition",
    "WWW-Authenticate",
)


@dataclass(frozen=True)
class RegistrySpec:
    """一个镜像组：同一组的上游互相等价，可故障转移。"""

    name: str
    #: 上游地址，按优先级排列
    upstreams: tuple[str, ...]
    #: 换 token 的地址（``?service=..&scope=..`` 由调用方追加）
    token_service: str = ""
    #: 换 token 时用的 service 参数
    service: str = ""

    def token_url(self, scope: str) -> str:
        if not self.token_service:
            return ""
        sep = "&" if "?" in self.token_service else "?"
        return f"{self.token_service}{sep}service={self.service}&scope={scope}"


@dataclass
class RegistryConfig:
    """镜像组集合。"""

    specs: dict[str, RegistrySpec] = field(default_factory=dict)
    path: str = ""
    #: 默认镜像组（Docker daemon 只有一个 registry-mirrors 入口）
    default: str = "dockerhub"

    def for_path(self, name: str) -> RegistrySpec | None:
        """按仓库名选镜像组。

        目前所有组都服务 Docker Hub 命名空间，故统一返回默认组；保留这个
        钩子是为了将来支持按前缀分流。
        """
        return self.specs.get(self.default)

    def save(self) -> None:
        data = {
            "default": self.default,
            "groups": {
                spec.name: {
                    "upstreams": list(spec.upstreams),
                    "token_service": spec.token_service,
                    "service": spec.service,
                }
                for spec in self.specs.values()
            },
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)


#: 内置镜像组。dockerproxy.net 匿名可用且直接返回 200（不需要 token），
#: 排最前；其余作为故障转移的备选。
DEFAULT_GROUPS: dict[str, dict[str, object]] = {
    "dockerhub": {
        "upstreams": (
            "https://dockerproxy.net",
            "https://docker.1ms.run",
            "https://docker.m.daocloud.io",
        ),
        "token_service": "https://auth.docker.io/token",
        "service": "registry.docker.io",
    },
}


def load_registry_config(path: str) -> RegistryConfig:
    """读取镜像组配置，与内置的合并。"""
    specs: dict[str, RegistrySpec] = {
        name: RegistrySpec(name=name, **kwargs)  # type: ignore[arg-type]
        for name, kwargs in DEFAULT_GROUPS.items()
    }
    default = "dockerhub"

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return RegistryConfig(specs=specs, path=path, default=default)
    except (OSError, ValueError) as exc:
        log.warning("镜像组配置解析失败 (%s): %s，使用内置配置", path, exc)
        return RegistryConfig(specs=specs, path=path, default=default)

    if not isinstance(data, dict):
        return RegistryConfig(specs=specs, path=path, default=default)

    default = data.get("default") or default
    for name, spec in (data.get("groups") or {}).items():
        if not isinstance(spec, dict):
            continue
        upstreams = spec.get("upstreams")
        if not isinstance(upstreams, list) or not upstreams:
            continue
        upstreams = tuple(u for u in upstreams if isinstance(u, str) and u)
        if not upstreams:
            continue
        specs[name] = RegistrySpec(
            name=name,
            upstreams=upstreams,
            token_service=str(spec.get("token_service") or ""),
            service=str(spec.get("service") or ""),
        )

    return RegistryConfig(specs=specs, path=path, default=default)


class MirrorRegistry:
    """上游镜像组的选择、认证与取数。"""

    def __init__(self, config: RegistryConfig, fetcher: Fetcher, cache_dir: str) -> None:
        self._config = config
        self._fetcher = fetcher
        self._cache_dir = cache_dir
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    # ---- token -----------------------------------------------------------
    def _token(self, spec: RegistrySpec, scope: str) -> str:
        """换取匿名 pull token，带缓存。"""
        url = spec.token_url(scope)
        if not url:
            return ""
        with self._lock:
            cached = self._tokens.get(url)
            if cached and cached[1] > time.time():
                return cached[0]

        token = ""
        try:
            response = self._fetcher.open(url, timeout=8)
            try:
                payload = json.loads(response.read().decode("utf-8"))
                token = payload.get("token") or payload.get("access_token") or ""
            finally:
                response.close()
        except Exception as exc:  # noqa: BLE001
            log.info("换取 token 失败 (%s): %s", spec.name, exc)
            return ""

        if token:
            with self._lock:
                self._tokens[url] = (token, time.time() + 300)
        return token

    # ---- 取数 ------------------------------------------------------------
    def fetch(self, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str], object]:
        """向各上游取一次，返回 (状态码, 响应头, 响应体或响应对象)。

        认证是**懒加载**的：先不带 token 请求，只有上游回 401 才换 token
        重试。多数国内镜像匿名可拉，预先换 token 纯属浪费 —— 而
        ``auth.docker.io`` 在本机被 DNS 污染（解析到 Facebook 的 IPv6
        段），那次请求会白等十几秒。

        上游策略：

        * 连接失败 / 5xx —— 换下一个上游（故障转移）
        * 404 —— 确定性结果（镜像确实不存在），直接返回，不再试
        * 2xx —— 成功
        """
        spec = self._config.for_path(path)
        if spec is None:
            return 404, {"Content-Type": "application/json"}, b"no registry group"

        if REFERRERS_RE.match(path):
            return (
                200,
                {"Content-Type": "application/vnd.oci.image.index.v1+json"},
                EMPTY_REFERRERS,
            )

        scope = self._scope(path, spec)
        last_error = ""
        token_tried = False

        for upstream in spec.upstreams:
            url = upstream.rstrip("/") + path
            request_headers = {
                k: v
                for k, v in headers.items()
                if k.lower() in {h.lower() for h in FORWARD_HEADERS}
            }
            request_headers.pop("Accept-Encoding", None)
            request_headers["Accept-Encoding"] = "identity"

            response = self._open_upstream(url, request_headers)
            if response is None:
                last_error = "connection failed"
                continue

            status = getattr(response, "status", None)
            if status == 401:
                if not token_tried and spec.token_service:
                    # 上游要认证 —— 换 token 后重试这个上游
                    token_tried = True
                    with contextlib.suppress(Exception):
                        response.close()
                    token = self._token(spec, scope)
                    if token:
                        request_headers["Authorization"] = f"Bearer {token}"
                        response = self._open_upstream(url, request_headers)
                        if response is None:
                            last_error = "connection failed"
                            continue
                        status = getattr(response, "status", None)
                    else:
                        # 换不到 token —— 该上游在本机不可用，换下一个
                        # 而不是让 daemon 收到 401（auth.docker.io 被 DNS
                        # 污染时，换 token 会白等十几秒）
                        last_error = "token unavailable"
                        log.info("%s 需要 token 但换不到 -> 换下一个上游", upstream)
                        continue
                if status == 401:
                    with contextlib.suppress(Exception):
                        response.close()
                    return 401, {"Content-Type": "application/json"}, b"unauthorized"

            resp_headers = {
                name: response.headers.get(name)
                for name in RESPONSE_HEADERS
                if response.headers.get(name)
            }
            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                # 有确切长度 -> 直接透传响应对象（流式）
                return response.status, resp_headers, response
            data = response.read()
            resp_headers["Content-Length"] = str(len(data))
            return response.status, resp_headers, data

        return 502, {"Content-Type": "text/plain"}, (f"all upstreams failed: {last_error}".encode())

    def _open_upstream(self, url: str, headers: dict[str, str]):
        """请求上游。连接失败返回 None，HTTP 错误则返回其响应对象。"""
        try:
            return self._fetcher.open(url, headers, timeout=30)
        except urllib.error.HTTPError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001
            log.info("%s 不可用: %s", url, exc)
            return None

    @staticmethod
    def _scope(path: str, spec: RegistrySpec) -> str:
        """从请求路径推出 token 的 scope。"""
        match = MANIFEST_RE.match(path) or BLOB_RE.match(path)
        if not match:
            return ""
        name = match.group("name")
        # Docker Hub 的官方镜像带 library/ 前缀
        if spec.service == "registry.docker.io" and "/" not in name:
            name = f"library/{name}"
        return f"repository:{name}:pull"


def make_mirror(config) -> MirrorRegistry:
    """构造 registry mirror（上游选择 + 认证）。"""
    fetcher = Fetcher(config)
    registry_config = load_registry_config(config.registries_file)
    return MirrorRegistry(registry_config, fetcher, config.file_cache_dir)
