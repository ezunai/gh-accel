"""安装器测试。"""

from __future__ import annotations

from pathlib import Path

from gh_accel.installer import InstallOptions, has_systemd_user, unit_template

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestUnitTemplate:
    def test_template_exists_in_repo(self) -> None:
        assert (REPO_ROOT / "systemd" / "gh-accel.service").is_file()

    def test_template_has_no_unresolved_placeholders(self) -> None:
        text = (REPO_ROOT / "systemd" / "gh-accel.service").read_text(encoding="utf-8")
        assert "@GH_ACCEL_ROOT@" in text, "模板应保留占位符供安装时替换"

    def test_rendered_unit_contains_root(self) -> None:
        text = unit_template("/opt/gh-accel", python="/usr/bin/python3")
        assert "@GH_ACCEL_ROOT@" not in text
        assert "/opt/gh-accel" in text
        assert "PYTHONPATH=/opt/gh-accel/src" in text
        assert "ExecStart=/usr/bin/python3 -m gh_accel" in text

    def test_packaged_copy_matches_source(self) -> None:
        """包内 data/ 的副本必须与 systemd/ 下的源文件一致，避免两处漂移。"""
        source = (REPO_ROOT / "systemd" / "gh-accel.service").read_text(encoding="utf-8")
        packaged = REPO_ROOT / "src" / "gh_accel" / "data" / "gh-accel.service"
        assert packaged.is_file(), "打包副本缺失"
        assert packaged.read_text(encoding="utf-8") == source

    def test_packaged_scripts_match_source(self) -> None:
        for name in ("gh-accel-get", "gh-accel-env"):
            source = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
            packaged = REPO_ROOT / "src" / "gh_accel" / "data" / name
            assert packaged.is_file(), f"打包副本缺失: {name}"
            assert packaged.read_text(encoding="utf-8") == source


class TestInstallOptions:
    def test_defaults(self) -> None:
        options = InstallOptions()
        assert options.port == 19770
        assert options.with_service is True
        assert options.with_client is True

    def test_dry_run_does_not_touch_system(self, tmp_path: Path) -> None:
        from gh_accel.installer import install

        options = InstallOptions(
            prefix=str(tmp_path / "prefix"),
            root=str(REPO_ROOT),
            with_service=False,
            with_client=False,
            dry_run=True,
        )
        assert install(options) == 0


class TestSystemdDetection:
    def test_returns_bool(self) -> None:
        assert isinstance(has_systemd_user(), bool)
