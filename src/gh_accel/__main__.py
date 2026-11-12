"""``python -m gh_accel`` —— 启动代理服务。"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

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
    log = logging.getLogger("gh_accel")

    os.makedirs(config.file_cache_dir, exist_ok=True)
    os.makedirs(config.cert_dir, exist_ok=True)

    from .certificates import CertificateAuthority

    authority = CertificateAuthority(config.cert_dir, config.ca_pem, config.ca_key)
    if authority.ensure_ca():
        log.info("CA 就绪: %s", config.ca_pem)
    else:
        log.warning("无法生成 CA，MITM(HTTPS 代理) 模式不可用")

    server = make_server(config)
    log.info("gh-accel 监听于 http://%s:%d", config.host, config.port)

    def _shutdown(_signum, _frame):
        log.info("收到停止信号，退出")
        # ``shutdown()`` 会等待 ``serve_forever()`` 的主循环退出；而信号处理
        # 函数本身就在那个主线程里执行，直接调用会自己等自己 —— 死锁。
        # 必须另起一个线程来调用，这也是 http.server 文档里的要求。
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
