from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path
from types import ModuleType


def _load_release_module() -> ModuleType:
    path = Path("scripts/audit_release_artifacts.py").resolve()
    spec = importlib.util.spec_from_file_location("audit_release_artifacts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release = _load_release_module()


def _write_workerbee_wheel(path: Path, *, extra_member: tuple[str, str] | None = None) -> None:
    metadata = "\n".join(
        (
            "Metadata-Version: 2.4",
            "Name: k1s-workerbee",
            "Version: 0.1.7",
            "Requires-Dist: k1s-core==0.1.8a1",
            "Requires-Dist: mcp>=1.8,<2",
            "",
        )
    )
    with zipfile.ZipFile(path, "w") as archive:
        for member in (
            "workerbee/__init__.py",
            "workerbee/__main__.py",
            "workerbee/cli.py",
            "workerbee/mcp_server.py",
            "workerbee/runtime_provider.py",
        ):
            archive.writestr(member, "")
        archive.writestr("k1s_workerbee-0.1.7.dist-info/METADATA", metadata)
        archive.writestr(
            "k1s_workerbee-0.1.7.dist-info/entry_points.txt",
            "[console_scripts]\nworkerbee = workerbee.cli:main\n",
        )
        if extra_member is not None:
            archive.writestr(*extra_member)


def _write_runtime_wheel(
    path: Path,
    *,
    distribution: str = "k1s-core",
    version: str = "0.1.8a1",
) -> None:
    dist_info = distribution.replace("-", "_")
    metadata = "\n".join(
        (
            "Metadata-Version: 2.4",
            f"Name: {distribution}",
            f"Version: {version}",
            "",
        )
    )
    with zipfile.ZipFile(path, "w") as archive:
        for member in (
            "ae/__init__.py",
            "ae/apishim/__main__.py",
            "ae/cli/__main__.py",
            "ae/controller/__main__.py",
        ):
            archive.writestr(member, "")
        archive.writestr(f"{dist_info}-{version}.dist-info/METADATA", metadata)


def test_project_release_contract_is_consistent() -> None:
    assert release.validate_project() == []


def test_clean_wheel_passes_identity_and_hygiene_audit(tmp_path: Path) -> None:
    wheel = tmp_path / "k1s_workerbee-0.1.7-py3-none-any.whl"
    _write_workerbee_wheel(wheel)

    assert release.audit_artifacts([wheel]) == []


def test_exact_runtime_artifact_identity_and_imports_are_enforced(
    tmp_path: Path, monkeypatch
) -> None:
    wheel = tmp_path / "k1s_core-0.1.8a1-py3-none-any.whl"
    _write_runtime_wheel(wheel)
    contract, project = release.load_contract()
    fixture_contract = replace(
        contract,
        runtime_provider_size=wheel.stat().st_size,
        runtime_provider_sha256=release.sha256_file(wheel),
    )
    monkeypatch.setattr(release, "load_contract", lambda: (fixture_contract, project))

    assert release.verify_runtime_artifact(wheel) == []

    with wheel.open("ab") as stream:
        stream.write(b"changed")
    assert "runtime artifact size mismatch" in "\n".join(release.verify_runtime_artifact(wheel))
    assert "runtime artifact SHA256 mismatch" in release.verify_runtime_artifact(wheel)


def test_wheelhouse_rejects_legacy_or_mixed_ae_providers(tmp_path: Path, monkeypatch) -> None:
    workerbee = tmp_path / "k1s_workerbee-0.1.7-py3-none-any.whl"
    runtime = tmp_path / "k1s_core-0.1.8a1-py3-none-any.whl"
    _write_workerbee_wheel(workerbee)
    _write_runtime_wheel(runtime)
    contract, project = release.load_contract()
    fixture_contract = replace(
        contract,
        runtime_provider_size=runtime.stat().st_size,
        runtime_provider_sha256=release.sha256_file(runtime),
    )
    monkeypatch.setattr(release, "load_contract", lambda: (fixture_contract, project))

    assert release.verify_wheelhouse(tmp_path) == []

    legacy = tmp_path / "k1s_workerbee_runtime-0.1.7-py3-none-any.whl"
    _write_runtime_wheel(legacy, distribution="k1s-workerbee-runtime", version="0.1.7")
    errors = release.verify_wheelhouse(tmp_path)
    assert any("legacy runtime provider is forbidden" in error for error in errors)
    assert any("exactly one ae provider" in error for error in errors)


def test_wheel_secret_pattern_fails_audit(tmp_path: Path) -> None:
    wheel = tmp_path / "k1s_workerbee-0.1.7-py3-none-any.whl"
    _write_workerbee_wheel(
        wheel,
        extra_member=("workerbee/leak.txt", "pypi-" + "abcdefghijklmnopqrstuvwxyz012345"),
    )

    findings = release.audit_artifacts([wheel])

    assert any(item.kind == "secret-pattern" and item.detail == "pypi-token" for item in findings)


def test_release_manifest_detects_byte_changes(tmp_path: Path) -> None:
    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"candidate-a")
    manifest = tmp_path / "RELEASE-MANIFEST.json"
    payload = release.write_manifest(manifest, [artifact])

    assert payload["distribution"] == "k1s-workerbee"
    assert release.verify_manifest(manifest, tmp_path) == []

    artifact.write_bytes(b"candidate-b")
    errors = release.verify_manifest(manifest, tmp_path)
    assert "size mismatch: demo.whl" not in errors
    assert errors == ["sha256 mismatch: demo.whl"]


def test_sha256s_are_sorted_by_filename(tmp_path: Path) -> None:
    second = tmp_path / "b.whl"
    first = tmp_path / "a.whl"
    second.write_bytes(b"b")
    first.write_bytes(b"a")
    output = tmp_path / "SHA256SUMS"

    release.write_sha256s(output, [second, first])

    names = [line.split()[1] for line in output.read_text(encoding="utf-8").splitlines()]
    assert names == ["a.whl", "b.whl"]


def test_sdist_normalization_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    payload = tmp_path / "payload.txt"
    payload.write_text("release bytes\n", encoding="utf-8")
    with tarfile.open(first, "w:gz") as archive:
        archive.add(payload, arcname="demo-0.1.0/payload.txt")
    payload.touch()
    with tarfile.open(second, "w:gz") as archive:
        archive.add(payload, arcname="demo-0.1.0/payload.txt")

    release.normalize_sdist(first, 1_700_000_000)
    release.normalize_sdist(second, 1_700_000_000)

    assert first.read_bytes() == second.read_bytes()


def test_json_audit_output_does_not_contain_secret_value(tmp_path: Path, capsys) -> None:
    wheel = tmp_path / "k1s_workerbee-0.1.7-py3-none-any.whl"
    secret = "pypi-" + "abcdefghijklmnopqrstuvwxyz012345"  # noqa: S105 - synthetic fixture
    _write_workerbee_wheel(wheel, extra_member=("workerbee/leak.txt", secret))

    result = release.main(["audit", "--json", str(wheel)])

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert secret not in json.dumps(payload)
