"""CA bundle 生成测试。

关键点：curl 的 ``cacert`` 与 Go 的 ``SSL_CERT_FILE`` 都是**替换**默认 CA
列表而非追加。只把本地 CA 放进去的话，直连任何其它站点都会因缺根证书
而校验失败 —— 表现是连接卡死或 "certificate verify failed"，很容易被
误判成网络问题。
"""

from __future__ import annotations

from pathlib import Path

from gh_accel.installer import build_ca_bundle


class TestBuildCaBundle:
    def test_bundle_contains_local_ca(self, tmp_path: Path) -> None:
        local = tmp_path / "ca.pem"
        local.write_text("-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----\n")
        bundle = build_ca_bundle(str(local), str(tmp_path / "bundle.pem"))
        assert "LOCAL" in Path(bundle).read_text(encoding="utf-8")

    def test_bundle_contains_system_ca(self, tmp_path: Path) -> None:
        local = tmp_path / "ca.pem"
        local.write_text("-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----\n")
        bundle = Path(build_ca_bundle(str(local), str(tmp_path / "bundle.pem")))
        text = bundle.read_text(encoding="utf-8")

        system = Path("/etc/ssl/certs/ca-certificates.crt")
        if not system.exists():
            return  # 其它发行版路径不同，跳过断言
        # 系统 CA 必须被包含进来 —— 否则直连其它站点会校验失败
        system_certs = system.read_text(encoding="utf-8").count("BEGIN CERTIFICATE")
        assert text.count("BEGIN CERTIFICATE") >= system_certs + 1

    def test_bundle_default_path(self, tmp_path: Path) -> None:
        local = tmp_path / "certs" / "ca.pem"
        local.parent.mkdir(parents=True)
        local.write_text("-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----\n")
        bundle = Path(build_ca_bundle(str(local)))
        assert bundle == local.parent / "ca-bundle.pem"
        assert bundle.exists()

    def test_bundle_is_world_readable(self, tmp_path: Path) -> None:
        """dockerd 以 root 运行，但普通用户也要能用。"""
        local = tmp_path / "ca.pem"
        local.write_text("-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----\n")
        bundle = Path(build_ca_bundle(str(local), str(tmp_path / "bundle.pem")))
        assert bundle.stat().st_mode & 0o044, "bundle 应对其它用户可读"
