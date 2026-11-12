"""服务器路由测试。

只测路由判定与响应构造，不触网。
"""

from __future__ import annotations

import pytest

from mise_gh_proxy.server import ProxyHandler


@pytest.fixture()
def handler() -> type[ProxyHandler]:
    return ProxyHandler


class TestMitmRouting:
    def test_github_release_path(self, handler) -> None:
        kind, payload = handler._mitm_route("github.com", "/o/r/releases/download/v1.0/app.tar.gz")
        assert kind == "release"
        assert payload == {"owner": "o", "repo": "r", "tag": "v1.0", "file": "app.tar.gz"}

    def test_github_other_path_is_page(self, handler) -> None:
        kind, payload = handler._mitm_route(
            "github.com", "/o/r.git/info/refs?service=git-upload-pack"
        )
        assert kind == "page"
        assert payload == "o/r.git/info/refs?service=git-upload-pack"

    def test_raw_host(self, handler) -> None:
        kind, payload = handler._mitm_route("raw.githubusercontent.com", "/o/r/main/file.md")
        assert kind == "raw"
        assert payload == "o/r/main/file.md"

    def test_api_host(self, handler) -> None:
        kind, payload = handler._mitm_route("api.github.com", "/repos/o/r/releases/assets/1")
        assert kind == "api"
        assert payload == "repos/o/r/releases/assets/1"

    def test_other_host_passthrough(self, handler) -> None:
        kind, payload = handler._mitm_route("pypi.org", "/simple/requests/")
        assert kind == "passthrough"
        assert payload == "/simple/requests/"


class TestReleaseKey:
    def test_key_format(self, handler) -> None:
        key = {"owner": "o", "repo": "r", "tag": "t", "file": "f"}
        assert handler._release_key(key) == "release:o/r"
