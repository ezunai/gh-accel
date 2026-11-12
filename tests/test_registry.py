"""registry mirror 测试。

覆盖：上游选择与故障转移、懒加载认证、registry 协议路由、与代理共用同一端口。
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
from dataclasses import replace
from pathlib import Path

from gh_accel.config import load_config
from gh_accel.fetcher import Fetcher
from gh_accel.registry import (
    DEFAULT_GROUPS,
    MirrorRegistry,
    RegistryConfig,
    RegistrySpec,
    load_registry_config,
)
from gh_accel.server import make_server


def _read_body(body):
    """fetch 返回的 body 可能是 bytes（已缓冲）或 HTTPResponse（流式）。"""
    if isinstance(body, bytes):
        return body
    try:
        return body.read()
    finally:
        with contextlib.suppress(Exception):
            body.close()


MANIFEST = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [{"digest": "sha256:abc", "platform": {"architecture": "amd64"}}],
}
BLOB = b"z" * 4096


def _handler(status: int = 200, body: bytes = b"", content_type: str = "application/json"):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/token"):
                payload = json.dumps({"token": "tok"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path.startswith("/v2/"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def _serve(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class TestRegistryConfig:
    def test_defaults_present(self, tmp_path: Path) -> None:
        config = load_registry_config(str(tmp_path / "registries.json"))
        assert "dockerhub" in config.specs
        assert config.specs["dockerhub"].upstreams

    def test_missing_file_yields_defaults(self, tmp_path: Path) -> None:
        config = load_registry_config(str(tmp_path / "nope.json"))
        assert set(config.specs) == set(DEFAULT_GROUPS)

    def test_corrupt_file_yields_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "registries.json"
        path.write_text("{ broken", encoding="utf-8")
        assert set(load_registry_config(str(path)).specs) == set(DEFAULT_GROUPS)

    def test_custom_upstream_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "registries.json"
        path.write_text(
            json.dumps({"groups": {"dockerhub": {"upstreams": ["https://only.example"]}}}),
            encoding="utf-8",
        )
        config = load_registry_config(str(path))
        assert config.specs["dockerhub"].upstreams == ("https://only.example",)

    def test_empty_upstreams_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "registries.json"
        path.write_text(json.dumps({"groups": {"dockerhub": {"upstreams": []}}}), encoding="utf-8")
        config = load_registry_config(str(path))
        assert config.specs["dockerhub"].upstreams == DEFAULT_GROUPS["dockerhub"]["upstreams"]


class TestUpstreamSelection:
    def test_first_working_upstream_wins(self, tmp_path: Path) -> None:
        server, url = _serve(_handler(200, json.dumps(MANIFEST).encode()))
        try:
            config = RegistryConfig(
                specs={
                    "dockerhub": RegistrySpec("dockerhub", (url,)),
                    "ghcr.io": RegistrySpec("ghcr.io", ("https://127.0.0.1:1/x",)),
                },
                path=str(tmp_path / "r.json"),
            )
            mirror = MirrorRegistry(config, Fetcher(load_config()), str(tmp_path))
            status, headers, body = mirror.fetch("/v2/library/alpine/manifests/latest", {})
            assert status == 200
            assert json.loads(_read_body(body))["mediaType"] == MANIFEST["mediaType"]
        finally:
            server.shutdown()
            server.server_close()

    def test_unreachable_upstream_falls_through(self, tmp_path: Path) -> None:
        server, url = _serve(_handler(200, json.dumps(MANIFEST).encode()))
        try:
            config = RegistryConfig(
                specs={
                    "dockerhub": RegistrySpec(
                        "dockerhub",
                        ("https://127.0.0.1:1/dead", url),
                        token_service="https://127.0.0.1:1/token",
                        service="registry.docker.io",
                    ),
                },
                path=str(tmp_path / "r.json"),
            )
            mirror = MirrorRegistry(config, Fetcher(load_config()), str(tmp_path))
            status, _, body = mirror.fetch("/v2/library/alpine/manifests/latest", {})
            assert status == 200
            assert json.loads(_read_body(body))["mediaType"]
        finally:
            server.shutdown()
            server.server_close()

    def test_referrers_returns_empty_index_without_upstream(self, tmp_path: Path) -> None:
        """referrers 查询直接返回空 index，不去上游。

        Docker 24+ 拉镜像时会先查 OCI referrers；mirror 若回 404，daemon 会
        回退到原始 registry（registry-1.docker.io，本机被 DNS 污染）再查，
        白等 60 秒。返回合法的空 index 可阻止这次回退。
        """
        config = RegistryConfig(
            specs={
                "dockerhub": RegistrySpec("dockerhub", ("https://127.0.0.1:1/dead",)),
            },
            path=str(tmp_path / "r.json"),
        )
        mirror = MirrorRegistry(config, Fetcher(load_config()), str(tmp_path))
        status, headers, body = mirror.fetch("/v2/library/alpine/referrers/sha256:abc", {})
        assert status == 200
        assert headers["Content-Type"] == "application/vnd.oci.image.index.v1+json"
        payload = json.loads(body)
        assert payload["manifests"] == []
        assert payload["schemaVersion"] == 2

    def test_all_upstreams_dead_returns_502(self, tmp_path: Path) -> None:
        config = RegistryConfig(
            specs={
                "dockerhub": RegistrySpec("dockerhub", ("https://127.0.0.1:1/a",)),
            },
            path=str(tmp_path / "r.json"),
        )
        mirror = MirrorRegistry(config, Fetcher(load_config()), str(tmp_path))
        status, _, _ = mirror.fetch("/v2/library/alpine/manifests/latest", {})
        assert status == 502

    def test_lazy_auth_token_not_requested_when_not_needed(self, tmp_path: Path) -> None:
        """上游直接 200 时不应去换 token（auth.docker.io 在本机不可达）。"""
        server, url = _serve(_handler(200, json.dumps(MANIFEST).encode()))
        try:
            config = RegistryConfig(
                specs={
                    "dockerhub": RegistrySpec(
                        "dockerhub",
                        (url,),
                        token_service="https://127.0.0.1:1/token",
                        service="registry.docker.io",
                    ),
                },
                path=str(tmp_path / "r.json"),
            )
            mirror = MirrorRegistry(config, Fetcher(load_config()), str(tmp_path))
            status, _, _ = mirror.fetch("/v2/library/alpine/manifests/latest", {})
            assert status == 200
            assert mirror._tokens == {}  # 没换过 token
        finally:
            server.shutdown()
            server.server_close()

    def test_401_triggers_token_exchange(self, tmp_path: Path) -> None:
        """上游要认证时换 token 并重试。"""
        server, url = _serve(_handler(401, b"{}"))
        try:
            config = RegistryConfig(
                specs={
                    "dockerhub": RegistrySpec(
                        "dockerhub",
                        (url,),
                        token_service=url + "/token",
                        service="registry.docker.io",
                    ),
                },
                path=str(tmp_path / "r.json"),
            )
            mirror = MirrorRegistry(config, Fetcher(load_config()), str(tmp_path))
            status, _, _ = mirror.fetch("/v2/library/alpine/manifests/latest", {})
            # 换到 token 后重试仍是 401（上游固定返回 401），但 token 已被换过
            assert mirror._tokens, "应已尝试换取 token"
            assert status == 401
        finally:
            server.shutdown()
            server.server_close()


class TestSharedPort:
    """registry 协议与代理协议共用同一个端口。"""

    def test_registry_route_on_main_server(self, tmp_path: Path) -> None:
        server, url = _serve(_handler(200, json.dumps(MANIFEST).encode()))
        try:
            config = replace(
                load_config(),
                host="127.0.0.1",
                port=0,
                cache_dir=str(tmp_path / "cache"),
                registries_file=str(tmp_path / "registries.json"),
            )
            path = tmp_path / "registries.json"
            path.write_text(
                json.dumps({"groups": {"dockerhub": {"upstreams": [url]}}}), encoding="utf-8"
            )

            import socket

            proxy = make_server(config)
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            try:
                sock = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=20)
                sock.sendall(
                    b"GET /v2/library/alpine/manifests/latest HTTP/1.1\r\n"
                    b"Host: example.com\r\nConnection: close\r\n\r\n"
                )
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                sock.close()
                assert b"200 OK" in data[:20]
                assert b"application/vnd.oci.image.index.v1+json" in data
            finally:
                proxy.shutdown()
                proxy.server_close()
        finally:
            server.shutdown()
            server.server_close()

    def test_v2_root_is_capability_probe(self, tmp_path: Path) -> None:
        config = replace(
            load_config(),
            host="127.0.0.1",
            port=0,
            cache_dir=str(tmp_path / "cache"),
            registries_file=str(tmp_path / "registries.json"),
        )
        proxy = make_server(config)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        try:
            import socket

            sock = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=10)
            sock.sendall(b"GET /v2/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            sock.close()
            assert b"200 OK" in data[:20]
        finally:
            proxy.shutdown()
            proxy.server_close()
