"""透传（passthrough）的流式行为测试。

Docker 拉镜像层时响应体上百 MB，若代理把响应全量缓冲进内存，客户端会
等到收完才看到第一个字节，且内存占用随响应体线性增长。这里用本地
HTTP server 验证流式转发确实生效。
"""

from __future__ import annotations

import http.server
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from gh_accel.config import Config, load_config
from gh_accel.server import make_server

#: 模拟一个较大的镜像层
LAYER_SIZE = 8 * 1024 * 1024


class _Handler(http.server.BaseHTTPRequestHandler):
    """按路径区分两种响应。

    * ``/large`` —— 带 Content-Length 的大响应（应走流式转发）
    * ``/chunked`` —— chunked 编码、无 Content-Length（应缓冲后按长度回）
    """

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/missing"):
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/chunked"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for _ in range(4):
                chunk = b"y" * 1024
                self.wfile.write(f"{len(chunk):x}\r\n".encode())
                self.wfile.write(chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(LAYER_SIZE))
        self.end_headers()
        remaining = LAYER_SIZE
        while remaining > 0:
            chunk = b"z" * min(256 * 1024, remaining)
            self.wfile.write(chunk)
            remaining -= len(chunk)


@pytest.fixture()
def upstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def proxy(config: Config, upstream: str):
    """启动代理，指向本地 upstream。"""
    server = make_server(replace(config, host="127.0.0.1", port=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _raw_request(port: int, path: str) -> tuple[float, bytes]:
    """发一个裸 HTTP 请求，返回 (耗时, 完整响应)。

    带 ``Connection: close`` 让服务端在响应后关闭连接，否则 keep-alive
    下客户端会一直等 EOF。
    """
    import socket

    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n".encode())
    started = time.time()
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    sock.close()
    return time.time() - started, b"".join(chunks)


class TestPassthroughStreaming:
    def test_large_body_is_streamed_with_content_length(self, proxy, upstream: str) -> None:
        port = proxy.server_address[1]
        _, response = _raw_request(port, f"http://{upstream}/large")
        assert b"200 OK" in response[:20]
        assert b"Content-Length: 8388608" in response
        # 完整响应体确实传过去了
        assert len(response) >= LAYER_SIZE

    def test_chunked_response_gets_explicit_length(self, proxy, upstream: str) -> None:
        """chunked 上游没有长度，必须缓冲后补 Content-Length。

        否则客户端会一直等响应结束。
        """
        port = proxy.server_address[1]
        _, response = _raw_request(port, f"http://{upstream}/chunked")
        assert b"200 OK" in response[:20]
        assert b"Content-Length:" in response
        assert b"Transfer-Encoding" not in response

    def test_upstream_error_status_is_passed_through(self, proxy, upstream: str) -> None:
        """上游的 4xx 应原样透传，而不是变成 502。"""
        port = proxy.server_address[1]
        _, response = _raw_request(port, f"http://{upstream}/missing")
        assert b"404" in response[:40]
        assert b"502" not in response[:40]


class TestRegistryRouting:
    def test_registry_is_injected(self, config: Config) -> None:
        server = make_server(config)
        try:
            handler = server.RequestHandlerClass
            assert hasattr(handler, "registry")
            assert handler.registry is not None
        finally:
            server.server_close()


class TestStreamingIsNotBuffered:
    """验证大响应体是流式转发，而不是全量缓冲。

    全量缓冲的表现是：客户端要等上游把整个响应体发完，才收到第一个字节。
    这里让上游先发一半内容再停顿，流式转发下客户端应当立刻收到首字节。
    """

    def test_client_receives_first_bytes_before_upstream_finishes(self) -> None:
        import socket
        import threading

        gate = threading.Event()
        released = threading.Event()

        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                half = b"a" * 1024
                self.send_response(200)
                self.send_header("Content-Length", str(len(half) * 2))
                self.end_headers()
                self.wfile.write(half)
                self.wfile.flush()
                released.set()
                gate.wait(timeout=10)
                self.wfile.write(half)

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        up_port = upstream.server_address[1]

        cfg = replace(load_config(), host="127.0.0.1", port=0)
        proxy = make_server(cfg)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        try:
            sock = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=15)
            sock.sendall(
                f"GET http://127.0.0.1:{up_port}/slow HTTP/1.1\r\n"
                "Host: example.com\r\nConnection: close\r\n\r\n".encode()
            )
            sock.settimeout(10)
            first = sock.recv(65536)
            # 上游还在阻塞，客户端已经拿到了响应头
            assert first, "流式转发下应立刻收到首字节"
            assert b"200 OK" in first[:20]
            assert b"Content-Length: 2048" in first

            # 继续读，在上游仍未放行时应已收到 body 的一部分
            buf = first
            deadline = time.time() + 8
            got_body = False
            while time.time() < deadline:
                sock.settimeout(0.5)
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                buf += chunk
                if b"\r\n\r\n" in buf and buf.split(b"\r\n\r\n", 1)[1]:
                    got_body = True
                    break
            assert got_body, "上游还在阻塞时，客户端就应收到 body 的第一部分"
            sock.close()
        finally:
            gate.set()
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()
