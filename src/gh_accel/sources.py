"""源池管理：内置源 + 用户自定义源的合并与持久化。

配置文件为 JSON（``~/.config/gh-accel/sources.json``），因为项目支持
Python 3.10，而 ``tomllib`` 是 3.11 才有的只读模块。

格式::

    {
      "sources": {
        "my-mirror": {
          "templates": {
            "release": "https://my.mirror/{owner}/{repo}/{tag}/{file}"
          },
          "priority": 15
        }
      },
      "disabled": ["ghproxy.cfd"]
    }

``sources`` 是用户新增的源（与内置源同名则覆盖其模板）；``disabled``
列出要停用的源名（内置或自定义均可）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from .config import DEFAULT_SOURCES, Source

__all__ = ["SourceRegistry", "load_registry"]

log = logging.getLogger(__name__)


@dataclass
class SourceRegistry:
    """合并后的源池。"""

    sources: dict[str, Source]
    disabled: set[str]
    path: str
    dirty: bool = False
    _mtime: float = 0.0

    # ---- 热重载 ----------------------------------------------------------
    def reload_if_changed(self) -> bool:
        """配置文件有变化时重新加载。返回是否发生了重载。

        代理是长期运行的服务，用户改了源配置应当立即生效，而不是
        还得重启 systemd 服务。
        """
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = 0.0
        if mtime == self._mtime:
            return False
        fresh = load_registry(self.path)
        self.sources = fresh.sources
        self.disabled = fresh.disabled
        self._mtime = mtime
        log.info("源配置已重新加载: %s", self.path)
        return True

    # ---- 查询 ------------------------------------------------------------
    def for_chain(self, chain: str) -> list[Source]:
        """返回某条链路适用的源，按 priority 排序。"""
        usable = [
            s for s in self.sources.values() if s.serves(chain) and s.name not in self.disabled
        ]
        return sorted(usable, key=lambda s: s.priority)

    def names(self) -> list[str]:
        return sorted(self.sources)

    def get(self, name: str) -> Source | None:
        return self.sources.get(name)

    def is_disabled(self, name: str) -> bool:
        return name in self.disabled

    # ---- 修改 ------------------------------------------------------------
    def set_source(self, source: Source) -> None:
        """新增或覆盖一个源。"""
        self.sources[source.name] = source
        self.dirty = True

    def remove_source(self, name: str) -> bool:
        """删除一个用户自定义源。内置源不能删，只能禁用。"""
        source = self.sources.get(name)
        if source is None:
            return False
        if source.builtin:
            return False
        del self.sources[name]
        self.dirty = True
        return True

    def disable(self, name: str) -> bool:
        if name not in self.sources:
            return False
        self.disabled.add(name)
        self.dirty = True
        return True

    def enable(self, name: str) -> bool:
        if name not in self.sources:
            return False
        self.disabled.discard(name)
        self.dirty = True
        return True

    def reset(self) -> None:
        """恢复内置源并清空用户改动。"""
        self.sources = {s.name: s for s in DEFAULT_SOURCES}
        self.disabled = set()
        self.dirty = True

    # ---- 持久化 ----------------------------------------------------------
    def save(self) -> None:
        """把用户改动写回配置文件。"""
        builtin = {s.name for s in DEFAULT_SOURCES}
        custom = {
            name: {
                "templates": source.templates,
                "priority": source.priority,
            }
            for name, source in self.sources.items()
            if name not in builtin or source.templates != _builtin_templates(source)
        }
        # 被覆盖过模板的内置源也要保留
        overrides = {}
        for name, source in self.sources.items():
            if name in builtin and source.templates != _builtin_templates(source):
                overrides[name] = {
                    "templates": source.templates,
                    "priority": source.priority,
                }
        custom.update(overrides)

        data = {
            "sources": custom,
            "disabled": sorted(self.disabled),
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)
        self.dirty = False


def _builtin_templates(source: Source) -> dict[str, str]:
    for builtin in DEFAULT_SOURCES:
        if builtin.name == source.name:
            return builtin.templates
    return {}


def _make(sources: dict[str, Source], disabled: set[str], path: str) -> SourceRegistry:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return SourceRegistry(sources=sources, disabled=disabled, path=path, _mtime=mtime)


def load_registry(path: str) -> SourceRegistry:
    """读取配置文件并与内置源合并。"""
    sources = {s.name: s for s in DEFAULT_SOURCES}
    disabled: set[str] = set()

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _make(sources, disabled, path)
    except (OSError, ValueError) as exc:
        log.warning("源配置文件解析失败 (%s): %s，使用内置源", path, exc)
        return _make(sources, disabled, path)

    if not isinstance(data, dict):
        return _make(sources, disabled, path)

    for name, spec in (data.get("sources") or {}).items():
        if not isinstance(spec, dict):
            continue
        templates = spec.get("templates")
        if not isinstance(templates, dict) or not templates:
            continue
        templates = {k: v for k, v in templates.items() if isinstance(v, str)}
        if not templates:
            continue
        priority = spec.get("priority", 100)
        if not isinstance(priority, int):
            priority = 100
        sources[name] = Source(
            name=name,
            templates=templates,
            priority=priority,
            builtin=name in {s.name for s in DEFAULT_SOURCES},
        )

    for name in data.get("disabled") or []:
        if isinstance(name, str):
            disabled.add(name)

    return _make(sources, disabled, path)
