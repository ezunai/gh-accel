"""下载层：探测、完整下载、并发竞速。

两个关键设计：

**长度校验。** 部分加速服务会截断大文件（返回 ``206`` 但 ``Content-Range``
声明的总大小远大于实际收到的字节数）。不校验的话，mise 侧的 checksum 校验
会失败，表现为安装报 digest mismatch。这里据此判定该源不可用并换源。

**完整下载竞速，而非「先测速再下载」。** 测速阶段连接建立开销占比高、且各
加速服务速度波动剧烈，实测出现过探测时 2.4MB/s、实际下载只有 60KB/s 的情况。
改为并发完整下载后，总耗时由最快源决定，不再依赖探测的准确性。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import os
import tempfile
import threading
import time
import urllib.request
from collections.abc import Sequence

from .config import Config, Source

__all__ = [
    "DownloadResult",
    "Fetcher",
    "clean_headers",
    "expected_size",
]

log = logging.getLogger(__name__)

#: 转发给上游时必须剥离的头。逐跳头 + Host（否则加速服务收到错误的 Host）
#: + Accept-Encoding（否则落盘的是压缩流，长度校验虽过但内容无法解压）。
DROP_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "accept-encoding",
        "content-length",
        "content-encoding",
    }
)


class DownloadResult:
    """一次成功下载的结果。"""

    __slots__ = ("path", "size", "content_type", "headers", "source")

    def __init__(
        self, path: str, size: int, content_type: str | None, headers: dict[str, str], source: str
    ) -> None:
        self.path = path
        self.size = size
        self.content_type = content_type
        self.headers = headers
        self.source = source


def clean_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """剥离逐跳头，并强制 identity 编码。"""
    out = {k: v for k, v in (headers or {}).items() if k.lower() not in DROP_HEADERS}
    out["Accept-Encoding"] = "identity"
    return out


def expected_size(response) -> int | None:
    """从响应头推断资源总大小；未知返回 None。"""
    content_range = response.headers.get("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[1].strip()
        if total.isdigit():
            return int(total)
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        return int(length)
    return None


class Fetcher:
    """负责向上游取数据。"""

    def __init__(self, config: Config) -> None:
        self._config = config

    # ---- 底层请求 --------------------------------------------------------
    def open(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        timeout: float | None = None,
        data: bytes | None = None,
    ):
        """发起请求，返回 urllib 的响应对象。"""
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", "mise-gh-proxy/1.0")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        return urllib.request.urlopen(request, timeout=timeout or self._config.timeout)

    # ---- 探测 ------------------------------------------------------------
    def probe_speed(self, url: str, headers: dict[str, str] | None = None) -> float | None:
        """探测某源的下载速度（字节/秒）。不可用返回 None。

        连接建立的时间不计入速度计算，否则冷启动的开销会让快源被误判为慢源。
        """
        cfg = self._config
        probe_headers = clean_headers(headers)
        probe_headers["Range"] = f"bytes=0-{cfg.probe_bytes - 1}"
        started = time.time()
        try:
            response = self.open(url, probe_headers, timeout=cfg.probe_timeout)
        except Exception:  # noqa: BLE001 - 任何异常都视为该源不可用
            return None
        connected = time.time()
        read = 0
        eof = False
        try:
            while read < cfg.probe_bytes:
                if time.time() - started > cfg.probe_max_duration:
                    break
                chunk = response.read(256 * 1024)
                if not chunk:
                    eof = True
                    break
                read += len(chunk)
        except Exception:  # noqa: BLE001
            pass
        finally:
            with contextlib.suppress(Exception):
                response.close()
        if read == 0:
            return None
        if not eof and read < 1024:
            # 未读完就被中断且数据太少 —— 视为不可用
            return None
        read_dt = max(time.time() - connected, 1e-6)
        speed = read / read_dt
        if read < cfg.probe_bytes:
            # 源提前结束（可能截断），用总时间兜底
            speed = min(speed, read / max(time.time() - started, 1e-6))
        return speed

    def rank_sources(
        self, sources: Sequence[Source], key: dict[str, str], headers: dict[str, str] | None = None
    ) -> list[tuple[float, Source]]:
        """并发探测各源，返回按速度降序的 (speed, source)。"""

        def probe(source: Source) -> tuple[Source, float | None]:
            return source, self.probe_speed(source.url(**key), headers)

        results: list[tuple[float, Source]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(sources), 1)) as pool:
            for source, speed in pool.map(probe, sources):
                if speed is None:
                    log.info("源 %s: 不可用", source.name)
                    continue
                log.info("源 %s: %.0f KB/s", source.name, speed / 1024)
                results.append((speed, source))
        results.sort(key=lambda item: item[0], reverse=True)
        return results

    # ---- 下载 ------------------------------------------------------------
    def download(self, url: str, headers: dict[str, str] | None = None) -> DownloadResult | None:
        """完整下载到临时文件并校验长度。失败返回 None。

        下载时不带客户端的 Range 头 —— 缓存里必须是**完整**文件，否则后续
        GET 会拿到被截断的内容。切片在回放阶段按客户端 Range 做。
        """
        cfg = self._config
        os.makedirs(cfg.file_cache_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="mghp-", dir=cfg.file_cache_dir)
        os.close(fd)
        try:
            response = self.open(url, clean_headers(headers))
        except Exception:  # noqa: BLE001
            _unlink(tmp_path)
            return None

        total = expected_size(response)
        received = 0
        try:
            with open(tmp_path, "wb") as fh:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
        except Exception:  # noqa: BLE001
            _unlink(tmp_path)
            return None
        finally:
            with contextlib.suppress(Exception):
                response.close()

        # 完整性校验：实际字节数必须等于上游声明的总大小
        if total is not None and received != total:
            log.info("长度不符: 收到 %d / 声明 %d -> 丢弃", received, total)
            _unlink(tmp_path)
            return None

        content_type = response.headers.get("Content-Type")
        extra = {
            k: response.headers.get(k) for k in ("ETag", "Last-Modified") if response.headers.get(k)
        }
        return DownloadResult(tmp_path, received, content_type, extra, "")

    def race(
        self,
        sources: Sequence[Source],
        key: dict[str, str],
        headers: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> DownloadResult | None:
        """并发完整下载多个源，最先**完成且长度校验通过**的胜出。"""
        limit = limit or self._config.race_limit
        candidates = list(sources)[:limit]
        if not candidates:
            return None

        winner: dict[str, DownloadResult] = {}
        lock = threading.Lock()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates))
        futures = {
            pool.submit(self.download, source.url(**key), headers): source for source in candidates
        }
        try:
            for future in concurrent.futures.as_completed(futures):
                source = futures[future]
                try:
                    result = future.result()
                except Exception:  # noqa: BLE001
                    result = None
                if result is None:
                    log.info("源 %s: 失败或长度不符", source.name)
                    continue
                result.source = source.name
                with lock:
                    if winner:
                        # 已有更快的源，清掉这份多余的下载
                        _unlink(result.path)
                        continue
                    winner["r"] = result
                    log.info("源 %s: 完成 (%d bytes)", source.name, result.size)
                break  # 赢家已产生，不再等待其余慢源
        finally:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False)
        return winner.get("r")


def _unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
