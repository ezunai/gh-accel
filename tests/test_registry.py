"""容器 registry（ghcr.io / quay.io）经代理的测试。

要点：

* registry 的 blob 可能上百 MB，透传必须是流式的；
* 拉取流程是「先换 token，再带 Authorization 拉 blob」，所以
  ``Authorization`` 头必须原样转发 —— 一旦被丢弃，blob 请求会 401。
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from dataclasses import replace

import pytest

from gh_accel.config import load_config
from gh_accel.server import ProxyHandler, make_server

TOKEN = "Bearer test-token-123"


class _RegistryHandler(http.server.BaseHTTPRequestHandler):
    """模拟 registry 的两个端点。

    * ``/token`` —— 返回一个 token JSON
    * ``/v2/.../blobs/...`` —— 要求带正确的 Authorization，否则 401
    """

    blob_size = 4 * 1024 * 1024

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/token"):
            body = json.dumps({"token": "test-token-123"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        auth = self.headers.get("Authorization")
        if auth != TOKEN:
            body = b'{"errors":[{"code":"UNAUTHORIZED"}]}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = b"b" * self.blob_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def registry():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RegistryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def proxy():
    server = make_server(replace(load_config(), host="127.0.0.1", port=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _raw(port: int, path: str, headers: dict[str, str] | None = None) -> bytes:
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    lines = [f"GET {path} HTTP/1.1", "Host: ghcr.io", "Connection: close"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    sock.close()
    return b"".join(chunks)


class TestRegistryRouting:
    def test_ghcr_is_passthrough(self) -> None:
        kind, _ = ProxyHandler._mitm_route("ghcr.io", "/v2/foo/blobs/sha256:x")
        assert kind == "passthrough"

    def test_quay_is_passthrough(self) -> None:
        kind, _ = ProxyHandler._mitm_route("quay.io", "/v2/foo/blobs/sha256:x")
        assert kind == "passthrough"

    def test_unknown_host_is_passthrough(self) -> None:
        kind, _ = ProxyHandler._mitm_route("example.com", "/x")
        assert kind == "passthrough"

    def test_github_release_still_routed_to_release(self) -> None:
        kind, payload = ProxyHandler._mitm_route(
            "github.com", "/o/r/releases/download/v1.0/app.tar.gz"
        )
        assert kind == "release"
        assert payload["owner"] == "o"


class TestAuthorizationForwarding:
    """Authorization 必须原样转发，否则 registry 的 blob 请求会 401。"""

    def test_authorization_header_is_forwarded(self, proxy, registry: str) -> None:
        port = proxy.server_address[1]
        response = _raw(
            port,
            f"http://{registry}/v2/foo/blobs/sha256:abc",
            {"Authorization": TOKEN},
        )
        assert b"200 OK" in response[:20], response[:200]
        assert b"401" not in response[:20]

    def test_missing_authorization_yields_401(self, proxy, registry: str) -> None:
        """上游的 401 应原样透传，而不是变成 502。"""
        port = proxy.server_address[1]
        response = _raw(port, f"http://{registry}/v2/foo/blobs/sha256:abc")
        assert b"401" in response[:40], response[:200]


class TestBlobStreaming:
    def test_blob_body_is_streamed(self, proxy, registry: str) -> None:
        """大 blob 应流式转发：客户端不必等全部收完。"""
        port = proxy.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        sock.sendall(
            f"GET http://{registry}/v2/foo/blobs/sha256:abc HTTP/1.1\r\n"
            f"Host: ghcr.io\r\nAuthorization: {TOKEN}\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        sock.settimeout(15)
        buf = b""
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
            if b"\r\n\r\n" in buf and len(buf.split(b"\r\n\r\n", 1)[1]) > 0:
                break
        sock.close()
        assert b"200 OK" in buf[:20]
        assert b"Content-Length: 4194304" in buf
        body = buf.split(b"\r\n\r\n", 1)[1]
        assert len(body) > 0, "应已收到 body 的一部分"
