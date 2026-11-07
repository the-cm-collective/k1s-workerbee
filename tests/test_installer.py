import os
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
    assert "SBOM.cdx.json" in text
    assert "audit-archive" in text
    assert "verify-sha256s" in text
    assert "--find-links <wheelhouse>" in text
    assert "--find-links ${OUT_DIR}" not in text
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
    assert "pip check" in text
    assert 'providers != ["k1s-core"]' in text


def test_standalone_install_uses_validated_versioned_venv_and_atomic_launcher() -> None:
    text = Path("scripts/install-workerbee.sh").read_text(encoding="utf-8")

    assert "workerbee-install" in text
    assert 'releases_dir="$INSTALL_DIR/releases"' in text
    assert ".candidate.XXXXXX" in text
    assert 'mv "$candidate_dir" "$release_dir"' in text
    assert 'atomic_link current "releases/$release_id"' in text
    assert 'exec "$INSTALL_DIR/current/bin/python" -m workerbee' in text
    assert "workerbee.standalone-install/v1" in text
    assert '"runtime_state": "outside-versioned-venv"' in text
    assert "--rollback" in text
    assert "wheelhouse archive must contain exactly one top-level directory" in text


def test_standalone_rollback_switches_only_versioned_release_links(tmp_path: Path) -> None:
    install_dir = tmp_path / "workerbee-install"
    for release in ("current-release", "previous-release"):
        binary = install_dir / "releases" / release / "bin" / "python"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o700)
    (install_dir / "current").symlink_to("releases/current-release")
    (install_dir / "previous").symlink_to("releases/previous-release")
    env = {
        **os.environ,
        "WORKERBEE_INSTALL_DIR": str(install_dir),
    }

    proc = subprocess.run(
        ["sh", "scripts/install-workerbee.sh", "--rollback"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert (install_dir / "current").readlink() == Path("releases/previous-release")
    assert (install_dir / "previous").readlink() == Path("releases/current-release")


def test_wheelhouse_build_is_local_only_and_has_no_index_resolution() -> None:
    text = Path("scripts/build_wheelhouse.sh").read_text(encoding="utf-8")
    assert "--dependency-wheelhouse" in text
    assert "--k1s-core-wheel" in text
    assert "verify-runtime-artifact" in text
    assert "PIP_NO_INDEX=1" in text
    assert "PIP_CONFIG_FILE=/dev/null" in text
    assert "--no-index" in text
    assert "--no-build-isolation" in text
    assert "--no-deps" in text
    assert "--index-url" not in text
    assert "--extra-index-url" not in text
    assert "WORKERBEE_DEPENDENCY_INDEX_URL" not in text


def test_runtime_provider_pin_and_artifact_identity_are_exact() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    release = pyproject["tool"]["workerbee"]["release"]
    runtime_pin = next(dep for dep in project["dependencies"] if dep.startswith("k1s-core=="))

    assert runtime_pin == "k1s-core==0.1.8a1"
    assert release["published-baseline-version"] == "0.1.7"
    assert release["runtime-provider-lane"] == "k1s-core"
    assert release["runtime-provider-requirement"] == runtime_pin
    assert release["runtime-provider-artifact"] == "k1s_core-0.1.8a1-py3-none-any.whl"
    assert release["runtime-provider-size"] == 879766
    assert (
        release["runtime-provider-sha256"]
        == "11103d1df772d9618e023d40a8bdfeecb10feac7a5a374fa2efe0b1ac607cb30"
    )
    assert release["runtime-provider-publication-status"] == "temporary-nonpublished"
    assert release["forbidden-runtime-provider"] == "k1s-workerbee-runtime"
    assert not any(dep.startswith("k1s-workerbee-runtime") for dep in project["dependencies"])


def test_mcp_dependency_stays_on_the_compatible_major_lane() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "mcp>=1.8,<2" in project["dependencies"]
    assert '"mcp>=1.8,<2"' in Path("scripts/build_wheelhouse.sh").read_text(encoding="utf-8")


def test_workerbee_module_version_matches_project_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    init_text = Path("src/workerbee/__init__.py").read_text(encoding="utf-8")

    assert f'__version__ = "{project["version"]}"' in init_text
