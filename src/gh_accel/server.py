"""HTTP 服务：明文路由 + MITM（CONNECT 隧道）。

两种接入模式：

* **明文** —— mise 的 ``url_replacements`` 把 URL 改写到 ``/gh/``、``/raw/``、
  ``/api/`` 前缀下。
* **MITM** —— 客户端发 ``CONNECT host:443``，代理用本地 CA 签发的证书终结
  TLS，从而能读到明文路径并改写。非 GitHub 的 host 原样转发（passthrough）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import socketserver
import urllib.error
from http.server import BaseHTTPRequestHandler

from .cache import FileCache, SourcePreference
from .certificates import CertificateAuthority
from .config import (
    API_PATH_RE,
    GH_PATH_RE,
    GIT_PATH_RE,
    MITM_ROUTES,
    RAW_PATH_RE,
    RELEASE_PATH_RE,
    Config,
)
from .fetcher import Fetcher
from .sources import SourceRegistry, load_registry

__all__ = ["ProxyServer", "make_server"]

log = logging.getLogger(__name__)


class ProxyHandler(BaseHTTPRequestHandler):
    """处理代理请求。"""

    protocol_version = "HTTP/1.1"
    server_version = "gh-accel/1.0"

    # 由 make_server 注入
    config: Config
    fetcher: Fetcher
    registry: SourceRegistry
    cache: FileCache
    preference: SourcePreference
    authority: CertificateAuthority

    # ---- 基础设施 --------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        pass  # 访问日志由 logging 统一控制

    def _err(self, code: int, msg: str) -> None:
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _client_headers(self) -> dict[str, str]:
        wanted = (
            "Range",
            "If-None-Match",
            "If-Modified-Since",
            "Accept",
            "Accept-Encoding",
            "X-GitHub-Api-Version",
        )
        out: dict[str, str] = {}
        for name in wanted:
            value = self.headers.get(name)
            if value:
                out[name] = value
        return out

    # ---- 回放 ------------------------------------------------------------
    def _send_file(
        self, path: str, size: int, content_type: str | None, extra: dict[str, str] | None = None
    ) -> None:
        """把本地文件回放给客户端，支持 Range。"""
        start, end = 0, size - 1
        partial = False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng.strip()[len("bytes=") :]
            first, _, last = spec.partition("-")
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:
                start = max(0, size - int(last))
            end = min(end, size - 1)
            partial = True
        length = max(end - start + 1, 0)

        self.send_response(206 if partial else 200)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _forward(self, response) -> None:
        """把上游响应透传给客户端。

        两种情况要分开处理：

        * **上游带 Content-Length** —— 直接流式转发。响应体可能很大
          （Docker 的镜像层上百 MB），全量缓冲会吃光内存，且客户端要等到
          全部收完才看到第一个字节。
        * **上游是 chunked** —— urllib 解码后剥离了 Transfer-Encoding 头，
          此时没有确切长度可用。若不补长度且不关连接，客户端会一直等待
          响应结束（git/curl 表现为超时）。这种情况才全量缓冲。
        """
        content_length = response.headers.get("Content-Length")
        streaming = content_length is not None and content_length.isdigit()

        if streaming:
            self._send_stream(response, int(content_length))
            return

        try:
            data = response.read()
        except Exception:  # noqa: BLE001
            data = b""
        finally:
            with contextlib.suppress(Exception):
                response.close()

        self.send_response(response.status)
        content_type = response.headers.get("Content-Type")
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name in (
            "ETag",
            "Last-Modified",
            "Content-Disposition",
            "Cache-Control",
            "X-GitHub-Request-Id",
        ):
            value = response.headers.get(name)
            if value:
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and data:
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _send_stream(self, response, length: int) -> None:
        """流式转发上游响应体，不在内存里缓冲。

        上游已声明确切长度，因此直接沿用该长度并边读边写，客户端能立刻
        收到首字节，内存占用也与响应体大小无关。
        """
        self.send_response(response.status)
        content_type = response.headers.get("Content-Type")
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name in (
            "ETag",
            "Last-Modified",
            "Content-Disposition",
            "Cache-Control",
            "X-GitHub-Request-Id",
        ):
            value = response.headers.get(name)
            if value:
                self.send_header(name, value)
        self.end_headers()
        if self.command == "HEAD":
            with contextlib.suppress(Exception):
                response.close()
            return
        try:
            remaining = length
            while remaining > 0:
                # 必须用 read1 而不是 read：read(n) 是「读满 n 字节」的语义，
                # 上游分批推送时会一直阻塞，导致客户端迟迟收不到数据
                chunk = response.read1(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            with contextlib.suppress(Exception):
                response.close()

    # ---- 明文路由 --------------------------------------------------------
    def _sources(self, chain: str):
        """取某条链路的源，必要时先热重载配置。"""
        self.registry.reload_if_changed()
        return self.registry.for_chain(chain)

    def _serve_release(self, key: dict[str, str], headers: dict[str, str]) -> None:
        canonical = ("https://github.com/{owner}/{repo}/releases/download/{tag}/{file}").format(
            **key
        )
        cached = self.cache.get(canonical)
        if cached:
            size = os.path.getsize(cached)
            log.info(
                "release %s/%s/%s/%s <- 缓存命中 (%d bytes)",
                key["owner"],
                key["repo"],
                key["tag"],
                key["file"],
                size,
            )
            self._send_file(cached, size, "application/octet-stream")
            return

        result = self.fetcher.race(self._sources("release"), "release", key, headers)
        if result is None:
            self._err(404, "not found in any source")
            return
        path = self.cache.store(canonical, result.path)
        self.preference.remember(self._release_key(key), result.source)
        log.info(
            "release %s/%s/%s/%s <- %s (%d bytes)",
            key["owner"],
            key["repo"],
            key["tag"],
            key["file"],
            result.source,
            result.size,
        )
        self._send_file(
            path, result.size, result.content_type or "application/octet-stream", result.headers
        )

    def _serve_raw(self, path: str, headers: dict[str, str]) -> None:
        canonical = "https://raw.githubusercontent.com/" + path
        cached = self.cache.get(canonical)
        if cached:
            size = os.path.getsize(cached)
            log.info("raw %s <- 缓存命中 (%d bytes)", path, size)
            self._send_file(cached, size, "text/plain; charset=utf-8")
            return

        result = self.fetcher.race(self._sources("raw"), "raw", {"path": path}, headers)
        if result is None:
            self._err(404, "not found in any source")
            return
        stored = self.cache.store(canonical, result.path)
        log.info("raw %s <- %s (%d bytes)", path, result.source, result.size)
        self._send_file(
            stored, result.size, result.content_type or "text/plain; charset=utf-8", result.headers
        )

    def _serve_api(self, path: str, headers: dict[str, str]) -> None:
        canonical = "https://api.github.com/" + path
        cached = self.cache.get(canonical)
        if cached:
            size = os.path.getsize(cached)
            log.info("api %s <- 缓存命中 (%d bytes)", path, size)
            self._send_file(cached, size, "application/json")
            return

        for source in self._sources("api"):
            url = source.url("api", path=path)
            result = self.fetcher.download(url, headers)
            if result is None:
                log.info("源 %s: 失败或长度不符", source.name)
                continue
            result.source = source.name
            stored = self.cache.store(canonical, result.path)
            log.info("api %s <- %s (%d bytes)", path, source.name, result.size)
            self._send_file(
                stored, result.size, result.content_type or "application/json", result.headers
            )
            return
        self._err(404, "not found")

    def _serve_page(self, path: str, headers: dict[str, str], body: bytes | None = None) -> None:
        """github.com 的其它路径（git smart http 的 info/refs、网页等）。

        直接按顺序尝试完整请求，不做探测 —— 探测既慢又会重复下载。
        """
        for source in self._sources("page"):
            url = source.url("page", path=path)
            try:
                response = self.fetcher.open(
                    url, _clean(headers), "GET" if body is None else "POST", data=body
                )
            except Exception:  # noqa: BLE001
                continue
            log.info("page %s <- %s", path[:60], source.name)
            self._forward(response)
            return
        self._err(502, "all sources failed")

    def _handle(self) -> None:
        if getattr(self, "_mitm", False):
            self._handle_mitm()
            return

        # 标准 HTTP 代理语义：请求行可以是绝对 URI（GET http://host/path）
        if self.path.startswith("http://"):
            self._serve_absolute(self.path)
            return

        path = self.path.split("?", 1)[0]
        headers = self._client_headers()

        match = RELEASE_PATH_RE.match(path)
        if match:
            self._serve_release(match.groupdict(), headers)
            return
        match = RAW_PATH_RE.match(path)
        if match:
            self._serve_raw(match.group("path"), headers)
            return
        match = API_PATH_RE.match(path)
        if match:
            self._serve_api(match.group("path"), headers)
            return
        match = GIT_PATH_RE.match(path)
        if match:
            self._serve_page(match.group("path"), headers)
            return
        match = GH_PATH_RE.match(path)
        if match:
            self._serve_page(match.group("path"), headers)
            return
        self._err(404, "unknown route")

    def do_GET(self) -> None:  # noqa: N802
        self._guarded(self._handle)

    def do_HEAD(self) -> None:  # noqa: N802
        self._guarded(self._handle)

    def do_POST(self) -> None:  # noqa: N802
        self._guarded(self._handle)

    def _guarded(self, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("请求处理失败")
            self._err(502, f"proxy error: {type(exc).__name__}: {exc}")

    # ---- CONNECT / MITM --------------------------------------------------
    def handle_one_request(self) -> None:
        """拦截 CONNECT。

        在读取 requestline 之前用 MSG_PEEK 探测，此时 socket 缓冲是干净的；
        CONNECT 分支自己逐字节读请求，不让 rfile 的缓冲吞掉 TLS 握手数据。
        """
        try:
            peek = self.connection.recv(1, socket.MSG_PEEK)
        except Exception:  # noqa: BLE001
            peek = b""
        if peek == b"C":
            self._handle_connect()
            return
        super().handle_one_request()

    def _handle_connect(self) -> None:
        conn = self.connection
        # 读完整个 CONNECT 请求（requestline + 头部），否则残留的明文 HTTP 头
        # 会被 TLS 握手当成协议数据，导致 WRONG_VERSION_NUMBER
        buf = b""
        while not buf.endswith(b"\r\n\r\n"):
            try:
                chunk = conn.recv(1)
            except Exception:  # noqa: BLE001
                return
            if not chunk:
                return
            buf += chunk
            if len(buf) > 16384:
                return
        try:
            target = buf.decode("latin1").split("\r\n")[0].split()[1]
        except Exception:  # noqa: BLE001
            return
        host = target.rpartition(":")[0] or target

        try:
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except Exception:  # noqa: BLE001
            return

        context = self.authority.tls_context(host)
        if context is None:
            return
        try:
            tls = context.wrap_socket(conn, server_side=True)
        except Exception as exc:  # noqa: BLE001
            log.info("TLS 握手失败 (%s): %s", host, exc)
            return

        self.connection = tls
        self.rfile = tls.makefile("rb", -1)
        self.wfile = tls.makefile("wb", 0)
        self.close_connection = False
        self._mitm = True
        try:
            while not self.close_connection:
                super().handle_one_request()
        except Exception:  # noqa: BLE001
            pass
        finally:
            with contextlib.suppress(Exception):
                tls.close()

    def _handle_mitm(self) -> None:
        """在 TLS 隧道内处理一条 HTTP 请求。

        requestline 与头部已由 BaseHTTPRequestHandler 解析完毕。
        """
        method = self.command
        path = self.path
        headers = {k.lower(): v for k, v in self.headers.items()}
        host = headers.get("host", "").split(":")[0]
        body = self._read_body(headers)

        kind, payload = self._mitm_route(host, path)
        log.info("MITM %s %s%s -> %s", method, host, path[:70], kind)

        if kind == "release":
            self._serve_release(payload, headers)
            return
        if kind == "raw":
            self._serve_raw(payload, headers)
            return
        if kind == "api":
            self._serve_api(payload, headers)
            return
        if kind == "page":
            self._serve_page(payload, headers, body or None)
            return
        self._passthrough("https://" + host + path, headers, body)

    def _read_body(self, headers: dict[str, str]) -> bytes:
        length = headers.get("content-length")
        if length and length.isdigit():
            return self.rfile.read(int(length))
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = b""
            while True:
                size_line = self.rfile.readline().strip()
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                body += self.rfile.read(size)
                self.rfile.readline()
            return body
        return b""

    @staticmethod
    def _mitm_route(host: str, path: str) -> tuple[str, object]:
        """按 host + path 决定走哪条加速链路。"""
        kind = MITM_ROUTES.get(host)
        if kind == "release":
            match = RELEASE_PATH_RE.match(
                path.replace("/gh/", "/", 1) if path.startswith("/gh/") else path
            )
            if not match:
                # /gh/<owner>/<repo>/releases/download/... 形式
                match = RELEASE_PATH_RE.match("/gh" + path)
            if match:
                return "release", match.groupdict()
            return "page", path.lstrip("/")
        if kind == "raw":
            return "raw", path.lstrip("/")
        if kind == "api":
            return "api", path.lstrip("/")
        return "passthrough", path

    def _serve_absolute(self, uri: str) -> None:
        """处理绝对 URI 形式的请求（标准 HTTP 代理语义）。

        ``GET http://host/path HTTP/1.1`` —— 客户端把完整 URL 交给代理，
        代理原样转发。这让明文模式下也能代理任意站点，不限于 GitHub。
        """
        headers = self._client_headers()
        self._passthrough(uri, headers, None)

    def _passthrough(self, url: str, headers: dict[str, str], body: bytes | None) -> None:
        """任意 URL 原样转发（保持通用代理能力）。"""
        host = url.split("://", 1)[-1].split("/", 1)[0]
        path = (
            "/" + url.split("://", 1)[-1].split("/", 1)[1]
            if "/" in url.split("://", 1)[-1]
            else "/"
        )
        try:
            response = self.fetcher.open(url, _clean(headers), self.command, data=body or None)
        except urllib.error.HTTPError as exc:
            # 上游的 4xx/5xx 是有效响应，应原样透传 —— 否则客户端看到的是
            # 一个不存在的 502，掩盖了「资源确实不存在」这个事实
            log.info("MITM passthrough %s%s -> HTTP %s", host, path[:60], exc.code)
            self._forward(exc)
            return
        except Exception as exc:  # noqa: BLE001
            log.info("MITM passthrough %s%s 失败: %s", host, path[:60], exc)
            self._err(502, str(exc))
            return
        self._forward(response)

    @staticmethod
    def _release_key(key: dict[str, str]) -> str:
        return f"release:{key['owner']}/{key['repo']}"


def _clean(headers: dict[str, str]) -> dict[str, str]:
    from .fetcher import clean_headers

    return clean_headers(headers)


class ProxyServer(socketserver.ThreadingTCPServer):
    """代理服务器。"""

    allow_reuse_address = True
    daemon_threads = True


def make_server(config: Config) -> ProxyServer:
    """构造并返回配置好的服务器。"""
    fetcher = Fetcher(config)
    registry = load_registry(config.sources_file)
    cache = FileCache(config.file_cache_dir, config.max_cache_bytes)
    preference = SourcePreference(config.source_cache_file)
    authority = CertificateAuthority(config.cert_dir, config.ca_pem, config.ca_key)

    handler = type(
        "BoundProxyHandler",
        (ProxyHandler,),
        {
            "config": config,
            "fetcher": fetcher,
            "registry": registry,
            "cache": cache,
            "preference": preference,
            "authority": authority,
        },
    )
    return ProxyServer((config.host, config.port), handler)
