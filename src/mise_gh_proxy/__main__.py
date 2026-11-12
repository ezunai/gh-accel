"""``python -m mise_gh_proxy`` —— 启动代理服务。"""

from __future__ import annotations

import logging
import os
import signal
import sys

from .config import load_config
from .server import make_server


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    verbose = "-v" in argv or "--verbose" in argv

    config = load_config()
    _setup_logging(verbose)
    log = logging.getLogger("mise_gh_proxy")

    os.makedirs(config.file_cache_dir, exist_ok=True)
    os.makedirs(config.cert_dir, exist_ok=True)

    from .certificates import CertificateAuthority

    authority = CertificateAuthority(config.cert_dir, config.ca_pem, config.ca_key)
    if authority.ensure_ca():
        log.info("CA 就绪: %s", config.ca_pem)
    else:
        log.warning("无法生成 CA，MITM(HTTPS 代理) 模式不可用")

    server = make_server(config)
    log.info("mise-gh-proxy 监听于 http://%s:%d", config.host, config.port)

    def _shutdown(_signum, _frame):
        log.info("收到停止信号，退出")
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
