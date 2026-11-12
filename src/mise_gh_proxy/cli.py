"""命令行入口：``mise-gh-mirror``。

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
from .config import RELEASE_SOURCES, Config, load_config

__all__ = ["main"]

SERVICE = "mise-gh-proxy.service"
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
    print(f"下载缓存: {count} 个文件, {_fmt_size(total)}  (mise-gh-mirror clean 可清理)")

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
    print(f"候选源实测 (取 {PROBE_BYTES // 1024 // 1024}MB 样本, 并发):")

    def probe(source):
        url = source.url(owner=SAMPLE_OWNER, repo=SAMPLE_REPO, tag=SAMPLE_TAG, file=SAMPLE_FILE)
        request = urllib.request.Request(url)
        request.add_header("Range", f"bytes=0-{PROBE_BYTES - 1}")
        request.add_header("User-Agent", "mise-gh-mirror/1.0")
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(RELEASE_SOURCES)) as pool:
        for name, speed, extra in pool.map(probe, RELEASE_SOURCES):
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


def cmd_sources(_config: Config) -> int:
    print("源池 (按优先级):")
    for index, source in enumerate(RELEASE_SOURCES, 1):
        note = ""
        if source.name == "ustc":
            note = "  # 国内镜像站, 速度最快但只覆盖部分仓库"
        elif source.name == "direct":
            note = "  # 官方直连, 最后兜底"
        print(f"  {index}. {source.name:<22} {source.template.split('{')[0][:44]}...{note}")
    print("\n代理采用并发竞速: 同时下载多个源, 最先完整下载成功的胜出,")
    print("并做长度校验 (防止部分服务截断大文件), 结果写入本地缓存。")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mise-gh-mirror", description="mise GitHub 下载代理的管理工具"
    )
    parser.add_argument(
        "command",
        choices=sorted(COMMANDS),
        help="status=服务状态 test=测速 sources=源池 clean=清缓存 logs=日志",
    )
    args = parser.parse_args(argv)
    return COMMANDS[args.command](load_config())


if __name__ == "__main__":
    sys.exit(main())
