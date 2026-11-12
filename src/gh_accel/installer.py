"""安装逻辑：装 systemd 服务、生成 CA、配置客户端。

把原先只存在于 ``scripts/install.sh`` 里的逻辑收进包内，好处是：

* PyPI 用户 ``pip install gh-accel`` 后直接 ``gh-accel install`` 即可，
  不必再去 GitHub 拿脚本；
* ``scripts/install.sh`` 退化成薄包装，与 ``gh-accel install`` 共用同一份
  实现，不会出现两套逻辑漂移。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

__all__ = ["InstallOptions", "install", "unit_template", "has_systemd_user"]

#: systemd unit 的文件名
UNIT_NAME = "gh-accel.service"

#: unit 模板的查找位置。systemd/ 是唯一来源，打包时复制到包内 data/。
UNIT_SEARCH_PATHS = (
    "systemd",  # 源码仓库布局
    os.path.join(os.path.dirname(__file__), "data"),  # 打包后的布局
)


def _find_file(*names: str) -> str | None:
    """在已知位置里查找文件，返回第一个存在的路径。"""
    roots = [os.path.join(os.getcwd(), d) for d in ("systemd", "scripts")]
    roots += [os.path.join(os.path.dirname(__file__), "data")]
    for root in roots:
        for name in names:
            path = os.path.join(root, name)
            if os.path.exists(path):
                return path
    return None


@dataclass
class InstallOptions:
    """安装选项。"""

    prefix: str = "~/.local"
    root: str = ""
    port: int = 19770
    with_service: bool = True
    with_client: bool = True
    dry_run: bool = False
    actions: list[str] = field(default_factory=list)


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def unit_template(root: str, python: str = sys.executable) -> str:
    """读取 systemd unit 模板并填入实际路径。

    模板只有一个来源（``systemd/gh-accel.service``），打包时复制到包内
    ``data/``。此处不在 Python 里再内嵌一份，避免两处漂移。
    """
    path = _find_file(UNIT_NAME)
    if path is None:
        raise FileNotFoundError(f"找不到 systemd unit 模板：{UNIT_NAME}")
    with open(path, encoding="utf-8") as fh:
        template = fh.read()
    return template.replace("@GH_ACCEL_ROOT@", root).replace("@GH_ACCEL_PYTHON@", python)


def has_systemd_user() -> bool:
    """当前环境是否支持 systemd user 服务。"""
    if not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _run(cmd: list[str], dry_run: bool) -> tuple[int, str]:
    if dry_run:
        return 0, ""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode, (result.stderr or result.stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def build_ca_bundle(ca_pem: str, bundle: str | None = None) -> str:
    """把系统 CA 与本地 CA 合并成一个 bundle。

    必须合并而不是只用本地 CA：curl 的 ``cacert`` 与 Go 的
    ``SSL_CERT_FILE`` 都是**替换**默认 CA 列表，不是追加。只放本地 CA
    的话，直连任何其它站点都会因缺根证书而校验失败（表现为连接卡死或
    certificate verify failed）。
    """
    if bundle is None:
        bundle = os.path.join(os.path.dirname(ca_pem), "ca-bundle.pem")
    system_candidates = [
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/ca-bundle.pem",
    ]
    system_ca = next((p for p in system_candidates if os.path.exists(p)), None)

    os.makedirs(os.path.dirname(bundle), exist_ok=True)
    with open(bundle, "wb") as out:
        if system_ca:
            with open(system_ca, "rb") as src:
                out.write(src.read())
                out.write(b"\n")
        with open(ca_pem, "rb") as src:
            out.write(src.read())
            out.write(b"\n")
    with contextlib.suppress(OSError):
        os.chmod(bundle, 0o644)
    return bundle


def _ensure_ca(ca_pem: str, dry_run: bool) -> bool:
    """确保本地 CA 存在，返回是否可用。"""
    if os.path.exists(ca_pem):
        return True
    if not shutil.which("openssl"):
        return False
    ca_key = ca_pem[:-4] + ".key" if ca_pem.endswith(".pem") else ca_pem + ".key"
    os.makedirs(os.path.dirname(ca_pem), exist_ok=True)
    cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-keyout",
        ca_key,
        "-out",
        ca_pem,
        "-days",
        "3650",
        "-nodes",
        "-subj",
        "/CN=gh-accel local CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign,digitalSignature",
        "-addext",
        "subjectKeyIdentifier=hash",
    ]
    code, _ = _run(cmd, dry_run)
    if code == 0:
        with contextlib.suppress(OSError):
            os.chmod(ca_key, 0o600)
        return True
    return False


def install(options: InstallOptions) -> int:
    """执行安装。返回进程退出码。"""
    bindir = os.path.join(_expand(options.prefix), "bin")
    root = _expand(options.root) if options.root else os.getcwd()
    unit_dir = os.path.join(_expand("~/.config/systemd/user"))
    ca_pem = os.path.join(_expand("~/.config/gh-accel/certs"), "ca.pem")

    def say(msg: str) -> None:
        print(f"==> {msg}")

    # ---- 1. 命令行工具 ---------------------------------------------------
    say(f"安装命令行工具到 {bindir}")
    os.makedirs(bindir, exist_ok=True)
    for name in ("gh-accel-get", "gh-accel-env", "docker-proxy.sh"):
        src = _find_file(name)
        if src is None:
            print(f"  跳过 {name}（未找到）")
            continue
        target = name if name.endswith(".sh") else name
        shutil.copy2(src, os.path.join(bindir, target))
        os.chmod(os.path.join(bindir, target), 0o755)
        print(f"  {bindir}/{target}")
    wrapper = os.path.join(bindir, "gh-accel")
    with open(wrapper, "w", encoding="utf-8") as fh:
        fh.write(
            "#!/usr/bin/env bash\n"
            "# gh-accel —— GitHub 下载加速代理的管理工具\n"
            "set -euo pipefail\n"
            f'ROOT="${{GH_ACCEL_HOME:-{root}}}"\n'
            'exec env PYTHONPATH="${ROOT}/src" '
            '"${GH_ACCEL_PYTHON:-python3}" -m gh_accel.cli "$@"\n'
        )
    os.chmod(wrapper, 0o755)
    print(f"  {wrapper}")

    # ---- 2. systemd 服务 -------------------------------------------------
    if options.with_service:
        if has_systemd_user():
            say("安装 systemd user 服务")
            os.makedirs(unit_dir, exist_ok=True)
            unit_path = os.path.join(unit_dir, UNIT_NAME)
            with open(unit_path, "w", encoding="utf-8") as fh:
                fh.write(unit_template(root))
            _run(["systemctl", "--user", "daemon-reload"], options.dry_run)
            _run(["systemctl", "--user", "enable", "--now", UNIT_NAME], options.dry_run)
            code, _ = _run(["systemctl", "--user", "is-active", UNIT_NAME], options.dry_run)
            if code == 0:
                print(f"  {UNIT_NAME} 已启动")
            else:
                print(
                    f"  注意：服务未正常启动，看 'journalctl --user -u {UNIT_NAME}'",
                    file=sys.stderr,
                )
        else:
            print("警告：当前环境不支持 systemd user 服务，跳过。", file=sys.stderr)
            print(f"       可手动运行： PYTHONPATH={root}/src python3 -m gh_accel", file=sys.stderr)

    # ---- 3. 客户端配置 ---------------------------------------------------
    if options.with_client:
        say("配置客户端")
        if not _ensure_ca(ca_pem, options.dry_run):
            print(f"警告：无法生成 CA（{ca_pem}），客户端可能无法验证证书", file=sys.stderr)
        else:
            print(f"  CA  {ca_pem}")
            # 客户端要的是「系统 CA + 本地 CA」的合集：curl 的 cacert 与
            # Go 的 SSL_CERT_FILE 都是替换而非追加
            bundle = build_ca_bundle(ca_pem)
            print(f"  CA bundle（系统 CA + 本地 CA）  {bundle}")

        proxy = f"http://127.0.0.1:{options.port}"
        if shutil.which("git"):
            _run(
                ["git", "config", "--global", f"http.https://github.com/.proxy={proxy}"],
                options.dry_run,
            )
            _run(["git", "config", "--global", f"http.sslCAInfo={ca_pem}"], options.dry_run)
            print("  git   仅 github.com 走代理")

        if shutil.which("curl"):
            path = os.path.join(_expand("~"), ".curlrc")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# 通过 gh-accel 代理访问 GitHub\n")
                fh.write(f"cacert = {bundle}\n")
                fh.write(f'proxy = "{proxy}"\n')
            print(f"  curl  {path}")

        if shutil.which("wget"):
            # wget 1.25 起读 XDG 路径，不是 ~/.wgetrc
            path = os.path.join(_expand("~/.config/wget"), "wgetrc")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# 通过 gh-accel 代理访问 GitHub\n")
                fh.write(f"ca_certificate = {bundle}\n")
                fh.write(f"https_proxy = {proxy}\n")
            print(f"  wget  {path}")

        mise_cfg = os.path.join(_expand("~/.config/mise"), "config.toml")
        if os.path.exists(mise_cfg):
            with open(mise_cfg, encoding="utf-8") as fh:
                content = fh.read()
            if "url_replacements" not in content:
                print(f"\n提示：把下面几行加进 {mise_cfg}\n")
                print("[settings.url_replacements]")
                print(f"'regex:^https://github\\.com/' = \"{proxy}/gh/\"")
                print(f"'regex:^https://raw\\.githubusercontent\\.com/' = \"{proxy}/raw/\"")
                print()

    # ---- 4. 验证 --------------------------------------------------------
    say("验证")
    from .cli import main as cli_main

    return cli_main(["status"])
