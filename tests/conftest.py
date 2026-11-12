"""测试夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gh_accel.config import Config  # noqa: E402


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """指向临时目录的配置。"""
    return Config(
        host="127.0.0.1",
        port=0,
        cache_dir=str(tmp_path / "cache"),
        certs_dir=str(tmp_path / "certs"),
        timeout=5.0,
        probe_timeout=3.0,
        probe_max_duration=2.0,
        probe_bytes=64 * 1024,
        max_cache_bytes=1024 * 1024,
        race_limit=2,
    )
