"""本地 CA 与按 SNI 动态签发的叶证书。

MITM 模式需要为每个被访问的 host 现场签发证书。这里踩过的坑：

* CA 必须带 ``basicConstraints`` / ``keyUsage`` / ``subjectKeyIdentifier``。
  缺少 Authority Key Identifier 时，Python 的 OpenSSL 会以
  ``Missing Authority Key Identifier`` 拒绝整条证书链。
* ``openssl x509 -extfile`` 要求扩展写在**具名 section** 里，并用
  ``-extensions <section>`` 指定，否则整个扩展文件被忽略，证书将没有 SAN。
* 签发必须加锁并用临时文件 + 原子替换：并发请求同一 host 时会互相覆盖，
  客户端可能读到半成品证书（表现为 "certificate is not valid for any names"）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import ssl
import subprocess
import threading

__all__ = ["CertificateAuthority"]

log = logging.getLogger(__name__)

_CA_SUBJECT = "/CN=gh-accel local CA"
_CA_DAYS = "3650"
_EXT_SECTION = "v3_req"


class CertificateAuthority:
    """管理本地 CA 与叶证书。"""

    def __init__(self, cert_dir: str, ca_pem: str, ca_key: str) -> None:
        self._cert_dir = cert_dir
        self._ca_pem = ca_pem
        self._ca_key = ca_key
        self._lock = threading.Lock()

    # ---- CA ------------------------------------------------------------
    def ensure_ca(self) -> bool:
        """确保 CA 存在。返回是否可用。"""
        os.makedirs(self._cert_dir, exist_ok=True)
        if os.path.exists(self._ca_pem) and os.path.exists(self._ca_key):
            return True
        cmd = [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            self._ca_key,
            "-out",
            self._ca_pem,
            "-days",
            _CA_DAYS,
            "-nodes",
            "-subj",
            _CA_SUBJECT,
            "-addext",
            "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign,digitalSignature",
            "-addext",
            "subjectKeyIdentifier=hash",
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            os.chmod(self._ca_key, 0o600)
            return True
        except Exception as exc:  # noqa: BLE001 - 任何失败都视为 CA 不可用
            log.error("CA 生成失败: %s", exc)
            return False

    # ---- 叶证书 --------------------------------------------------------
    def leaf_cert(self, host: str) -> tuple[str, str] | None:
        """返回 (证书路径, 私钥路径)，失败返回 None。结果带缓存。"""
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in host)
        cert = os.path.join(self._cert_dir, f"{safe}.pem")
        key = os.path.join(self._cert_dir, f"{safe}.key")

        with self._lock:
            if os.path.exists(cert) and os.path.exists(key):
                return cert, key
            if not self.ensure_ca():
                return None
            csr = os.path.join(self._cert_dir, f"{safe}.{os.getpid()}.csr")
            ext = os.path.join(self._cert_dir, f"{safe}.{os.getpid()}.ext")
            tmp_cert = f"{cert}.{os.getpid()}.tmp"
            tmp_key = f"{key}.{os.getpid()}.tmp"
            try:
                with open(ext, "w", encoding="utf-8") as fh:
                    fh.write(f"[{_EXT_SECTION}]\n")
                    fh.write(f"subjectAltName=DNS:{host}\n")
                    fh.write("basicConstraints=critical,CA:FALSE\n")
                    fh.write("keyUsage=critical,digitalSignature,keyEncipherment\n")
                    fh.write("extendedKeyUsage=serverAuth\n")
                    fh.write("authorityKeyIdentifier=keyid:always\n")
                    fh.write("subjectKeyIdentifier=hash\n")
                subprocess.run(
                    [
                        "openssl",
                        "req",
                        "-newkey",
                        "rsa:2048",
                        "-keyout",
                        tmp_key,
                        "-out",
                        csr,
                        "-nodes",
                        "-subj",
                        f"/CN={host}",
                    ],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    [
                        "openssl",
                        "x509",
                        "-req",
                        "-in",
                        csr,
                        "-CA",
                        self._ca_pem,
                        "-CAkey",
                        self._ca_key,
                        "-CAcreateserial",
                        "-out",
                        tmp_cert,
                        "-days",
                        _CA_DAYS,
                        "-extfile",
                        ext,
                        "-extensions",
                        _EXT_SECTION,
                    ],
                    check=True,
                    capture_output=True,
                )
                os.chmod(tmp_key, 0o600)
                os.replace(tmp_key, key)
                os.replace(tmp_cert, cert)
                return cert, key
            except Exception as exc:  # noqa: BLE001
                log.error("证书签发失败 (%s): %s", host, exc)
                return None
            finally:
                for path in (csr, ext, tmp_cert, tmp_key):
                    with contextlib.suppress(OSError):
                        os.unlink(path)

    def tls_context(self, host: str) -> ssl.SSLContext | None:
        """构造用于该 host 的服务端 TLS 上下文。"""
        pair = self.leaf_cert(host)
        if pair is None:
            return None
        cert, key = pair
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        # 只宣告 http/1.1：代理按 HTTP/1.1 处理请求，客户端若协商到 h2 会失败
        with contextlib.suppress(NotImplementedError):  # pragma: no cover
            ctx.set_alpn_protocols(["http/1.1"])
        return ctx
