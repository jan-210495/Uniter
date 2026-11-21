from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "vps" / "install.sh"


@pytest.fixture(scope="module")
def fake_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("uniter-installer-bin")
    for name in (
        "systemctl", "systemd-analyze", "install", "getent", "id", "python3",
        "python3.12", "git", "curl", "ss", "useradd", "runuser", "apt-get",
        "dpkg-query", "dnf", "rpm", "sudo", "openssl", "update-ca-certificates",
        "update-ca-trust",
    ):
        tool = root / name
        tool.write_text("#!/bin/sh\nif [ \"${1:-}\" = cat ]; then exit 1; fi\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
    return root


def run_script(tmp_path: Path, fake_bin: Path, answers: str, os_release: str = "ID=debian\n") -> subprocess.CompletedProcess[str]:
    fake_etc = tmp_path / "etc"
    fake_etc.mkdir()
    (fake_etc / "os-release").write_text(os_release, encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8").replace("/etc/os-release", str(fake_etc / "os-release"))
    test_script = tmp_path / "installer.sh"
    test_script.write_text(script, encoding="utf-8")
    env = {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin", "EUID": "0"}
    return subprocess.run(["bash", str(test_script)], input=answers, text=True, capture_output=True, env=env, timeout=10)


def test_cli_exit_returns_without_mutations(tmp_path, fake_bin):
    result = run_script(tmp_path, fake_bin, "4\n")
    assert result.returncode == 0, result.stderr
    assert "Cancelled without changes" in result.stdout
    assert not (tmp_path / "opt").exists()


def test_cli_safety_notes_are_read_only(tmp_path, fake_bin):
    result = run_script(tmp_path, fake_bin, "3\n")
    assert result.returncode == 0, result.stderr
    assert "No firewall/DNS/Cloudflare/SSH/certificate" in result.stdout
    assert not (tmp_path / "opt").exists()


def test_unsupported_os_stops_before_menu_or_changes(tmp_path, fake_bin):
    result = run_script(tmp_path, fake_bin, "1\n", "ID=alpine\n")
    assert result.returncode != 0
    assert "Supported systems are Debian/Ubuntu" in result.stderr
    assert "Creating the service account" not in result.stdout


def test_preflight_offers_missing_python_and_declining_stops_before_mutation(tmp_path, fake_bin):
    (fake_bin / "python3.12").unlink()
    (fake_bin / "python3").unlink()
    (fake_bin / "dpkg-query").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (fake_bin / "dpkg-query").chmod(0o755)
    result = run_script(tmp_path, fake_bin, "1\nn\n", "ID=debian\nVERSION_ID=13\nVERSION_CODENAME=trixie\n")
    assert result.returncode != 0
    assert "The following required Debian/Ubuntu packages are missing" in result.stdout
    assert "No apt command was run" in result.stderr
    assert not (tmp_path / "opt").exists()
    (fake_bin / "python3.12").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "python3.12").chmod(0o755)
    (fake_bin / "python3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "python3").chmod(0o755)
    (fake_bin / "dpkg-query").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "dpkg-query").chmod(0o755)


def test_empty_hostname_selects_local_only_no_proxy():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "No hostname entered; configure DNS/TLS later" in text
    assert "Proxy configuration will not be created." in text
    assert 'if [[ -n $domain ]]; then' in text
    assert 'refuse_path "$NGINX_SITE"' in text


def test_installer_static_safety_contract():
    text = SCRIPT.read_text(encoding="utf-8")
    for required in (
        "127.0.0.1:8000", "UNITER_DB=", "runuser -u uniter", "systemd-analyze verify",
        "REPLACE_WITH_DOMAIN", "setup_code == 200", "No automatic rollback was attempted",
        "Using Python interpreter", "apt-get install -y", "Approve installing the missing packages?", "refs/tags/$tag",
    ):
        assert required in text
    lowered = text.lower()
    for forbidden in (
        "ufw", "iptables", "nft ", "cloudflared tunnel", "ssh-keygen", "certbot",
        "chmod 777", "curl | bash", "curl|bash", "password:",
    ):
        assert forbidden not in lowered
    assert "allow 127.0.0.1;" in (SCRIPT.parents[0] / "nginx.conf.example").read_text(encoding="utf-8")
    assert "deny all;" in (SCRIPT.parents[0] / "nginx.conf.example").read_text(encoding="utf-8")


def test_installer_has_no_existing_install_mutation_paths():
    text = SCRIPT.read_text(encoding="utf-8")
    for guard in (
        "id -u uniter", "systemctl cat uniter", 'refuse_path "$path"',
        '[[ ! -e "$STATE_DIR/uniter.db"', 'ln -s "$release_dir" "$INSTALL_ROOT/current"',
    ):
        assert guard in text
    assert "systemctl disable" not in text
    assert "rm -rf" not in text
    assert "DROP TABLE" not in text
