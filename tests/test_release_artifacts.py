from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import stat
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


def _record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"sha256={encoded.decode()}"


def _write_normalizable_wheel(path: Path, *, mode: int, reverse: bool) -> None:
    record_name = "demo-1.0.0.dist-info/RECORD"
    payloads = {
        "demo/__init__.py": b'__version__ = "1.0.0"\n',
        "demo-1.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: demo\nVersion: 1.0.0\n\n"
        ),
        "demo-1.0.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nTag: py3-none-any\nRoot-Is-Purelib: true\n\n"
        ),
    }
    rows = [
        (name, _record_digest(payload), str(len(payload)))
        for name, payload in payloads.items()
    ]
    rows.append((record_name, "", ""))
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    payloads[record_name] = record.getvalue().encode()
    names = list(payloads)
    if reverse:
        names.reverse()
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 2, 3, 4, 4))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, payloads[name])


def _write_workerbee_wheel(path: Path, *, extra_member: tuple[str, str] | None = None) -> None:
    metadata = "\n".join(
        (
            "Metadata-Version: 2.4",
            "Name: k1s-workerbee",
            "Version: 1.0.1",
            "Requires-Dist: k1s-core==1.0.0",
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
        archive.writestr("k1s_workerbee-1.0.1.dist-info/METADATA", metadata)
        archive.writestr(
            "k1s_workerbee-1.0.1.dist-info/entry_points.txt",
            "[console_scripts]\nworkerbee = workerbee.cli:main\n",
        )
        if extra_member is not None:
            archive.writestr(*extra_member)


def _write_runtime_wheel(
    path: Path,
    *,
    distribution: str = "k1s-core",
    version: str = "1.0.0",
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


def test_published_rc_provider_is_publication_ready(capsys) -> None:
    contract = release.load_contract()[0]
    assert contract.version == "1.0.1"
    assert contract.runtime_provider_publication_status == "published"
    assert contract.published_baseline_version == "0.1.7"
    assert release.validate_publication_ready() == []

    assert release.main(["verify-publication-ready"]) == 0
    assert capsys.readouterr().err == ""


def test_clean_wheel_passes_identity_and_hygiene_audit(tmp_path: Path) -> None:
    wheel = tmp_path / "k1s_workerbee-1.0.1-py3-none-any.whl"
    _write_workerbee_wheel(wheel)

    assert release.audit_artifacts([wheel]) == []


def test_exact_runtime_artifact_identity_and_imports_are_enforced(
    tmp_path: Path, monkeypatch
) -> None:
    wheel = tmp_path / "k1s_core-1.0.0-py3-none-any.whl"
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
    workerbee = tmp_path / "k1s_workerbee-1.0.1-py3-none-any.whl"
    runtime = tmp_path / "k1s_core-1.0.0-py3-none-any.whl"
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
    wheel = tmp_path / "k1s_workerbee-1.0.1-py3-none-any.whl"
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


def test_release_manifest_fails_closed_on_incomplete_or_extra_inventory(tmp_path: Path) -> None:
    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"candidate")
    manifest = tmp_path / "RELEASE-MANIFEST.json"
    release.write_manifest(manifest, [artifact])

    extra = tmp_path / "untracked.txt"
    extra.write_text("not inventoried", encoding="utf-8")
    assert release.verify_manifest(manifest, tmp_path) == ["untracked artifact: untracked.txt"]

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["artifacts"].append(dict(payload["artifacts"][0]))
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    errors = release.verify_manifest(manifest, tmp_path)
    assert "manifest contains duplicate artifact filenames" in errors


def test_manifest_can_verify_a_second_candidate_directory(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    artifact = first / "demo.whl"
    artifact.write_bytes(b"identical")
    manifest = first / "RELEASE-MANIFEST.json"
    release.write_manifest(manifest, [artifact])
    (second / artifact.name).write_bytes(artifact.read_bytes())
    (second / manifest.name).write_bytes(manifest.read_bytes())

    assert release.verify_manifest(manifest, second) == []


def test_deterministic_sbom_binds_exact_artifact_bytes(tmp_path: Path) -> None:
    wheel = tmp_path / "k1s_workerbee-1.0.1-py3-none-any.whl"
    _write_workerbee_wheel(wheel)
    first = tmp_path / "first.cdx.json"
    second = tmp_path / "second.cdx.json"

    first_payload = release.write_sbom(first, [wheel])
    second_payload = release.write_sbom(second, [wheel])

    assert first.read_bytes() == second.read_bytes()
    assert first_payload == second_payload
    assert first_payload["bomFormat"] == "CycloneDX"
    assert first_payload["specVersion"] == "1.5"
    component = first_payload["components"][0]
    assert component["name"] == wheel.name
    assert component["version"] == "1.0.1"
    assert component["hashes"] == [{"alg": "SHA-256", "content": release.sha256_file(wheel)}]


def test_index_verification_fetches_only_publishable_manifest_entries(
    tmp_path: Path, monkeypatch
) -> None:
    wheel = tmp_path / "demo.whl"
    wheel.write_bytes(b"wheel")
    sbom = tmp_path / "SBOM.cdx.json"
    sbom.write_text("{}\n", encoding="utf-8")
    manifest = tmp_path / "RELEASE-MANIFEST.json"
    release.write_manifest(manifest, [wheel, sbom])
    index = {
        "urls": [
            {
                "filename": wheel.name,
                "digests": {"sha256": release.sha256_file(wheel)},
                "url": "https://files.invalid/demo.whl",
            }
        ]
    }
    retrieved: list[str] = []
    monkeypatch.setattr(
        release.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps(index).encode()),
    )

    def retrieve(url: str, destination: Path) -> None:
        retrieved.append(url)
        Path(destination).write_bytes(wheel.read_bytes())

    monkeypatch.setattr(release.urllib.request, "urlretrieve", retrieve)

    errors = release.fetch_index_artifacts(
        manifest,
        "https://index.invalid/pypi",
        tmp_path / "download",
    )

    assert errors == []
    assert retrieved == ["https://files.invalid/demo.whl"]
    assert not (tmp_path / "download" / sbom.name).exists()


def test_archive_hygiene_rejects_links_private_generated_and_duplicate_members(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = b"safe"
        normal = tarfile.TarInfo("package/file.txt")
        normal.size = len(payload)
        archive.addfile(normal, io.BytesIO(payload))
        duplicate = tarfile.TarInfo("package/file.txt")
        duplicate.size = len(payload)
        archive.addfile(duplicate, io.BytesIO(payload))
        generated = tarfile.TarInfo("package/dist/generated.txt")
        generated.size = len(payload)
        archive.addfile(generated, io.BytesIO(payload))
        private = tarfile.TarInfo("package/platform-debug/adapter.py")
        private.size = len(payload)
        archive.addfile(private, io.BytesIO(payload))
        symlink = tarfile.TarInfo("package/current")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "file.txt"
        archive.addfile(symlink)
        hardlink = tarfile.TarInfo("package/copy")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "package/file.txt"
        archive.addfile(hardlink)

    findings = release.audit_archive_container(archive_path)
    kinds = {finding.kind for finding in findings}

    assert {
        "duplicate-member",
        "forbidden-path",
        "private-marker",
        "unsafe-member-type",
    } <= kinds
    assert {finding.detail for finding in findings if finding.kind == "unsafe-member-type"} == {
        "hardlink",
        "symlink",
    }


def test_checksum_inventory_rejects_missing_extra_duplicate_and_mismatch(tmp_path: Path) -> None:
    first = tmp_path / "a.whl"
    second = tmp_path / "b.whl"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    sums = tmp_path / "SHA256SUMS"
    release.write_sha256s(sums, [first, second])
    assert release.verify_sha256s(sums, [first, second]) == []

    sums.write_text(
        f"{'0' * 64}  a.whl\n{'1' * 64}  a.whl\n{'2' * 64}  extra.whl\n",
        encoding="utf-8",
    )
    errors = release.verify_sha256s(sums, [first, second])
    assert "duplicate checksum entry: a.whl" in errors
    assert "missing checksum entry: b.whl" in errors
    assert "unexpected checksum entry: extra.whl" in errors
    assert "checksum mismatch: a.whl" in errors


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


def test_wheel_normalization_canonicalizes_modes_order_timestamp_and_record(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.whl"
    second = tmp_path / "second.whl"
    _write_normalizable_wheel(first, mode=0o664, reverse=False)
    _write_normalizable_wheel(second, mode=0o644, reverse=True)

    release.normalize_wheel(first, 1_700_000_001)
    release.normalize_wheel(second, 1_700_000_001)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == sorted(archive.namelist(), key=str.encode)
        assert all(
            stat.S_IMODE(info.external_attr >> 16) == 0o644
            for info in archive.infolist()
        )
        assert all(info.create_system == 3 for info in archive.infolist())
        assert len({info.date_time for info in archive.infolist()}) == 1
        rows = list(
            csv.reader(
                io.StringIO(
                    archive.read("demo-1.0.0.dist-info/RECORD").decode("utf-8")
                )
            )
        )
        assert [row[0] for row in rows[:-1]] == sorted(
            (row[0] for row in rows[:-1]), key=str.encode
        )
        assert rows[-1] == ["demo-1.0.0.dist-info/RECORD", "", ""]


def test_sdist_normalization_canonicalizes_equivalent_source_modes(tmp_path: Path) -> None:
    def write(path: Path, *, regular_mode: int, executable_mode: int) -> None:
        with tarfile.open(path, "w:gz") as archive:
            directory = tarfile.TarInfo("demo-1.0.0/scripts")
            directory.type = tarfile.DIRTYPE
            directory.mode = executable_mode
            archive.addfile(directory)
            for name, payload, mode in (
                ("demo-1.0.0/data.txt", b"data\n", regular_mode),
                ("demo-1.0.0/scripts/run.sh", b"#!/bin/sh\n", executable_mode),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = mode
                archive.addfile(info, io.BytesIO(payload))

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    write(first, regular_mode=0o664, executable_mode=0o775)
    write(second, regular_mode=0o644, executable_mode=0o755)

    release.normalize_sdist(first, 1_700_000_001)
    release.normalize_sdist(second, 1_700_000_001)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        modes = {member.name: member.mode for member in archive.getmembers()}
    assert modes == {
        "demo-1.0.0/data.txt": 0o644,
        "demo-1.0.0/scripts": 0o755,
        "demo-1.0.0/scripts/run.sh": 0o755,
    }


def test_json_audit_output_does_not_contain_secret_value(tmp_path: Path, capsys) -> None:
    wheel = tmp_path / "k1s_workerbee-1.0.1-py3-none-any.whl"
    secret = "pypi-" + "abcdefghijklmnopqrstuvwxyz012345"  # noqa: S105 - synthetic fixture
    _write_workerbee_wheel(wheel, extra_member=("workerbee/leak.txt", secret))

    result = release.main(["audit", "--json", str(wheel)])

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert secret not in json.dumps(payload)
