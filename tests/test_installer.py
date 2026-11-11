import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path


def _write_fake_release(release_dir: Path, *, shebang: Path | None = None) -> None:
    bin_dir = release_dir / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "workerbee" ]; then exit 0; fi\n'
        'exec /bin/sh "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o700)
    interpreter = shebang or python
    for name in ("workerbee", "ae"):
        script = bin_dir / name
        script.write_text(f"#!{interpreter}\nexit 0\n", encoding="utf-8")
        script.chmod(0o700)


def _installer_env(install_dir: Path, bin_dir: Path) -> dict[str, str]:
    return {
        "HOME": str(install_dir.parent),
        "PATH": os.environ["PATH"],
        "PYTHON": sys.executable,
        "WORKERBEE_BIN_DIR": str(bin_dir),
        "WORKERBEE_FORCE_STANDALONE": "1",
        "WORKERBEE_INSTALL_DIR": str(install_dir),
    }


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
    assert ".candidate.XXXXXX" not in text
    assert "candidate_dir" not in text
    assert 'mkdir "$release_dir"' in text
    assert '"$python_bin" -m venv "$release_dir"' in text
    assert 'created_release_dir="$release_dir"' in text
    assert 'validate_release_entrypoints "$release_dir"' in text
    assert '"$release_dir/bin/workerbee" --version' in text
    assert '"$release_dir/bin/ae" version' in text
    assert '"$INSTALL_DIR/current/bin/workerbee" --version' in text
    assert '"$INSTALL_DIR/current/bin/ae" version' in text
    assert 'exec "$release_dir/bin/python" -m workerbee' in text
    assert '"$launcher_probe" --version' in text
    assert 'atomic_link current "releases/$release_id"' in text
    assert 'exec "$INSTALL_DIR/current/bin/python" -m workerbee' in text
    assert "workerbee.standalone-install/v1" in text
    assert '"runtime_state": "outside-versioned-venv"' in text
    assert "--rollback" in text
    assert "wheelhouse archive must contain exactly one top-level directory" in text


def test_standalone_rollback_switches_only_versioned_release_links(tmp_path: Path) -> None:
    install_dir = tmp_path / "workerbee-install"
    for release in ("current-release", "previous-release"):
        _write_fake_release(install_dir / "releases" / release)
    (install_dir / "current").symlink_to("releases/current-release")
    (install_dir / "previous").symlink_to("releases/previous-release")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "workerbee"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{install_dir}/current/bin/python" -m workerbee "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    proc = subprocess.run(
        ["sh", "scripts/install-workerbee.sh", "--rollback"],
        text=True,
        capture_output=True,
        env=_installer_env(install_dir, bin_dir),
    )

    assert proc.returncode == 0, proc.stderr
    assert (install_dir / "current").readlink() == Path("releases/previous-release")
    assert (install_dir / "previous").readlink() == Path("releases/current-release")
    for name, args in (("workerbee", ["--version"]), ("ae", ["version"])):
        script = install_dir / "current" / "bin" / name
        shebang_target = Path(script.read_text(encoding="utf-8").splitlines()[0][2:])
        assert shebang_target.exists()
        assert subprocess.run([str(script), *args], check=False).returncode == 0
    assert subprocess.run([str(wrapper), "--version"], check=False).returncode == 0


def test_standalone_rollback_rejects_stale_shebang_without_link_mutation(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "workerbee-install"
    _write_fake_release(install_dir / "releases" / "current-release")
    stale = install_dir / "releases" / ".candidate.gone" / "bin" / "python"
    _write_fake_release(
        install_dir / "releases" / "previous-release",
        shebang=stale,
    )
    (install_dir / "current").symlink_to("releases/current-release")
    (install_dir / "previous").symlink_to("releases/previous-release")
    current_before = (install_dir / "current").readlink()
    previous_before = (install_dir / "previous").readlink()

    proc = subprocess.run(
        ["sh", "scripts/install-workerbee.sh", "--rollback"],
        text=True,
        capture_output=True,
        env=_installer_env(install_dir, tmp_path / "bin"),
    )

    assert proc.returncode != 0
    assert "rollback release is invalid" in proc.stderr
    assert (install_dir / "current").readlink() == current_before
    assert (install_dir / "previous").readlink() == previous_before


def test_failed_offline_install_preserves_links_and_cleans_final_release(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "workerbee-install"
    for release in ("current-release", "previous-release"):
        (install_dir / "releases" / release).mkdir(parents=True)
    (install_dir / "current").symlink_to("releases/current-release")
    (install_dir / "previous").symlink_to("releases/previous-release")
    current_before = (install_dir / "current").readlink()
    previous_before = (install_dir / "previous").readlink()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "workerbee"
    wrapper.write_text("original launcher\n", encoding="utf-8")

    release_files = tmp_path / "release-files"
    release_files.mkdir()
    wheel = release_files / "k1s_workerbee-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"intentionally invalid wheel")
    sbom = release_files / "SBOM.cdx.json"
    sbom.write_text("{}\n", encoding="utf-8")
    artifacts = []
    for path, role, publish in (
        (wheel, "package", True),
        (sbom, "sbom", False),
    ):
        artifacts.append(
            {
                "filename": path.name,
                "publish": publish,
                "role": role,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
        )
    (release_files / "RELEASE-MANIFEST.json").write_text(
        json.dumps({"artifacts": artifacts}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    published = tmp_path / "published"
    published.mkdir()
    archive = published / "workerbee-wheelhouse.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path in sorted(release_files.iterdir()):
            bundle.add(path, arcname=path.name)
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (published / "SHA256SUMS").write_text(
        f"{archive_sha}  {archive.name}\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "url=''\n"
        "dest=''\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -o) dest="$2"; shift 2 ;;\n'
        "    -*) shift ;;\n"
        '    *) url="$1"; shift ;;\n'
        "  esac\n"
        "done\n"
        'cp "${url#file://}" "$dest"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)
    env = _installer_env(install_dir, bin_dir)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "WORKERBEE_INSTALL_BASE_URL": published.as_uri(),
        }
    )

    proc = subprocess.run(
        ["sh", "scripts/install-workerbee.sh"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode != 0
    assert "refusing package-index fallback" in proc.stderr
    assert (install_dir / "current").readlink() == current_before
    assert (install_dir / "previous").readlink() == previous_before
    assert wrapper.read_text(encoding="utf-8") == "original launcher\n"
    assert sorted(path.name for path in (install_dir / "releases").iterdir()) == [
        "current-release",
        "previous-release",
    ]


def test_wheelhouse_build_is_local_only_and_has_no_index_resolution() -> None:
    text = Path("scripts/build_wheelhouse.sh").read_text(encoding="utf-8")
    assert "--dependency-wheelhouse" in text
    assert "--k1s-core-wheel" in text
    assert "--workerbee-wheel" in text
    assert "WORKERBEE_RELEASE_WHEEL" in text
    assert 'cp "$WORKERBEE_WHEEL" "$OUT_DIR/"' in text
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

    assert runtime_pin == "k1s-core==1.0.0"
    assert release["published-baseline-version"] == "0.1.7"
    assert release["runtime-provider-lane"] == "k1s-core"
    assert release["runtime-provider-requirement"] == runtime_pin
    assert release["runtime-provider-artifact"] == "k1s_core-1.0.0-py3-none-any.whl"
    assert release["runtime-provider-size"] == 1057377
    assert (
        release["runtime-provider-sha256"]
        == "1208552881446eb7b4fdefd27a22ca2f79f70fb851445bebf0a17273a507724f"
    )
    assert release["runtime-provider-publication-status"] == "published"
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
