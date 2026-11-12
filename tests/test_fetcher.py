"""下载层测试。

用本地 HTTP 服务器模拟上游，验证长度校验与竞速逻辑。
"""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

from gh_accel.config import Source
from gh_accel.fetcher import Fetcher, clean_headers, expected_size


class _Handler(http.server.BaseHTTPRequestHandler):
    """按查询参数模拟不同上游行为。

    * ``?mode=full``  —— 正常返回完整内容
    * ``?mode=truncated`` —— 声明总长但只发一部分（模拟截断的加速服务）
    * ``?mode=slow``  —— 慢速返回
    """

    payload = b"x" * 4096

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        mode = "full"
        if "mode=" in self.path:
            mode = self.path.split("mode=")[1].split("&")[0]
        if mode == "truncated":
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-99/{len(self.payload)}")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(self.payload[:100])
            return
        if mode == "slow":
            import time

            time.sleep(1.5)
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(self.payload)


@pytest.fixture()
def upstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


class TestCleanHeaders:
    def test_strips_host_and_hop_by_hop(self) -> None:
        out = clean_headers(
            {
                "Host": "github.com",
                "Connection": "keep-alive",
                "Accept-Encoding": "gzip",
                "User-Agent": "x",
            }
        )
        assert "Host" not in out
        assert "Connection" not in out
        assert out["Accept-Encoding"] == "identity"
        assert out["User-Agent"] == "x"

    def test_handles_none(self) -> None:
        assert clean_headers(None)["Accept-Encoding"] == "identity"


class TestExpectedSize:
    class _Resp:
        def __init__(self, headers):
            self.headers = headers

    def test_from_content_range(self) -> None:
        resp = self._Resp({"Content-Range": "bytes 0-99/1000"})
        assert expected_size(resp) == 1000

    def test_from_content_length(self) -> None:
        resp = self._Resp({"Content-Length": "42"})
        assert expected_size(resp) == 42

    def test_unknown(self) -> None:
        assert expected_size(self._Resp({})) is None


class TestDownload:
    def test_downloads_full_content(self, config, upstream) -> None:
        fetcher = Fetcher(config)
        result = fetcher.download(f"{upstream}/file?mode=full")
        assert result is not None
        assert result.size == len(_Handler.payload)
        assert Path(result.path).read_bytes() == _Handler.payload

    def test_rejects_truncated_response(self, config, upstream) -> None:
        """截断的响应必须被判为不可用。"""
        fetcher = Fetcher(config)
        assert fetcher.download(f"{upstream}/file?mode=truncated") is None

    def test_download_ignores_client_range_header(self, config, upstream) -> None:
        """缓存里必须是完整文件，不能带客户端的 Range。"""
        fetcher = Fetcher(config)
        result = fetcher.download(f"{upstream}/file?mode=full", {"Range": "bytes=0-9"})
        assert result is not None
        assert result.size == len(_Handler.payload)

    def test_unreachable_source(self, config) -> None:
        fetcher = Fetcher(config)
        assert fetcher.download("http://127.0.0.1:1/nothing") is None


class TestRace:
    def test_fastest_source_wins(self, config, upstream) -> None:
        slow = Source("slow", {"release": f"{upstream}/file?mode=slow"})
        fast = Source("fast", {"release": f"{upstream}/file?mode=full"})
        fetcher = Fetcher(config)
        result = fetcher.race([slow, fast], "release", {}, limit=2)
        assert result is not None
        assert result.source == "fast"

    def test_all_sources_failing(self, config) -> None:
        fetcher = Fetcher(config)
        bad = Source("bad", {"release": "http://127.0.0.1:1/x"})
        assert fetcher.race([bad], "release", {}, limit=1) is None

    def test_empty_pool(self, config) -> None:
        assert Fetcher(config).race([], "release", {}) is None


class TestProbe:
    def test_probe_returns_speed_for_working_source(self, config, upstream) -> None:
        fetcher = Fetcher(config)
        speed = fetcher.probe_speed(f"{upstream}/file?mode=full")
        assert speed is not None and speed > 0

    def test_probe_returns_none_for_dead_source(self, config) -> None:
        assert Fetcher(config).probe_speed("http://127.0.0.1:1/x") is None
