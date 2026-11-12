"""源池管理测试。"""

from __future__ import annotations

import json
from pathlib import Path

from gh_accel.config import DEFAULT_SOURCES, Source
from gh_accel.sources import load_registry


class TestLoadRegistry:
    def test_missing_file_yields_builtins(self, tmp_path: Path) -> None:
        registry = load_registry(str(tmp_path / "sources.json"))
        assert len(registry.sources) == len(DEFAULT_SOURCES)
        assert registry.disabled == set()

    def test_corrupt_file_falls_back_to_builtins(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        path.write_text("{ broken", encoding="utf-8")
        registry = load_registry(str(path))
        assert len(registry.sources) == len(DEFAULT_SOURCES)

    def test_non_dict_payload_falls_back(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert len(load_registry(str(path)).sources) == len(DEFAULT_SOURCES)

    def test_custom_source_is_loaded(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        path.write_text(
            json.dumps(
                {
                    "sources": {
                        "mine": {
                            "templates": {"release": "https://mine/{owner}/{repo}/{tag}/{file}"},
                            "priority": 5,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        registry = load_registry(str(path))
        source = registry.get("mine")
        assert source is not None
        assert source.templates["release"].startswith("https://mine/")
        assert source.priority == 5
        assert source.builtin is False

    def test_disabled_list_is_loaded(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        path.write_text(json.dumps({"disabled": ["ghproxy.cfd"]}), encoding="utf-8")
        registry = load_registry(str(path))
        assert registry.is_disabled("ghproxy.cfd")

    def test_malformed_source_entry_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        path.write_text(
            json.dumps(
                {
                    "sources": {
                        "bad": {"templates": "not-a-dict"},
                        "worse": {"templates": {}},
                    }
                }
            ),
            encoding="utf-8",
        )
        registry = load_registry(str(path))
        assert registry.get("bad") is None
        assert registry.get("worse") is None


class TestForChain:
    def test_filters_by_chain(self, tmp_path: Path) -> None:
        registry = load_registry(str(tmp_path / "sources.json"))
        for source in registry.for_chain("raw"):
            assert source.serves("raw")

    def test_disabled_are_excluded(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        path.write_text(json.dumps({"disabled": ["ustc"]}), encoding="utf-8")
        registry = load_registry(str(path))
        assert "ustc" not in [s.name for s in registry.for_chain("release")]

    def test_sorted_by_priority(self, tmp_path: Path) -> None:
        registry = load_registry(str(tmp_path / "sources.json"))
        priorities = [s.priority for s in registry.for_chain("release")]
        assert priorities == sorted(priorities)


class TestMutations:
    def test_set_and_save(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        registry = load_registry(str(path))
        registry.set_source(
            Source(
                name="mine",
                templates={"release": "https://mine/{owner}/{repo}/{tag}/{file}"},
                priority=5,
                builtin=False,
            )
        )
        registry.save()

        reloaded = load_registry(str(path))
        assert reloaded.get("mine") is not None
        assert reloaded.get("mine").priority == 5

    def test_remove_custom_source(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        registry = load_registry(str(path))
        registry.set_source(
            Source(name="mine", templates={"release": "https://mine/{path}"}, builtin=False)
        )
        registry.save()
        assert registry.remove_source("mine") is True
        registry.save()
        assert load_registry(str(path)).get("mine") is None

    def test_cannot_remove_builtin(self, tmp_path: Path) -> None:
        registry = load_registry(str(tmp_path / "sources.json"))
        assert registry.remove_source("ustc") is False

    def test_cannot_remove_unknown(self, tmp_path: Path) -> None:
        registry = load_registry(str(tmp_path / "sources.json"))
        assert registry.remove_source("nope") is False

    def test_disable_and_enable(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        registry = load_registry(str(path))
        assert registry.disable("ghproxy.cfd") is True
        registry.save()
        assert load_registry(str(path)).is_disabled("ghproxy.cfd")
        registry = load_registry(str(path))
        assert registry.enable("ghproxy.cfd") is True
        registry.save()
        assert not load_registry(str(path)).is_disabled("ghproxy.cfd")

    def test_reset_restores_builtins(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        registry = load_registry(str(path))
        registry.set_source(
            Source(name="mine", templates={"release": "https://mine/{path}"}, builtin=False)
        )
        registry.disable("ustc")
        registry.reset()
        registry.save()

        reloaded = load_registry(str(path))
        assert reloaded.get("mine") is None
        assert reloaded.disabled == set()
        assert len(reloaded.sources) == len(DEFAULT_SOURCES)

    def test_saved_file_is_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.json"
        registry = load_registry(str(path))
        registry.disable("ustc")
        registry.save()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "sources" in data
        assert "disabled" in data
