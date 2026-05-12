import subprocess
from pathlib import Path


def test_install_script_is_valid_shell() -> None:
    script = Path("scripts/install-workerbee.sh")
    proc = subprocess.run(["sh", "-n", str(script)], text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr


def test_wheelhouse_build_script_creates_release_archive() -> None:
    text = Path("scripts/build_wheelhouse.sh").read_text(encoding="utf-8")
    assert "workerbee-wheelhouse.tar.gz" in text
    assert "rm -rf" in text
    assert "tar -C" in text


def test_install_script_disables_bytecode_compile_for_package_install() -> None:
    text = Path("scripts/install-workerbee.sh").read_text(encoding="utf-8")
    assert "pip install --no-compile --no-index --find-links" in text
    assert "pip install --no-compile --find-links" in text
