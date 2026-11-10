#!/usr/bin/env python3
"""Validate WorkerBee release contracts, archives, and exact-byte manifests."""

from __future__ import annotations

import argparse
import configparser
import email
import gzip
import hashlib
import json
import re
import stat
import sys
import tarfile
import tomllib
import urllib.request
import uuid
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
INIT_MODULE = ROOT / "src/workerbee/__init__.py"
RUNTIME_MODULE = ROOT / "src/workerbee/runtime_provider.py"

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("pypi-token", re.compile(rb"pypi-[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(rb"gh[oprsu]_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)
FORBIDDEN_PATH_PARTS = {
    ".git",
    ".gitea",
    ".github",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".workerbee",
    "__pycache__",
    "build",
    "dist",
    "generated",
    "htmlcov",
    "node_modules",
    "state",
    "temp",
    "tmp",
}
PRIVATE_PATH_PATTERN = re.compile(
    r"(?i)(?:^|[-_.])(?:private|internal)(?:[-_.]|$)|platform[-_]debug"
)


@dataclass(frozen=True, slots=True)
class ReleaseContract:
    distribution: str
    version: str
    published_baseline_version: str
    import_package: str
    cli_entry_point: str
    mcp_entry_point: str
    runtime_import: str
    runtime_provider_lane: str
    runtime_provider_requirement: str
    runtime_provider_artifact: str
    runtime_provider_size: int
    runtime_provider_sha256: str
    runtime_provider_publication_status: str
    forbidden_runtime_provider: str


@dataclass(frozen=True, slots=True)
class Finding:
    artifact: str
    member: str
    kind: str
    detail: str


def normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        raise ValueError(f"invalid requirement: {requirement!r}")
    return normalize_distribution(match.group(1))


def load_contract() -> tuple[ReleaseContract, dict[str, Any]]:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = pyproject["project"]
    release = pyproject["tool"]["workerbee"]["release"]
    contract = ReleaseContract(
        distribution=release["distribution"],
        version=project["version"],
        published_baseline_version=release["published-baseline-version"],
        import_package=release["import-package"],
        cli_entry_point=release["cli-entry-point"],
        mcp_entry_point=release["mcp-entry-point"],
        runtime_import=release["runtime-import"],
        runtime_provider_lane=release["runtime-provider-lane"],
        runtime_provider_requirement=release["runtime-provider-requirement"],
        runtime_provider_artifact=release["runtime-provider-artifact"],
        runtime_provider_size=release["runtime-provider-size"],
        runtime_provider_sha256=release["runtime-provider-sha256"],
        runtime_provider_publication_status=release["runtime-provider-publication-status"],
        forbidden_runtime_provider=release["forbidden-runtime-provider"],
    )
    return contract, pyproject


def validate_project() -> list[str]:
    contract, pyproject = load_contract()
    project = pyproject["project"]
    errors: list[str] = []
    if project["name"] != contract.distribution:
        errors.append("project.name does not match the release distribution")
    if contract.published_baseline_version != "0.1.7":
        errors.append("the published baseline version receipt must remain 0.1.7")
    if contract.import_package != "workerbee":
        errors.append("the import package must remain workerbee")
    if contract.runtime_import != "ae":
        errors.append("the compatibility runtime import must remain ae")
    if contract.runtime_provider_lane != "k1s-core":
        errors.append("the runtime provider lane must be the accepted k1s-core lane")
    if project.get("scripts", {}).get("workerbee") != contract.cli_entry_point:
        errors.append("the workerbee console script does not match the release contract")
    dependencies = tuple(project.get("dependencies", ()))
    if "mcp>=1.8,<2" not in dependencies:
        errors.append("the current MCP API lane must remain constrained to mcp>=1.8,<2")
    provider_name = requirement_name(contract.runtime_provider_requirement)
    provider_dependencies = tuple(
        dependency
        for dependency in dependencies
        if requirement_name(dependency).startswith("k1s")
        or "workerbee-runtime" in requirement_name(dependency)
    )
    if provider_dependencies != (contract.runtime_provider_requirement,):
        errors.append(
            "runtime dependency exclusivity failed: expected only "
            f"{contract.runtime_provider_requirement!r}, found {provider_dependencies!r}"
        )
    if requirement_name(contract.runtime_provider_requirement) != provider_name:
        errors.append("runtime provider requirement is malformed")
    if provider_name != "k1s-core":
        errors.append("the accepted runtime provider distribution must be k1s-core")
    if requirement_name(contract.forbidden_runtime_provider) != "k1s-workerbee-runtime":
        errors.append("the forbidden legacy provider contract is malformed")
    if any(
        requirement_name(dependency) == requirement_name(contract.forbidden_runtime_provider)
        for dependency in dependencies
    ):
        errors.append("the legacy runtime provider must not be a project dependency")
    if not re.fullmatch(r"[0-9a-f]{64}", contract.runtime_provider_sha256):
        errors.append("the runtime provider SHA256 must be a lowercase hex digest")
    if contract.runtime_provider_size <= 0:
        errors.append("the runtime provider artifact size must be positive")
    if contract.runtime_provider_publication_status not in {
        "temporary-nonpublished",
        "published",
    }:
        errors.append("the runtime provider publication status is invalid")
    init_text = INIT_MODULE.read_text(encoding="utf-8")
    if f'__version__ = "{contract.version}"' not in init_text:
        errors.append("workerbee.__version__ does not match project.version")
    runtime_text = RUNTIME_MODULE.read_text(encoding="utf-8")
    provider_version = contract.runtime_provider_requirement.split("==", 1)[-1]
    expected_runtime_constants = (
        f'RUNTIME_DISTRIBUTION = "{provider_name}"',
        f'RUNTIME_VERSION = "{provider_version}"',
        f'RUNTIME_ARTIFACT_FILENAME = "{contract.runtime_provider_artifact}"',
        f"RUNTIME_ARTIFACT_SIZE = {contract.runtime_provider_size}",
        f'RUNTIME_ARTIFACT_SHA256 = "{contract.runtime_provider_sha256}"',
        f'LEGACY_RUNTIME_DISTRIBUTION = "{contract.forbidden_runtime_provider}"',
    )
    if any(item not in runtime_text for item in expected_runtime_constants):
        errors.append("runtime provider module does not match the accepted artifact contract")
    if pyproject["build-system"].get("build-backend") != "setuptools.build_meta":
        errors.append("unexpected build backend")
    return errors


def validate_publication_ready() -> list[str]:
    """Fail closed until the provider and WorkerBee version can be published."""

    contract, _ = load_contract()
    errors = validate_project()
    if contract.runtime_provider_publication_status != "published":
        errors.append("runtime provider is temporary-nonpublished; refusing WorkerBee publication")
    if contract.version == contract.published_baseline_version:
        errors.append(
            f"WorkerBee {contract.version} is already the published baseline; "
            "refusing same-version publication"
        )
    return errors


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(path: str) -> bool:
    member = PurePosixPath(path)
    return not member.is_absolute() and ".." not in member.parts


def _iter_archive(path: Path) -> Iterator[tuple[str, bytes]]:
    for name, kind, blob in _archive_entries(path):
        if kind == "file" and blob is not None:
            yield name, blob


def _archive_entries(path: Path) -> Iterator[tuple[str, str, bytes | None]]:
    """Yield member name, normalized kind, and regular-file bytes."""

    if path.suffix in {".whl", ".zip"}:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                mode = info.external_attr >> 16
                if info.is_dir():
                    yield info.filename, "directory", None
                elif mode and stat.S_ISLNK(mode):
                    yield info.filename, "symlink", None
                else:
                    yield info.filename, "file", archive.read(info)
        return
    if path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            for info in archive.getmembers():
                if info.isdir():
                    yield info.name, "directory", None
                elif info.isfile():
                    stream = archive.extractfile(info)
                    yield info.name, "file", stream.read() if stream is not None else b""
                elif info.issym():
                    yield info.name, "symlink", None
                elif info.islnk():
                    yield info.name, "hardlink", None
                else:
                    yield info.name, "special", None
        return
    yield path.name, "file", path.read_bytes()


def _archive_names(path: Path) -> tuple[str, ...]:
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            return tuple(info.filename for info in archive.infolist() if not info.is_dir())
    if path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            return tuple(info.name for info in archive.getmembers() if info.isfile())
    return (path.name,)


def normalize_sdist(path: Path, epoch: int) -> None:
    """Rewrite a setuptools sdist with stable tar and gzip metadata."""

    temporary = path.with_name(f".{path.name}.normalized")
    with (
        tarfile.open(path, "r:gz") as source,
        temporary.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target,
    ):
        for member in sorted(source.getmembers(), key=lambda item: item.name):
            if not _safe_member(member.name):
                raise ValueError(f"unsafe sdist member: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported sdist member type: {member.name}")
            member.mtime = epoch
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.pax_headers = {}
            stream = source.extractfile(member) if member.isfile() else None
            target.addfile(member, stream)
    temporary.replace(path)


def _metadata_from_wheel(path: Path) -> email.message.Message:
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError(f"{path}: expected one wheel METADATA, found {metadata_names!r}")
        return email.message_from_bytes(archive.read(metadata_names[0]))


def _entry_points_from_wheel(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    with zipfile.ZipFile(path) as archive:
        names = [
            name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(names) != 1:
            raise ValueError(f"{path}: expected one entry_points.txt, found {names!r}")
        parser.read_string(archive.read(names[0]).decode("utf-8"))
    return parser


def _wheel_has_import(path: Path, import_package: str) -> bool:
    prefix = f"{import_package}/"
    with zipfile.ZipFile(path) as archive:
        return any(name.startswith(prefix) for name in archive.namelist())


def verify_runtime_artifact(path: Path) -> list[str]:
    """Verify the exact accepted runtime wheel identity and compatibility imports."""

    contract, _ = load_contract()
    errors: list[str] = []
    if path.name != contract.runtime_provider_artifact:
        errors.append(
            "runtime artifact filename mismatch: "
            f"expected {contract.runtime_provider_artifact}, found {path.name}"
        )
    if not path.is_file():
        return [*errors, f"runtime artifact is not a file: {path}"]
    if path.stat().st_size != contract.runtime_provider_size:
        errors.append(
            "runtime artifact size mismatch: "
            f"expected {contract.runtime_provider_size}, found {path.stat().st_size}"
        )
    if sha256_file(path) != contract.runtime_provider_sha256:
        errors.append("runtime artifact SHA256 mismatch")
    try:
        metadata = _metadata_from_wheel(path)
        names = set(_archive_names(path))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return [*errors, f"runtime artifact is not a valid wheel: {exc}"]
    expected_version = contract.runtime_provider_requirement.split("==", 1)[-1]
    if normalize_distribution(metadata.get("Name", "")) != requirement_name(
        contract.runtime_provider_requirement
    ):
        errors.append("runtime artifact distribution mismatch")
    if metadata.get("Version") != expected_version:
        errors.append("runtime artifact version mismatch")
    required_imports = {
        "ae/__init__.py",
        "ae/apishim/__main__.py",
        "ae/cli/__main__.py",
        "ae/controller/__main__.py",
    }
    for member in sorted(required_imports - names):
        errors.append(f"runtime artifact missing compatibility import: {member}")
    return errors


def audit_artifacts(paths: Sequence[Path]) -> list[Finding]:
    contract, _ = load_contract()
    findings: list[Finding] = []
    for path in paths:
        if not path.is_file():
            findings.append(Finding(str(path), "", "missing", "artifact does not exist"))
            continue
        findings.extend(audit_archive_container(path))
        for member, blob in _iter_archive(path):
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(blob):
                    findings.append(Finding(str(path), member, "secret-pattern", name))
        if path.suffix == ".whl":
            findings.extend(_audit_workerbee_wheel(path, contract))
        elif path.name.endswith(".tar.gz"):
            findings.extend(_audit_workerbee_sdist(path, contract))
        else:
            findings.append(Finding(str(path), path.name, "unsupported", "release type"))
    return findings


def audit_archive_container(path: Path) -> list[Finding]:
    """Reject unsafe or non-public archive topology before content inspection."""

    findings: list[Finding] = []
    seen: set[str] = set()
    for member, kind, _blob in _archive_entries(path):
        parts = PurePosixPath(member).parts
        if member in seen:
            findings.append(Finding(str(path), member, "duplicate-member", "duplicate path"))
        seen.add(member)
        if not _safe_member(member):
            findings.append(Finding(str(path), member, "unsafe-path", "path traversal"))
        if kind not in {"file", "directory"}:
            findings.append(Finding(str(path), member, "unsafe-member-type", kind))
        if any(part in FORBIDDEN_PATH_PARTS or part.startswith(".env") for part in parts):
            findings.append(Finding(str(path), member, "forbidden-path", "local/generated state"))
        if any(PRIVATE_PATH_PATTERN.search(part) for part in parts):
            findings.append(Finding(str(path), member, "private-marker", "non-public path marker"))
    return findings


def _audit_workerbee_wheel(path: Path, contract: ReleaseContract) -> list[Finding]:
    findings: list[Finding] = []
    metadata = _metadata_from_wheel(path)
    if normalize_distribution(metadata.get("Name", "")) != normalize_distribution(
        contract.distribution
    ):
        findings.append(Finding(str(path), "METADATA", "identity", "distribution name"))
    if metadata.get("Version") != contract.version:
        findings.append(Finding(str(path), "METADATA", "identity", "version"))
    requires = tuple(metadata.get_all("Requires-Dist", ()))
    mcp_requires = tuple(
        requirement.replace(" ", "")
        for requirement in requires
        if requirement_name(requirement) == "mcp"
    )
    if len(mcp_requires) != 1 or ">=1.8" not in mcp_requires[0] or "<2" not in mcp_requires[0]:
        findings.append(Finding(str(path), "METADATA", "mcp-api-lane", repr(mcp_requires)))
    providers = tuple(
        requirement
        for requirement in requires
        if requirement_name(requirement).startswith("k1s")
        or "workerbee-runtime" in requirement_name(requirement)
    )
    if providers != (contract.runtime_provider_requirement,):
        findings.append(Finding(str(path), "METADATA", "runtime-providers", repr(providers)))
    entry_points = _entry_points_from_wheel(path)
    actual_entry = entry_points.get("console_scripts", "workerbee", fallback="")
    if actual_entry != contract.cli_entry_point:
        findings.append(Finding(str(path), "entry_points.txt", "entry-point", actual_entry))
    names = set(_archive_names(path))
    for required in (
        f"{contract.import_package}/__init__.py",
        f"{contract.import_package}/__main__.py",
        f"{contract.import_package}/cli.py",
        f"{contract.import_package}/mcp_server.py",
        f"{contract.import_package}/runtime_provider.py",
    ):
        if required not in names:
            findings.append(Finding(str(path), required, "missing-member", "wheel contract"))
    if any(name.startswith(("tests/", "scripts/", ".github/")) for name in names):
        findings.append(Finding(str(path), "", "wheel-hygiene", "non-package files"))
    return findings


def _audit_workerbee_sdist(path: Path, contract: ReleaseContract) -> list[Finding]:
    findings: list[Finding] = []
    names = _archive_names(path)
    roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    if len(roots) != 1:
        return [Finding(str(path), "", "sdist-root", repr(sorted(roots)))]
    root = next(iter(roots))
    required = {
        f"{root}/LICENSE",
        f"{root}/README.md",
        f"{root}/docs/v1-contract.md",
        f"{root}/pyproject.toml",
        f"{root}/scripts/audit_release_artifacts.py",
        f"{root}/scripts/build_release_candidate.sh",
        f"{root}/scripts/build_wheelhouse.sh",
        f"{root}/scripts/install-workerbee.sh",
        f"{root}/src/{contract.import_package}/__init__.py",
        f"{root}/src/{contract.import_package}/cli.py",
        f"{root}/src/{contract.import_package}/mcp_server.py",
        f"{root}/tests/test_release_artifacts.py",
    }
    for member in sorted(required - set(names)):
        findings.append(Finding(str(path), member, "missing-member", "sdist contract"))
    if any(f"/{prefix}" in name for name in names for prefix in (".github/", "dist/", "build/")):
        findings.append(Finding(str(path), "", "sdist-hygiene", "generated/workflow files"))
    return findings


def verify_wheelhouse(wheelhouse: Path) -> list[str]:
    contract, _ = load_contract()
    wheels = sorted(wheelhouse.glob("*.whl"))
    errors: list[str] = []
    workerbee = [
        path
        for path in wheels
        if normalize_distribution(_metadata_from_wheel(path).get("Name", ""))
        == normalize_distribution(contract.distribution)
    ]
    provider_name = requirement_name(contract.runtime_provider_requirement)
    provider = [
        path
        for path in wheels
        if normalize_distribution(_metadata_from_wheel(path).get("Name", "")) == provider_name
    ]
    forbidden = [
        path
        for path in wheels
        if normalize_distribution(_metadata_from_wheel(path).get("Name", ""))
        == normalize_distribution(contract.forbidden_runtime_provider)
    ]
    ae_providers = [path for path in wheels if _wheel_has_import(path, contract.runtime_import)]
    if len(workerbee) != 1:
        errors.append(f"expected one {contract.distribution} wheel, found {workerbee!r}")
    if len(provider) != 1:
        errors.append(f"expected one accepted runtime wheel, found {provider!r}")
    if forbidden:
        errors.append(f"legacy runtime provider is forbidden: {forbidden!r}")
    if ae_providers != provider:
        errors.append(
            "wheelhouse must contain exactly one ae provider and it must be the accepted "
            f"k1s-core wheel; providers={ae_providers!r}"
        )
    if provider:
        errors.extend(verify_runtime_artifact(provider[0]))
    if workerbee:
        findings = _audit_workerbee_wheel(workerbee[0], contract)
        errors.extend(f"{item.kind}: {item.detail}" for item in findings)
    return errors


def write_manifest(output: Path, paths: Sequence[Path]) -> dict[str, Any]:
    contract, _ = load_contract()
    output_resolved = output.resolve()
    artifacts = []
    names: set[str] = set()
    for path in sorted((item.resolve() for item in paths), key=lambda item: item.name):
        if path == output_resolved:
            raise ValueError("manifest cannot include itself")
        if path.name in names:
            raise ValueError(f"manifest artifact filename is duplicated: {path.name}")
        names.add(path.name)
        artifacts.append(
            {
                "filename": path.name,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "role": (
                    "sbom"
                    if path.name == "SBOM.cdx.json"
                    else "package"
                    if path.suffix == ".whl" or path.name.endswith(".tar.gz")
                    else "support"
                ),
                "publish": path.suffix == ".whl" or path.name.endswith(".tar.gz"),
            }
        )
    payload = {
        "schema": "k1s-workerbee.release-manifest/v1",
        "distribution": contract.distribution,
        "version": contract.version,
        "artifacts": artifacts,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_sha256s(output: Path, paths: Sequence[Path]) -> None:
    output_resolved = output.resolve()
    resolved = sorted((path.resolve() for path in paths), key=lambda path: path.name)
    if output_resolved in resolved:
        raise ValueError("checksum file cannot include itself")
    lines = [f"{sha256_file(path)}  {path.name}" for path in resolved]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_sha256s(output: Path, paths: Sequence[Path]) -> list[str]:
    expected = {
        path.resolve().name: sha256_file(path.resolve())
        for path in paths
        if path.resolve() != output.resolve()
    }
    actual: dict[str, str] = {}
    errors: list[str] = []
    for line in output.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
            errors.append("invalid SHA256SUMS entry")
            continue
        name = fields[1].lstrip("*")
        if name in actual:
            errors.append(f"duplicate checksum entry: {name}")
        actual[name] = fields[0]
    for name in sorted(set(expected) - set(actual)):
        errors.append(f"missing checksum entry: {name}")
    for name in sorted(set(actual) - set(expected)):
        errors.append(f"unexpected checksum entry: {name}")
    for name in sorted(set(expected) & set(actual)):
        if actual[name] != expected[name]:
            errors.append(f"checksum mismatch: {name}")
    return errors


def write_sbom(output: Path, paths: Sequence[Path]) -> dict[str, Any]:
    """Write a deterministic CycloneDX inventory for exact local artifacts."""

    contract, _ = load_contract()
    components: list[dict[str, Any]] = []
    for path in sorted((item.resolve() for item in paths), key=lambda item: item.name):
        digest = sha256_file(path)
        component: dict[str, Any] = {
            "type": "file",
            "name": path.name,
            "bom-ref": f"artifact:sha256:{digest}",
            "hashes": [{"alg": "SHA-256", "content": digest}],
            "properties": [
                {"name": "workerbee:artifact:size", "value": str(path.stat().st_size)},
                {
                    "name": "workerbee:artifact:publishable",
                    "value": str(path.suffix == ".whl" or path.name.endswith(".tar.gz")).lower(),
                },
            ],
        }
        if path.suffix == ".whl":
            metadata = _metadata_from_wheel(path)
            component["type"] = "library"
            component["group"] = normalize_distribution(metadata.get("Name", ""))
            component["version"] = metadata.get("Version", "")
            component["properties"].extend(
                {"name": "workerbee:requires-dist", "value": requirement}
                for requirement in sorted(metadata.get_all("Requires-Dist", ()))
            )
        components.append(component)
    seed = json.dumps(components, sort_keys=True, separators=(",", ":")).encode()
    serial = uuid.UUID(hashlib.sha256(seed).hexdigest()[:32])
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": contract.distribution,
                "version": contract.version,
            },
            "properties": [
                {"name": "workerbee:generator", "value": "audit_release_artifacts.py"},
                {"name": "workerbee:network", "value": "not-used"},
            ],
        },
        "components": components,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def verify_manifest(manifest_path: Path, directory: Path) -> list[str]:
    contract, _ = load_contract()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if payload.get("schema") != "k1s-workerbee.release-manifest/v1":
        errors.append("unexpected release manifest schema")
    if payload.get("distribution") != contract.distribution:
        errors.append("manifest distribution does not match the project")
    if payload.get("version") != contract.version:
        errors.append("manifest version does not match the project")
    entries = payload.get("artifacts", [])
    if not isinstance(entries, list):
        return [*errors, "manifest artifacts must be a list"]
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("manifest artifact entry must be an object")
            continue
        filename = entry.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            errors.append("manifest artifact filename is unsafe")
            continue
        names.append(filename)
        if entry.get("role") not in {"package", "sbom", "support"}:
            errors.append(f"invalid artifact role: {filename}")
        if not isinstance(entry.get("publish"), bool):
            errors.append(f"invalid publish flag: {filename}")
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            errors.append(f"invalid artifact size: {filename}")
            continue
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", entry["sha256"]
        ):
            errors.append(f"invalid artifact digest: {filename}")
            continue
        path = directory / filename
        if not path.is_file():
            errors.append(f"missing artifact: {path}")
            continue
        if path.stat().st_size != entry["size"]:
            errors.append(f"size mismatch: {path.name}")
        if sha256_file(path) != entry["sha256"]:
            errors.append(f"sha256 mismatch: {path.name}")
    if len(names) != len(set(names)):
        errors.append("manifest contains duplicate artifact filenames")
    directory_manifest = (directory / manifest_path.name).resolve()
    actual = {path.name for path in directory.iterdir() if path.resolve() != directory_manifest}
    for name in sorted(actual - set(names)):
        errors.append(f"untracked artifact: {name}")
    for name in sorted(set(names) - actual):
        if f"missing artifact: {directory / name}" not in errors:
            errors.append(f"missing artifact: {directory / name}")
    return errors


def fetch_index_artifacts(
    manifest_path: Path,
    repository_json_base: str,
    output: Path,
) -> list[str]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    distribution = payload["distribution"]
    version = payload["version"]
    endpoint = f"{repository_json_base.rstrip('/')}/{distribution}/{version}/json"
    with urllib.request.urlopen(endpoint, timeout=30) as response:  # noqa: S310
        index_payload = json.load(response)
    # The version-specific Warehouse JSON endpoint exposes release files under
    # ``urls``. Keep the project-level ``releases`` shape as a compatibility
    # fallback for mirrors that return it from a version endpoint.
    release_files = index_payload.get("urls") or index_payload.get("releases", {}).get(
        version, []
    )
    by_name = {item["filename"]: item for item in release_files}
    output.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for entry in payload["artifacts"]:
        if not entry.get("publish", True):
            continue
        filename = entry["filename"]
        item = by_name.get(filename)
        if item is None:
            errors.append(f"published artifact missing: {filename}")
            continue
        if item.get("digests", {}).get("sha256") != entry["sha256"]:
            errors.append(f"published index digest mismatch: {filename}")
            continue
        destination = output / filename
        urllib.request.urlretrieve(item["url"], destination)  # noqa: S310
        if sha256_file(destination) != entry["sha256"]:
            errors.append(f"downloaded byte mismatch: {filename}")
    return errors


def _print_errors(errors: Iterable[str]) -> int:
    errors = tuple(errors)
    if not errors:
        print("release contract verified")
        return 0
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify-project")
    subparsers.add_parser("verify-publication-ready")
    audit = subparsers.add_parser("audit")
    audit.add_argument("artifacts", nargs="+", type=Path)
    audit.add_argument("--json", action="store_true")
    wheelhouse = subparsers.add_parser("verify-wheelhouse")
    wheelhouse.add_argument("--wheelhouse", required=True, type=Path)
    runtime = subparsers.add_parser("verify-runtime-artifact")
    runtime.add_argument("--artifact", required=True, type=Path)
    manifest = subparsers.add_parser("write-manifest")
    manifest.add_argument("--output", required=True, type=Path)
    manifest.add_argument("artifacts", nargs="+", type=Path)
    checksums = subparsers.add_parser("write-sha256s")
    checksums.add_argument("--output", required=True, type=Path)
    checksums.add_argument("artifacts", nargs="+", type=Path)
    verify_checksums = subparsers.add_parser("verify-sha256s")
    verify_checksums.add_argument("--checksums", required=True, type=Path)
    verify_checksums.add_argument("artifacts", nargs="+", type=Path)
    sbom = subparsers.add_parser("write-sbom")
    sbom.add_argument("--output", required=True, type=Path)
    sbom.add_argument("artifacts", nargs="+", type=Path)
    archive = subparsers.add_parser("audit-archive")
    archive.add_argument("artifact", type=Path)
    normalize = subparsers.add_parser("normalize-sdist")
    normalize.add_argument("--epoch", required=True, type=int)
    normalize.add_argument("sdist", type=Path)
    verify = subparsers.add_parser("verify-manifest")
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--directory", required=True, type=Path)
    index = subparsers.add_parser("fetch-index")
    index.add_argument("--manifest", required=True, type=Path)
    index.add_argument("--repository-json-base", required=True)
    index.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "verify-project":
        return _print_errors(validate_project())
    if args.command == "verify-publication-ready":
        return _print_errors(validate_publication_ready())
    if args.command == "audit":
        findings = audit_artifacts(args.artifacts)
        if args.json:
            print(
                json.dumps(
                    {"ok": not findings, "findings": [asdict(item) for item in findings]},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for finding in findings:
                print(
                    f"{finding.artifact}: {finding.member}: {finding.kind}: {finding.detail}",
                    file=sys.stderr,
                )
            if not findings:
                print("release artifact audit passed")
        return 1 if findings else 0
    if args.command == "verify-wheelhouse":
        return _print_errors(verify_wheelhouse(args.wheelhouse))
    if args.command == "verify-runtime-artifact":
        return _print_errors(verify_runtime_artifact(args.artifact))
    if args.command == "write-manifest":
        write_manifest(args.output, args.artifacts)
        print(args.output)
        return 0
    if args.command == "write-sha256s":
        write_sha256s(args.output, args.artifacts)
        print(args.output)
        return 0
    if args.command == "verify-sha256s":
        return _print_errors(verify_sha256s(args.checksums, args.artifacts))
    if args.command == "write-sbom":
        write_sbom(args.output, args.artifacts)
        print(args.output)
        return 0
    if args.command == "audit-archive":
        findings = audit_archive_container(args.artifact)
        for finding in findings:
            print(
                f"{finding.artifact}: {finding.member}: {finding.kind}: {finding.detail}",
                file=sys.stderr,
            )
        if not findings:
            print("archive hygiene audit passed")
        return 1 if findings else 0
    if args.command == "normalize-sdist":
        normalize_sdist(args.sdist, args.epoch)
        print(args.sdist)
        return 0
    if args.command == "verify-manifest":
        return _print_errors(verify_manifest(args.manifest, args.directory))
    if args.command == "fetch-index":
        return _print_errors(
            fetch_index_artifacts(
                args.manifest,
                args.repository_json_base,
                args.output,
            )
        )
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
