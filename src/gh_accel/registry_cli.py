"""registry mirror 的命令行管理：``gh-accel registry``。

管理「镜像地址」—— 即 Docker daemon 的 ``registry-mirrors`` 背后的上游
列表。本机的 ``registry-1.docker.io`` 与 ``auth.docker.io`` 都被 DNS 污染
（解析到 Facebook 的 IPv6 段），直连必然失败，所以镜像必须经由可达的
上游。本地 mirror 把这些上游收进项目统一管理，并提供故障转移。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.request

from .config import Config, load_config
from .registry import DEFAULT_GROUPS, load_registry_config

__all__ = ["main"]

SAMPLE_IMAGE = "library/alpine"
SAMPLE_REF = "latest"


def _probe(url: str, timeout: int = 10) -> tuple[int, float, int]:
    """探测一个上游：返回 (状态码, 耗时, 字节数)。"""
    target = f"{url.rstrip('/')}/v2/{SAMPLE_IMAGE}/manifests/{SAMPLE_REF}"
    request = urllib.request.Request(target)
    request.add_header(
        "Accept",
        "application/vnd.docker.distribution.manifest.list.v2+json,"
        "application/vnd.oci.image.index.v1+json",
    )
    request.add_header("User-Agent", "gh-accel-registry/1.0")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, time.time() - started, len(body)
    except urllib.error.HTTPError as exc:
        return exc.code, time.time() - started, 0
    except Exception:  # noqa: BLE001
        return 0, time.time() - started, 0


def _print_groups(config: Config) -> None:
    registry_config = load_registry_config(config.registries_file)
    print(f"镜像组（配置文件: {config.registries_file}）")
    for name, spec in registry_config.specs.items():
        default = "（默认）" if name == registry_config.default else ""
        print(f"\n  [{name}] {default}")
        for upstream in spec.upstreams:
            print(f"    {upstream}")
        if spec.token_service:
            print(f"    token: {spec.token_service}?service={spec.service}")
    print(f"\n  daemon 的 registry-mirrors 指向本机: http://{config.host}:{config.port}")


def _test(config: Config) -> int:
    registry_config = load_registry_config(config.registries_file)
    print(f"上游测速（拉 {SAMPLE_IMAGE}:{SAMPLE_REF} 的 manifest）：")
    for name, spec in registry_config.specs.items():
        print(f"\n  [{name}]")

        def probe(upstream: str) -> tuple[str, int, float, int]:
            status, elapsed, size = _probe(upstream)
            return upstream, status, elapsed, size

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(spec.upstreams), 1)) as pool:
            for upstream, status, elapsed, size in pool.map(probe, spec.upstreams):
                mark = "OK " if status == 200 else f"{status or 'ERR':>3}"
                print(f"    {mark}  {elapsed:6.2f}s  {size:>7} B  {upstream}")
    return 0


def _set_upstream(config: Config, group: str, upstream: str, remove: bool) -> int:
    registry_config = load_registry_config(config.registries_file)
    spec = registry_config.specs.get(group)
    if spec is None:
        print(f"错误: 未知镜像组 {group}", file=sys.stderr)
        return 1

    upstreams = list(spec.upstreams)
    if remove:
        if upstream not in upstreams:
            print(f"错误: {group} 中没有 {upstream}", file=sys.stderr)
            return 1
        upstreams.remove(upstream)
    elif upstream not in upstreams:
        upstreams.insert(0, upstream)

    from .registry import RegistrySpec

    registry_config.specs[group] = RegistrySpec(
        name=spec.name,
        upstreams=tuple(upstreams),
        token_service=spec.token_service,
        service=spec.service,
    )
    registry_config.save()
    action = "移除" if remove else "添加"
    print(f"已{action}上游 {upstream}（组 {group}）")
    return 0


def _reset(config: Config) -> int:
    registry_config = load_registry_config(config.registries_file)
    from .registry import RegistrySpec

    registry_config.specs = {
        name: RegistrySpec(name=name, **kwargs)  # type: ignore[arg-type]
        for name, kwargs in DEFAULT_GROUPS.items()
    }
    registry_config.save()
    print("已恢复内置镜像组")
    return 0


def _daemon_config(config: Config) -> int:
    """打印应写入 /etc/docker/daemon.json 的内容。"""
    snippet = {
        "registry-mirrors": [f"http://{config.host}:{config.port}"],
    }
    print("把下面这行加进 /etc/docker/daemon.json 的 registry-mirrors：")
    print()
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    print()
    print("改完需要重启 dockerd:  sudo systemctl restart docker")
    print("（本机的 registry-1.docker.io 与 auth.docker.io 被 DNS 污染，")
    print("  直连必然失败，必须经由镜像地址）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gh-accel registry",
        description="管理 Docker 镜像地址（registry mirror 的上游）",
    )
    parser.add_argument("--group", default="dockerhub", help="镜像组名")
    parser.add_argument("--add-upstream", metavar="URL", help="添加一个上游")
    parser.add_argument("--remove-upstream", metavar="URL", help="移除一个上游")
    parser.add_argument("--test", action="store_true", help="实测各上游速度")
    parser.add_argument("--reset", action="store_true", help="恢复内置镜像组")
    parser.add_argument("--daemon-config", action="store_true", help="打印 daemon.json 配置片段")
    args = parser.parse_args(argv)

    config = load_config()

    if args.test:
        return _test(config)
    if args.reset:
        return _reset(config)
    if args.daemon_config:
        return _daemon_config(config)
    if args.add_upstream:
        return _set_upstream(config, args.group, args.add_upstream, remove=False)
    if args.remove_upstream:
        return _set_upstream(config, args.group, args.remove_upstream, remove=True)
    _print_groups(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
