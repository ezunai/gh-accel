"""GitHub 下载加速代理（gh-accel）。

把 GitHub 下载导向本地代理，按「国内镜像优先 → 多源并发竞速 → 长度校验
→ 本地缓存」工作，并通过 MITM（CONNECT 隧道）把加速能力开放给 curl / wget /
git / python 等客户端。

用法::

    python -m gh_accel            # 启动代理服务
    gh-accel status              # 管理工具
"""

from __future__ import annotations

__version__ = "1.1.0"

from .config import Config, Source, load_config

__all__ = ["Config", "Source", "load_config", "__version__"]
