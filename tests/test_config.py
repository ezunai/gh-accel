"""配置模块测试。"""

from __future__ import annotations

import os

from gh_accel.config import (
    API_PATH_RE,
    CHAINS,
    DEFAULT_SOURCES,
    GH_PATH_RE,
    GIT_PATH_RE,
    RAW_PATH_RE,
    RELEASE_PATH_RE,
    Source,
    load_config,
)


class TestSource:
    def test_release_template_expands(self) -> None:
        source = Source("x", {"release": "https://e/{owner}/{repo}/{tag}/{file}"})
        assert source.url("release", owner="o", repo="r", tag="t", file="f") == "https://e/o/r/t/f"

    def test_path_template_expands(self) -> None:
        source = Source("x", {"page": "https://e/{path}"})
        assert source.url("page", path="a/b") == "https://e/a/b"

    def test_chains_property(self) -> None:
        source = Source("x", {"release": "a", "page": "b"})
        assert set(source.chains) == {"release", "page"}

    def test_serves(self) -> None:
        source = Source("x", {"release": "a"})
        assert source.serves("release")
        assert not source.serves("raw")


class TestDefaultSources:
    def test_registry_non_empty(self) -> None:
        assert DEFAULT_SOURCES

    def test_source_names_unique(self) -> None:
        names = [s.name for s in DEFAULT_SOURCES]
        assert len(names) == len(set(names)), f"源名重复: {names}"

    def test_every_source_serves_a_known_chain(self) -> None:
        for source in DEFAULT_SOURCES:
            for chain in source.chains:
                assert chain in CHAINS, f"{source.name} 声明了未知链路 {chain}"

    def test_direct_is_last(self) -> None:
        """直连必须是最后兜底。"""
        direct = next(s for s in DEFAULT_SOURCES if s.name == "direct")
        others = [s for s in DEFAULT_SOURCES if s.name != "direct"]
        assert direct.priority > max(s.priority for s in others)

    def test_domestic_mirror_first(self) -> None:
        release = [s for s in DEFAULT_SOURCES if s.serves("release")]
        assert release[0].name == "ustc"

    def test_all_chains_have_at_least_one_source(self) -> None:
        for chain in CHAINS:
            assert any(s.serves(chain) for s in DEFAULT_SOURCES), f"{chain} 无可用源"


class TestPathRegex:
    def test_release_path(self) -> None:
        match = RELEASE_PATH_RE.match("/gh/o/r/releases/download/v1.0/app.tar.gz")
        assert match
        assert match.groupdict() == {"owner": "o", "repo": "r", "tag": "v1.0", "file": "app.tar.gz"}

    def test_release_path_rejects_trailing_slash(self) -> None:
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
        monkeypatch.setenv("GH_ACCEL_CONFIG", str(tmp_path / "config"))
        cfg = load_config()
        assert cfg.file_cache_dir == str(tmp_path / "cache" / "files")
        assert cfg.cert_dir == str(tmp_path / "config" / "certs")
        assert cfg.ca_pem.endswith("ca.pem")
        assert cfg.sources_file.endswith("sources.json")
        assert cfg.base_url.startswith("http://127.0.0.1:")

    def test_certs_are_outside_cache_dir(self, monkeypatch) -> None:
        """CA 是持久凭据，不能放在可被清理的 cache_dir 里。"""
        monkeypatch.setenv("GH_ACCEL_CACHE", "/tmp/x/cache")
        monkeypatch.setenv("GH_ACCEL_CONFIG", "/tmp/x/config")
        cfg = load_config()
        assert not cfg.cert_dir.startswith(cfg.cache_dir)
        assert not cfg.ca_pem.startswith(cfg.cache_dir)

    def test_certs_default_is_config_dir(self, monkeypatch) -> None:
        for key in list(os.environ):
            if key.startswith("GH_ACCEL_"):
                monkeypatch.delenv(key, raising=False)
        assert load_config().certs_dir.endswith("/.config/gh-accel/certs")
