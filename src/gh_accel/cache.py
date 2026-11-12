"""缓存层。

两类缓存：

* **源偏好缓存** (``sources.json``) —— 记录每个仓库上次成功的源，下次优先使用，
  省掉一次竞速。
* **文件缓存** (``files/``) —— 完整下载的产物。按原始 GitHub URL 作键，因此
  跨源通用：不论最终是哪个源下载下来的，换源后依然命中缓存。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

__all__ = ["SourcePreference", "FileCache"]

log = logging.getLogger(__name__)


class SourcePreference:
    """记录并查询「哪个仓库上次用哪个源成功」。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._data = {k: list(v) for k, v in data.items()}
        except (OSError, ValueError):
            self._data = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("源偏好缓存写入失败: %s", exc)

    def preferred(self, key: str) -> list[str]:
        """返回该 key 的源名列表，按偏好排序。"""
        with self._lock:
            return list(self._data.get(key, []))

    def remember(self, key: str, name: str) -> None:
        """把 name 记为该 key 的首选源。"""
        with self._lock:
            order = self._data.get(key, [])
            if name in order:
                order.remove(name)
            order.insert(0, name)
            self._data[key] = order
        self._save()

    def forget(self, key: str, name: str) -> None:
        """把 name 从该 key 的偏好里去掉。"""
        with self._lock:
            order = self._data.get(key, [])
            if name in order:
                order.remove(name)
                self._data[key] = order
        self._save()

    def all(self) -> dict[str, list[str]]:
        with self._lock:
            return {k: list(v) for k, v in self._data.items()}


class FileCache:
    """按 URL 键存放完整文件的缓存。"""

    def __init__(self, directory: str, max_bytes: int) -> None:
        self._dir = directory
        self._max_bytes = max_bytes

    def path_for(self, url: str) -> str:
        """返回 url 对应的缓存文件路径。"""
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return os.path.join(self._dir, digest[:2], digest)

    def get(self, url: str) -> str | None:
        """命中则返回文件路径，否则 None。"""
        path = self.path_for(url)
        return path if os.path.exists(path) else None

    def store(self, url: str, source_path: str) -> str:
        """把临时文件移入缓存，返回最终路径。"""
        dst = self.path_for(url)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                os.unlink(dst)
            os.replace(source_path, dst)
            return dst
        except OSError:
            return source_path

    def prune(self) -> None:
        """按总大小上限清理缓存（最旧的先删）。"""
        entries: list[tuple[float, int, str]] = []
        total = 0
        for root, _dirs, files in os.walk(self._dir):
            for name in files:
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                total += st.st_size
                entries.append((st.st_mtime, st.st_size, path))
        if total <= self._max_bytes:
            return
        for _mtime, size, path in sorted(entries):
            if total <= self._max_bytes:
                break
            try:
                os.unlink(path)
                total -= size
            except OSError:
                pass

    def stats(self) -> tuple[int, int]:
        """返回 (文件数, 总字节数)。"""
        count = 0
        total = 0
        for root, _dirs, files in os.walk(self._dir):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                    count += 1
                except OSError:
                    pass
        return count, total
