"""配置模块测试。"""

from __future__ import annotations

import os

from mise_gh_proxy.config import (
    API_PATH_RE,
    API_SOURCES,
    GH_PATH_RE,
    GIT_PATH_RE,
    PAGE_SOURCES,
    RAW_PATH_RE,
    RAW_SOURCES,
    RELEASE_PATH_RE,
    RELEASE_SOURCES,
    Source,
    load_config,
)


class TestSourceTemplates:
    def test_release_template_expands(self) -> None:
        source = Source("x", "https://e/{owner}/{repo}/{tag}/{file}")
        assert source.url(owner="o", repo="r", tag="t", file="f") == "https://e/o/r/t/f"

    def test_path_template_expands(self) -> None:
        source = Source("x", "https://e/{path}")
        assert source.url(path="a/b") == "https://e/a/b"


class TestSourcePools:
    def test_release_pool_ends_with_direct(self) -> None:
        assert RELEASE_SOURCES[-1].name == "direct"

    def test_release_pool_starts_with_domestic_mirror(self) -> None:
        assert RELEASE_SOURCES[0].name == "ustc"

    def test_pools_are_non_empty(self) -> None:
        for pool in (RELEASE_SOURCES, PAGE_SOURCES, RAW_SOURCES, API_SOURCES):
            assert pool, "源池不应为空"

    def test_source_names_unique_per_pool(self) -> None:
        for pool in (RELEASE_SOURCES, PAGE_SOURCES, RAW_SOURCES, API_SOURCES):
            names = [s.name for s in pool]
            assert len(names) == len(set(names)), f"源名重复: {names}"


class TestPathRegex:
    def test_release_path(self) -> None:
        match = RELEASE_PATH_RE.match("/gh/o/r/releases/download/v1.0/app.tar.gz")
        assert match
        assert match.groupdict() == {"owner": "o", "repo": "r", "tag": "v1.0", "file": "app.tar.gz"}

    def test_release_path_rejects_traversal(self) -> None:
        assert RELEASE_PATH_RE.match("/gh/o/r/releases/download/v1.0/") is None

    def test_raw_path(self) -> None:
        assert RAW_PATH_RE.match("/raw/a/b/c.md").group("path") == "a/b/c.md"

    def test_api_path(self) -> None:
        assert API_PATH_RE.match("/api/repos/o/r/releases").group("path") == "repos/o/r/releases"

    def test_gh_path(self) -> None:
        assert GH_PATH_RE.match("/gh/o/r/archive/x.zip").group("path") == "o/r/archive/x.zip"

    def test_git_path(self) -> None:
        assert GIT_PATH_RE.match("/git/o/r.git/info/refs").group("path") == "o/r.git/info/refs"


class TestLoadConfig:
    def test_defaults(self, monkeypatch) -> None:
        for key in list(os.environ):
            if key.startswith("MISE_GH_PROXY_"):
                monkeypatch.delenv(key, raising=False)
        cfg = load_config()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 19770
        assert cfg.cache_dir.endswith("mise-gh-proxy")

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("MISE_GH_PROXY_PORT", "12345")
        monkeypatch.setenv("MISE_GH_PROXY_TIMEOUT", "7.5")
        cfg = load_config()
        assert cfg.port == 12345
        assert cfg.timeout == 7.5

    def test_invalid_env_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("MISE_GH_PROXY_PORT", "not-a-number")
        assert load_config().port == 19770

    def test_derived_paths(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("MISE_GH_PROXY_CACHE", str(tmp_path))
        cfg = load_config()
        assert cfg.file_cache_dir == str(tmp_path / "files")
        assert cfg.cert_dir == str(tmp_path / "certs")
        assert cfg.ca_pem.endswith("ca.pem")
        assert cfg.base_url.startswith("http://127.0.0.1:")
