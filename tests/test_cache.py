"""缓存模块测试。"""

from __future__ import annotations

from pathlib import Path

from mise_gh_proxy.cache import FileCache, SourcePreference


class TestSourcePreference:
    def test_remembers_most_recent_first(self, tmp_path: Path) -> None:
        pref = SourcePreference(str(tmp_path / "sources.json"))
        pref.remember("release:o/r", "a")
        pref.remember("release:o/r", "b")
        assert pref.preferred("release:o/r") == ["b", "a"]

    def test_moves_existing_to_front(self, tmp_path: Path) -> None:
        pref = SourcePreference(str(tmp_path / "sources.json"))
        pref.remember("k", "a")
        pref.remember("k", "b")
        pref.remember("k", "a")
        assert pref.preferred("k") == ["a", "b"]

    def test_forget(self, tmp_path: Path) -> None:
        pref = SourcePreference(str(tmp_path / "sources.json"))
        pref.remember("k", "a")
        pref.forget("k", "a")
        assert pref.preferred("k") == []

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        path = str(tmp_path / "sources.json")
        SourcePreference(path).remember("k", "x")
        assert SourcePreference(path).preferred("k") == ["x"]

    def test_corrupt_file_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        path.write_text("{ not json", encoding="utf-8")
        assert SourcePreference(str(path)).preferred("k") == []

    def test_missing_file_is_ignored(self, tmp_path: Path) -> None:
        pref = SourcePreference(str(tmp_path / "nope.json"))
        assert pref.preferred("k") == []
        pref.remember("k", "a")  # 不应抛异常
        assert pref.preferred("k") == ["a"]


class TestFileCache:
    def test_path_is_stable_and_url_specific(self, tmp_path: Path) -> None:
        cache = FileCache(str(tmp_path), 1024)
        a = cache.path_for("https://example.com/x")
        assert a == cache.path_for("https://example.com/x")
        assert a != cache.path_for("https://example.com/y")

    def test_store_and_get(self, tmp_path: Path) -> None:
        cache = FileCache(str(tmp_path / "files"), 1024 * 1024)
        src = tmp_path / "tmp.bin"
        src.write_bytes(b"hello")
        url = "https://example.com/x"
        stored = cache.store(url, str(src))
        assert Path(stored).read_bytes() == b"hello"
        assert cache.get(url) == stored

    def test_get_miss(self, tmp_path: Path) -> None:
        cache = FileCache(str(tmp_path / "files"), 1024)
        assert cache.get("https://example.com/nope") is None

    def test_stats(self, tmp_path: Path) -> None:
        cache = FileCache(str(tmp_path / "files"), 1024 * 1024)
        src = tmp_path / "t.bin"
        src.write_bytes(b"x" * 100)
        cache.store("https://example.com/x", str(src))
        count, total = cache.stats()
        assert count == 1
        assert total == 100

    def test_prune_removes_oldest(self, tmp_path: Path) -> None:
        cache = FileCache(str(tmp_path / "files"), 150)
        for i in range(3):
            src = tmp_path / f"t{i}.bin"
            src.write_bytes(b"x" * 100)
            cache.store(f"https://example.com/{i}", str(src))
        cache.prune()
        count, total = cache.stats()
        assert total <= 150
        assert count < 3
