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
    assert "SHA256SUMS" in text
    assert "RELEASE-MANIFEST.json" in text
    assert "rm -rf" in text
    assert "gzip -n" in text


def test_install_script_is_offline_only_and_fails_closed() -> None:
    text = Path("scripts/install-workerbee.sh").read_text(encoding="utf-8")
    assert "--no-compile" in text
    assert "--no-index" in text
    assert "--find-links" in text
    assert "refusing package-index fallback" in text
    assert "retrying with package index" not in text.lower()
    assert "pip install --upgrade pip" not in text


def test_wheelhouse_build_has_no_implicit_index_resolution() -> None:
    text = Path("scripts/build_wheelhouse.sh").read_text(encoding="utf-8")
    assert "--dependency-index-url" in text
    assert "--index-url" in text
    assert '"${RUNTIME_WHEELS[0]}"' in text
    assert text.count("pip wheel") >= 2
    assert "--no-index" in text
    assert "--no-build-isolation" in text
    assert "--no-deps" in text


def test_workerbee_runtime_pin_matches_project_version() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    release = pyproject["tool"]["workerbee"]["release"]
    runtime_pin = next(
        dep for dep in project["dependencies"] if dep.startswith("k1s-workerbee-runtime==")
    )

    assert runtime_pin == f"k1s-workerbee-runtime=={project['version']}"
    assert release["runtime-provider-lane"] == "legacy"
    assert release["runtime-provider-requirement"] == runtime_pin


def test_mcp_dependency_stays_on_the_compatible_major_lane() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "mcp>=1.8,<2" in project["dependencies"]
    assert '"mcp>=1.8,<2"' in Path("scripts/build_wheelhouse.sh").read_text(encoding="utf-8")


def test_workerbee_module_version_matches_project_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    init_text = Path("src/workerbee/__init__.py").read_text(encoding="utf-8")

    assert f'__version__ = "{project["version"]}"' in init_text
