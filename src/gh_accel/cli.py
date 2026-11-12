"""命令行入口：``gh-accel``。

子命令：

* ``status``  —— 服务状态、缓存占用、各仓库上次可用源
* ``test``    —— 实测各候选源速度
* ``sources`` —— 列出源池与优先级
* ``clean``   —— 清理下载缓存
* ``logs``    —— 查看代理最近日志
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .cache import FileCache, SourcePreference
from .config import CHAINS, Config, Source, load_config

__all__ = ["main"]

SERVICE = "gh-accel.service"
SAMPLE_OWNER = "astral-sh"
SAMPLE_REPO = "uv"
SAMPLE_TAG = "0.13.7"
SAMPLE_FILE = "uv-x86_64-unknown-linux-gnu.tar.gz"
PROBE_BYTES = 4 * 1024 * 1024
PROBE_TIMEOUT = 8
PROBE_MAX_DURATION = 10


def _fmt_size(num: int) -> str:
    return f"{num / 1024 / 1024:.1f} MB"


def cmd_status(config: Config) -> int:
    active = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE], capture_output=True, text=True
    ).stdout.strip()
    enabled = subprocess.run(
        ["systemctl", "--user", "is-enabled", SERVICE], capture_output=True, text=True
    ).stdout.strip()
    print(f"代理服务: {active} ({enabled})")
    print(f"  systemctl --user status {SERVICE}")

    cache = FileCache(config.file_cache_dir, config.max_cache_bytes)
    count, total = cache.stats()
    print(f"下载缓存: {count} 个文件, {_fmt_size(total)}  (gh-accel clean 可清理)")

    print(f"CA 凭据: {config.ca_pem}")

    preference = SourcePreference(config.source_cache_file)
    records = preference.all()
    if records:
        print("各仓库上次可用源:")
        for key, names in records.items():
            print(f"  {key}: {', '.join(names)}")

    try:
        request = urllib.request.Request(f"{config.base_url}/gh/", method="HEAD")
        urllib.request.urlopen(request, timeout=5)
        print(f"端口 {config.port}: 可连接")
    except urllib.error.HTTPError as exc:
        print(f"端口 {config.port}: 可连接 (HTTP {exc.code})")
    except Exception as exc:  # noqa: BLE001
        print(f"端口 {config.port}: 不可用 ({type(exc).__name__})")
    return 0


def cmd_test(config: Config) -> int:
    from .sources import load_registry

    registry = load_registry(config.sources_file)
    sources = registry.for_chain("release")
    print(f"候选源实测 (取 {PROBE_BYTES // 1024 // 1024}MB 样本, 并发):")

    def probe(source):
        url = source.url(owner=SAMPLE_OWNER, repo=SAMPLE_REPO, tag=SAMPLE_TAG, file=SAMPLE_FILE)
        request = urllib.request.Request(url)
        request.add_header("Range", f"bytes=0-{PROBE_BYTES - 1}")
        request.add_header("User-Agent", "gh-accel/1.0")
        started = time.time()
        try:
            response = urllib.request.urlopen(request, timeout=PROBE_TIMEOUT)
            read = 0
            while read < PROBE_BYTES:
                if time.time() - started > PROBE_MAX_DURATION:
                    break
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                read += len(chunk)
            response.close()
            elapsed = time.time() - started
            return source.name, (read / elapsed if elapsed > 0 else 0), read
        except Exception as exc:  # noqa: BLE001
            return source.name, None, type(exc).__name__

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as pool:
        for name, speed, extra in pool.map(probe, sources):
            if speed is None:
                print(f"  {name:<22} 不可用  ({extra})")
            else:
                print(f"  {name:<22} {speed / 1024:9.0f} KB/s   ({extra // 1024} KB)")
                results.append((speed, name))

    if results:
        speed, name = max(results)
        print(f"\n最快: {name}  ({speed / 1024:.0f} KB/s)")
        print("代理会自动并发竞速，无需手动指定。")
    return 0


def sources_main(argv: list[str]) -> int:
    """``gh-accel sources`` 的入口。选项在这里自己解析，避免与主 CLI 冲突。"""
    parser = argparse.ArgumentParser(prog="gh-accel sources", description="查看与编辑源池")
    parser.add_argument("--add", metavar="名字", help="新增一个源")
    parser.add_argument("--remove", metavar="名字", help="删除一个自定义源")
    parser.add_argument("--disable", metavar="名字", help="禁用一个源")
    parser.add_argument("--enable", metavar="名字", help="启用一个源")
    parser.add_argument("--reset", action="store_true", help="恢复内置源池")
    parser.add_argument("--template", metavar="URL", help="源的 URL 模板")
    parser.add_argument("--chains", metavar="a,b", help="适用的链路")
    parser.add_argument("--chain", default="release", help="单个链路（默认 release）")
    parser.add_argument("--priority", type=int, default=100, help="优先级，越小越靠前")
    args = parser.parse_args(argv)
    return cmd_sources(load_config(), args)


def cmd_sources(config: Config, args: argparse.Namespace) -> int:
    """查看与编辑源池。"""
    from .sources import load_registry

    registry = load_registry(config.sources_file)

    # ---- 编辑操作 ----
    if args.add:
        name = args.add
        if not args.template:
            print("错误: --add 需要同时给出 --template", file=sys.stderr)
            return 2
        template = args.template
        chain = args.chain
        templates = {chain: template}
        if args.chains:
            templates = dict.fromkeys(args.chains.split(","), template)
        registry.set_source(
            Source(name=name, templates=templates, priority=args.priority, builtin=False)
        )
        registry.save()
        print(f"已添加源 {name} (链路: {', '.join(sorted(templates))})")
        return 0

    if args.remove:
        if registry.remove_source(args.remove):
            registry.save()
            print(f"已删除源 {args.remove}")
        else:
            print(
                f"错误: 无法删除 {args.remove}（不存在，或是内置源——内置源请用 --disable）",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.disable:
        if registry.disable(args.disable):
            registry.save()
            print(f"已禁用源 {args.disable}")
        else:
            print(f"错误: 未知源 {args.disable}", file=sys.stderr)
            return 1
        return 0

    if args.enable:
        if registry.enable(args.enable):
            registry.save()
            print(f"已启用源 {args.enable}")
        else:
            print(f"错误: 未知源 {args.enable}", file=sys.stderr)
            return 1
        return 0

    if args.reset:
        registry.reset()
        registry.save()
        print("已恢复内置源池")
        return 0

    # ---- 列出 ----
    print(f"源池（配置文件: {config.sources_file}）")
    for chain in CHAINS:
        sources = registry.for_chain(chain)
        print(f"\n  [{chain}] {len(sources)} 个源")
        for index, source in enumerate(sources, 1):
            flag = ""
            if not source.builtin:
                flag = " (自定义)"
            template = source.templates[chain]
            print(f"    {index}. {source.name:<22} p{source.priority:<4} {template[:52]}{flag}")
    disabled = sorted(registry.disabled)
    if disabled:
        print(f"\n  已禁用: {', '.join(disabled)}")
    print("\n代理采用并发竞速: 同时下载多个源, 最先完整下载成功的胜出,")
    print("并做长度校验 (防止部分服务截断大文件), 结果写入本地缓存。")
    print("\n编辑方式:")
    print("  gh-accel sources --add <名字> --template <URL 模板> [--chains release,page]")
    print("  gh-accel sources --disable <名字> | --enable <名字> | --remove <名字>")
    print("  gh-accel sources --reset")
    return 0


def cmd_clean(config: Config) -> int:
    directory = config.file_cache_dir
    count = 0
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            path = os.path.join(root, name)
            try:
                total += os.path.getsize(path)
                os.unlink(path)
                count += 1
            except OSError:
                pass
    print(f"已删除 {count} 个缓存文件 ({_fmt_size(total)})")
    return 0


def cmd_logs(_config: Config) -> int:
    subprocess.run(["journalctl", "--user", "-u", SERVICE, "-n", "40", "--no-pager"])
    return 0


COMMANDS = {
    "status": cmd_status,
    "test": cmd_test,
    "sources": cmd_sources,
    "clean": cmd_clean,
    "logs": cmd_logs,
}


def cmd_install(config: Config, args: argparse.Namespace) -> int:
    """安装服务并配置客户端。"""
    from .installer import InstallOptions, install

    options = InstallOptions(
        prefix=args.prefix,
        root=args.root or os.getcwd(),
        port=args.port,
        with_service=not args.no_service,
        with_client=not args.no_client,
        dry_run=args.dry_run,
    )
    return install(options)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gh-accel", description="gh-accel —— GitHub 下载加速代理的管理工具"
    )
    parser.add_argument(
        "command",
        choices=sorted(COMMANDS) + ["install", "registry"],
        help="status=服务状态 test=测速 sources=源池 registry=镜像地址 "
        "clean=清缓存 logs=日志 install=安装服务并配置客户端",
    )
    parser.add_argument("--prefix", default="~/.local", help="命令安装目录")
    # sources 子命令的编辑选项
    # registry（镜像地址）子命令的选项
    parser.add_argument("--root", default="", help="源码目录（默认当前目录）")
    parser.add_argument("--port", type=int, default=19770, help="代理端口")
    parser.add_argument("--no-service", action="store_true", help="不装 systemd 服务")
    parser.add_argument("--no-client", action="store_true", help="不配客户端")
    parser.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args, extra = parser.parse_known_args(argv)
    if args.command == "install":
        return cmd_install(load_config(), args)
    if args.command == "sources":
        return sources_main(extra)
    if args.command == "registry":
        # registry 子命令的参数原样透传，避免两套选项名对不上
        from .registry_cli import main as registry_main

        return registry_main(extra)
    return COMMANDS[args.command](load_config())


if __name__ == "__main__":
    sys.exit(main())
