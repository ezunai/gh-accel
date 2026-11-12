"""配置模块测试。"""

from __future__ import annotations

import os

from gh_accel.config import (
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
            if key.startswith("GH_ACCEL_"):
                monkeypatch.delenv(key, raising=False)
        cfg = load_config()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 19770
        assert cfg.cache_dir.endswith("gh-accel")

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("GH_ACCEL_PORT", "12345")
        monkeypatch.setenv("GH_ACCEL_TIMEOUT", "7.5")
        cfg = load_config()
        assert cfg.port == 12345
        assert cfg.timeout == 7.5

    def test_invalid_env_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("GH_ACCEL_PORT", "not-a-number")
        assert load_config().port == 19770

    def test_derived_paths(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GH_ACCEL_CACHE", str(tmp_path / "cache"))
        monkeypatch.setenv("GH_ACCEL_CERTS", str(tmp_path / "certs"))
        cfg = load_config()
        assert cfg.file_cache_dir == str(tmp_path / "cache" / "files")
        assert cfg.cert_dir == str(tmp_path / "certs")
        assert cfg.ca_pem.endswith("ca.pem")
        assert cfg.base_url.startswith("http://127.0.0.1:")

    def test_certs_are_outside_cache_dir(self, monkeypatch) -> None:
        """CA 是持久凭据，不能放在可被清理的 cache_dir 里。"""
        monkeypatch.setenv("GH_ACCEL_CACHE", "/tmp/x/cache")
        monkeypatch.setenv("GH_ACCEL_CERTS", "/tmp/x/certs")
        cfg = load_config()
        assert not cfg.cert_dir.startswith(cfg.cache_dir)
        assert not cfg.ca_pem.startswith(cfg.cache_dir)

    def test_certs_default_is_config_dir(self, monkeypatch) -> None:
        for key in list(os.environ):
            if key.startswith("GH_ACCEL_"):
                monkeypatch.delenv(key, raising=False)
        assert load_config().certs_dir.endswith("/.config/gh-accel/certs")
