import subprocess
import tomllib
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


def test_workerbee_runtime_pin_matches_project_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime_pin = next(
        dep for dep in project["dependencies"] if dep.startswith("k1s-workerbee-runtime==")
    )

    assert runtime_pin == f"k1s-workerbee-runtime=={project['version']}"


def test_workerbee_module_version_matches_project_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    init_text = Path("src/workerbee/__init__.py").read_text(encoding="utf-8")

    assert f'__version__ = "{project["version"]}"' in init_text
