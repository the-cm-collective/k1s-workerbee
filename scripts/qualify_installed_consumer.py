#!/usr/bin/env python3
"""Qualify the installed WorkerBee/K1s consumer contract without source fallback.

The lane is deliberately non-live.  It creates fresh virtual environments,
installs only from a caller-supplied sealed wheelhouse, and records a canonical
receipt after proving that imports and distribution metadata resolve inside the
new environment rather than either source checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

RECEIPT_SCHEMA = "workerbee.installed-consumer-qualification/v1"
AGGREGATE_SCHEMA = "workerbee.installed-consumer-qualification-set/v1"
WORKERBEE_VERSION = "1.0.0"
K1S_VERSION = "1.0.0"
WORKERBEE_WHEEL_NAME = "k1s_workerbee-1.0.0-py3-none-any.whl"
K1S_WHEEL_NAME = "k1s_core-1.0.0-py3-none-any.whl"
K1S_WHEEL_SIZE = 1_057_577
K1S_WHEEL_SHA256 = "1208552881446eb7b4fdefd27a22ca2f79f70fb851445bebf0a17273a507724f"
MAX_WHEEL_BYTES = 128 * 1024 * 1024
MAX_STDOUT_BYTES = 256 * 1024
MAX_STDERR_BYTES = 16 * 1024
MAX_RECEIPT_BYTES = 128 * 1024
COMMAND_TIMEOUT_SECONDS = 60.0
INSTALL_TIMEOUT_SECONDS = 240.0
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class QualificationError(RuntimeError):
    """Fail-closed qualification error with a stable public reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _fail(reason: str) -> NoReturn:
    raise QualificationError(reason)


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    filename: str
    size: int
    sha256: str
    source_commit: str
    source_tree: str

    def public(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size": self.size,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
        }


@dataclass(frozen=True, slots=True)
class WheelhouseIdentity:
    wheel_count: int
    complete_set_sha256: str

    def public(self) -> dict[str, object]:
        return {
            "complete_set_sha256": self.complete_set_sha256,
            "wheel_count": self.wheel_count,
        }


@dataclass(frozen=True, slots=True)
class QualificationInputs:
    python: Path
    wheelhouse: Path
    workerbee_wheel: Path
    k1s_wheel: Path
    workerbee: ArtifactIdentity
    k1s: ArtifactIdentity
    wheelhouse_identity: WheelhouseIdentity
    source_roots: tuple[Path, Path]
    run_root: Path
    runs: int


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: bytes
    stderr: bytes


CommandRunner = Callable[..., CommandResult]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_WHEEL_BYTES:
                _fail("artifact-size-limit")
            digest.update(chunk)
    return digest.hexdigest()


def _validate_digest(value: str, reason: str) -> str:
    lowered = value.lower()
    if not _DIGEST.fullmatch(lowered):
        _fail(reason)
    return lowered


def _validate_object_id(value: str, reason: str) -> str:
    lowered = value.lower()
    if not _OBJECT_ID.fullmatch(lowered):
        _fail(reason)
    return lowered


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_artifact(
    path: Path,
    *,
    wheelhouse: Path,
    expected_name: str,
    expected_size: int,
    expected_sha256: str,
    source_commit: str,
    source_tree: str,
) -> ArtifactIdentity:
    if path.is_symlink() or not path.is_file():
        _fail("artifact-not-regular")
    resolved = path.resolve(strict=True)
    if resolved.parent != wheelhouse:
        _fail("artifact-outside-wheelhouse")
    if resolved.name != expected_name:
        _fail("artifact-filename-mismatch")
    if resolved.stat().st_size != expected_size:
        _fail("artifact-size-mismatch")
    actual = _sha256_file(resolved)
    if actual != _validate_digest(expected_sha256, "artifact-digest-invalid"):
        _fail("artifact-sha256-mismatch")
    return ArtifactIdentity(
        filename=resolved.name,
        size=expected_size,
        sha256=actual,
        source_commit=_validate_object_id(source_commit, "source-commit-invalid"),
        source_tree=_validate_object_id(source_tree, "source-tree-invalid"),
    )


def _wheelhouse_identity(wheelhouse: Path) -> WheelhouseIdentity:
    entries: list[dict[str, object]] = []
    for path in sorted(wheelhouse.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
            _fail("wheelhouse-has-non-wheel-entry")
        entries.append(
            {
                "filename": path.name,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    names = [str(entry["filename"]) for entry in entries]
    if not entries or len(names) != len(set(names)):
        _fail("wheelhouse-empty-or-duplicate")
    if sum(name.startswith("k1s_core-") for name in names) != 1:
        _fail("wheelhouse-k1s-provider-count")
    if sum(name.startswith("k1s_workerbee-") for name in names) != 1:
        _fail("wheelhouse-workerbee-count")
    if any(name.startswith("k1s_workerbee_runtime-") for name in names):
        _fail("wheelhouse-legacy-provider")
    return WheelhouseIdentity(
        wheel_count=len(entries),
        complete_set_sha256=_sha256_bytes(_canonical_bytes(entries)),
    )


def _prepare_run_root(path: Path, source_roots: Sequence[Path]) -> Path:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        _fail("run-root-must-be-new-absolute-path")
    resolved_parent = path.parent.resolve(strict=True)
    resolved = resolved_parent / path.name
    for source in source_roots:
        if _is_within(resolved, source) or _is_within(source, resolved):
            _fail("run-root-overlaps-source")
    resolved.mkdir(mode=0o700)
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode != 0o700 or resolved.stat().st_uid != os.getuid():
        _fail("run-root-not-owner-private")
    return resolved


def _clean_environment(venv: Path, home: Path, temporary: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join((str(venv / "bin"), "/usr/bin", "/bin")),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(temporary),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
    }


def _run_bounded(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    label: str,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> CommandResult:
    try:
        completed = subprocess.run(
            tuple(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise QualificationError(f"{label}-timeout") from exc
    if len(completed.stdout) > MAX_STDOUT_BYTES or len(completed.stderr) > MAX_STDERR_BYTES:
        _fail(f"{label}-output-limit")
    if completed.returncode != 0:
        _fail(f"{label}-failed")
    return CommandResult(completed.stdout, completed.stderr)


_INSPECT_SCRIPT = r"""
import hashlib
import importlib.metadata as metadata
import json
import pathlib
import sys

import ae
import workerbee
from workerbee.k1s_consumer_contract import k1s_v1_consumer_contract
from workerbee.profile_evidence import PUBLIC_OPERATIONS

venv = pathlib.Path(sys.argv[1]).resolve()
source_roots = tuple(pathlib.Path(value).resolve() for value in sys.argv[2:])

def within(path, root):
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True

def installed_path(path):
    resolved = pathlib.Path(path).resolve()
    if not within(resolved, venv):
        raise SystemExit("installed-origin-outside-venv")
    if any(within(resolved, root) for root in source_roots):
        raise SystemExit("installed-origin-in-source-checkout")
    return "$VENV/" + resolved.relative_to(venv).as_posix()

for entry in sys.path:
    if not entry:
        continue
    resolved = pathlib.Path(entry).resolve()
    if any(within(resolved, root) for root in source_roots):
        raise SystemExit("source-checkout-on-sys-path")

workerbee_dist = metadata.distribution("k1s-workerbee")
k1s_dist = metadata.distribution("k1s-core")
forbidden = [dist.metadata["Name"] for dist in metadata.distributions()
             if dist.metadata.get("Name", "").lower().replace("_", "-") == "k1s-workerbee-runtime"]
if forbidden:
    raise SystemExit("legacy-provider-installed")
owners = sorted(metadata.packages_distributions().get("ae", []))
if owners != ["k1s-core"]:
    raise SystemExit("unexpected-ae-provider")
requirements = sorted(metadata.requires("k1s-workerbee") or [])
if "k1s-core==1.0.0" not in requirements:
    raise SystemExit("inexact-provider-requirement")

contract = k1s_v1_consumer_contract()
if contract["provider_requirement"] != "k1s-core==1.0.0":
    raise SystemExit("consumer-provider-mismatch")
if contract["workerbee_profile_evidence"]["public_operations"] != list(PUBLIC_OPERATIONS):
    raise SystemExit("consumer-operation-mismatch")

def entrypoints(dist):
    return {
        entry.name: entry.value
        for entry in dist.entry_points
        if entry.group == "console_scripts"
    }

payload = {
    "contract_schema": contract["schema"],
    "contract_sha256": hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest(),
    "distributions": {
        "k1s-core": {
            "location": installed_path(k1s_dist.locate_file("")),
            "version": k1s_dist.version,
        },
        "k1s-workerbee": {
            "location": installed_path(workerbee_dist.locate_file("")),
            "version": workerbee_dist.version,
        },
    },
    "entrypoints": {
        "ae": entrypoints(k1s_dist).get("ae"),
        "k1s": entrypoints(k1s_dist).get("k1s"),
        "workerbee": entrypoints(workerbee_dist).get("workerbee"),
    },
    "imports": {
        "ae": installed_path(ae.__file__),
        "workerbee": installed_path(workerbee.__file__),
    },
    "provider_owners": owners,
    "public_operations": list(PUBLIC_OPERATIONS),
    "requirements": requirements,
    "source_checkouts_on_sys_path": False,
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
"""


def _json_output(result: CommandResult, label: str) -> Any:
    try:
        return json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"{label}-invalid-json") from exc


def _command_summary(payload: Any, *, operation: str, workerbee: bool) -> dict[str, object]:
    if not isinstance(payload, dict):
        _fail(f"{operation}-invalid-payload")
    if workerbee:
        evidence = payload.get("evidence")
        diagnostics = payload.get("diagnostics")
        if not isinstance(evidence, dict) or not isinstance(diagnostics, dict):
            _fail(f"workerbee-{operation}-invalid-envelope")
        if evidence.get("operation") != operation:
            _fail(f"workerbee-{operation}-operation-mismatch")
        if diagnostics.get("provider_distribution") != "k1s-core":
            _fail(f"workerbee-{operation}-provider-mismatch")
        if diagnostics.get("provider_version") != K1S_VERSION:
            _fail(f"workerbee-{operation}-version-mismatch")
        if diagnostics.get("same_runtime") is not True:
            _fail(f"workerbee-{operation}-runtime-mismatch")
        return {
            "kind": evidence.get("kind"),
            "operation": evidence.get("operation"),
            "provider_distribution": diagnostics.get("provider_distribution"),
            "provider_version": diagnostics.get("provider_version"),
            "same_runtime": diagnostics.get("same_runtime"),
            "sha256": _sha256_bytes(_canonical_bytes(payload)),
        }
    if payload.get("mutating") is not False:
        _fail(f"k1s-{operation}-not-read-only")
    return {
        "apiVersion": payload.get("apiVersion"),
        "kind": payload.get("kind"),
        "mutating": payload.get("mutating"),
        "sha256": _sha256_bytes(_canonical_bytes(payload)),
    }


def _qualify_one(
    inputs: QualificationInputs,
    *,
    run_number: int,
    runner: CommandRunner = _run_bounded,
) -> dict[str, object]:
    run_dir = inputs.run_root / f"run-{run_number}"
    run_dir.mkdir(mode=0o700)
    home = run_dir / "home"
    temporary = run_dir / "tmp"
    scratch = run_dir / "scratch"
    venv = run_dir / "venv"
    for path in (home, temporary, scratch):
        path.mkdir(parents=True, mode=0o700)

    bootstrap_env = _clean_environment(run_dir / "empty-path", home, temporary)
    runner(
        (str(inputs.python), "-I", "-m", "venv", "--copies", str(venv)),
        env=bootstrap_env,
        cwd=scratch,
        label="venv-create",
        timeout=INSTALL_TIMEOUT_SECONDS,
    )
    python = venv / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        _fail("venv-python-missing")
    env = _clean_environment(venv, home, temporary)

    runner(
        (
            str(python),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(inputs.wheelhouse),
            "--only-binary=:all:",
            "--no-compile",
            "--disable-pip-version-check",
            f"k1s-workerbee=={WORKERBEE_VERSION}",
        ),
        env=env,
        cwd=scratch,
        label="offline-install",
        timeout=INSTALL_TIMEOUT_SECONDS,
    )
    pip_check = runner(
        (str(python), "-I", "-m", "pip", "check"),
        env=env,
        cwd=scratch,
        label="pip-check",
    )
    inspection = _json_output(
        runner(
            (
                str(python),
                "-I",
                "-c",
                _INSPECT_SCRIPT,
                str(venv),
                *(str(root) for root in inputs.source_roots),
            ),
            env=env,
            cwd=scratch,
            label="installed-inspection",
        ),
        "installed-inspection",
    )

    commands: dict[str, object] = {}
    simple_commands = {
        "ae-version": (venv / "bin" / "ae", "version"),
        "workerbee-help": (venv / "bin" / "workerbee", "--help"),
        "workerbee-k1s-evidence-help": (
            venv / "bin" / "workerbee",
            "k1s-evidence",
            "--help",
        ),
        "workerbee-version": (venv / "bin" / "workerbee", "--version"),
    }
    for label, command in simple_commands.items():
        result = runner(tuple(str(item) for item in command), env=env, cwd=scratch, label=label)
        commands[label] = {
            "sha256": _sha256_bytes(result.stdout),
            "stdout": result.stdout.decode("utf-8", errors="strict").strip()
            if label in {"ae-version", "workerbee-version"}
            else None,
        }

    operations = (("list", None), ("doctor", "core"), ("plan", "core"))
    for operation, profile in operations:
        profile_args = () if profile is None else (profile,)
        direct = _json_output(
            runner(
                (
                    str(venv / "bin" / "k1s"),
                    "profile",
                    operation,
                    *profile_args,
                    "--output",
                    "json",
                ),
                env=env,
                cwd=scratch,
                label=f"k1s-{operation}",
            ),
            f"k1s-{operation}",
        )
        consumer = _json_output(
            runner(
                (
                    str(venv / "bin" / "workerbee"),
                    "--json",
                    "k1s-evidence",
                    operation,
                    *profile_args,
                ),
                env=env,
                cwd=scratch,
                label=f"workerbee-{operation}",
            ),
            f"workerbee-{operation}",
        )
        commands[f"k1s-{operation}"] = _command_summary(
            direct, operation=operation, workerbee=False
        )
        commands[f"workerbee-{operation}"] = _command_summary(
            consumer, operation=operation, workerbee=True
        )

    receipt: dict[str, object] = {
        "artifacts": {
            "k1s-core": inputs.k1s.public(),
            "k1s-workerbee": inputs.workerbee.public(),
        },
        "commands": commands,
        "environment": {
            "cwd": "$RUN/scratch",
            "fresh_venv": True,
            "owner_private_run_root": True,
            "pip_check_sha256": _sha256_bytes(pip_check.stdout),
            "pip_config_file": os.devnull,
            "pip_no_index": True,
            "python_no_user_site": True,
            "wheelhouse": inputs.wheelhouse_identity.public(),
        },
        "installed": inspection,
        "live_operations": False,
        "schema": RECEIPT_SCHEMA,
        "source_isolation": {
            "checkout_path_sha256": sorted(
                _sha256_bytes(str(root).encode("utf-8")) for root in inputs.source_roots
            ),
            "source_checkouts_on_sys_path": False,
        },
        "status": "qualified",
    }
    encoded = _canonical_bytes(receipt)
    if len(encoded) > MAX_RECEIPT_BYTES:
        _fail("receipt-size-limit")
    (run_dir / "receipt.json").write_bytes(encoded + b"\n")
    return receipt


def _aggregate(receipts: Sequence[Mapping[str, object]]) -> dict[str, object]:
    if len(receipts) < 2:
        _fail("at-least-two-runs-required")
    digests = [_sha256_bytes(_canonical_bytes(receipt)) for receipt in receipts]
    if len(set(digests)) != 1:
        _fail("qualification-receipts-differ")
    return {
        "deterministic": True,
        "receipt_sha256": digests[0],
        "run_receipts": [f"run-{index}/receipt.json" for index in range(1, len(receipts) + 1)],
        "runs": len(receipts),
        "schema": AGGREGATE_SCHEMA,
        "status": "qualified",
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--workerbee-wheel", type=Path, required=True)
    parser.add_argument("--workerbee-size", type=int, required=True)
    parser.add_argument("--workerbee-sha256", required=True)
    parser.add_argument("--workerbee-source-commit", required=True)
    parser.add_argument("--workerbee-source-tree", required=True)
    parser.add_argument("--k1s-wheel", type=Path, required=True)
    parser.add_argument("--k1s-size", type=int, default=K1S_WHEEL_SIZE)
    parser.add_argument("--k1s-sha256", default=K1S_WHEEL_SHA256)
    parser.add_argument("--k1s-source-commit", required=True)
    parser.add_argument("--k1s-source-tree", required=True)
    parser.add_argument("--workerbee-source-root", type=Path, required=True)
    parser.add_argument("--k1s-source-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=2, choices=range(2, 5), metavar="{2,3,4}")
    return parser.parse_args(argv)


def _inputs_from_args(args: argparse.Namespace) -> QualificationInputs:
    python = args.python.resolve(strict=True)
    if not python.is_file() or not os.access(python, os.X_OK):
        _fail("python-not-executable")
    wheelhouse = args.wheelhouse.resolve(strict=True)
    if args.wheelhouse.is_symlink() or not wheelhouse.is_dir():
        _fail("wheelhouse-not-directory")
    source_roots = (
        args.workerbee_source_root.resolve(strict=True),
        args.k1s_source_root.resolve(strict=True),
    )
    workerbee = _validate_artifact(
        args.workerbee_wheel,
        wheelhouse=wheelhouse,
        expected_name=WORKERBEE_WHEEL_NAME,
        expected_size=args.workerbee_size,
        expected_sha256=args.workerbee_sha256,
        source_commit=args.workerbee_source_commit,
        source_tree=args.workerbee_source_tree,
    )
    if args.k1s_size != K1S_WHEEL_SIZE or args.k1s_sha256.lower() != K1S_WHEEL_SHA256:
        _fail("k1s-authority-mismatch")
    k1s = _validate_artifact(
        args.k1s_wheel,
        wheelhouse=wheelhouse,
        expected_name=K1S_WHEEL_NAME,
        expected_size=K1S_WHEEL_SIZE,
        expected_sha256=K1S_WHEEL_SHA256,
        source_commit=args.k1s_source_commit,
        source_tree=args.k1s_source_tree,
    )
    wheelhouse_identity = _wheelhouse_identity(wheelhouse)
    run_root = _prepare_run_root(args.run_root, source_roots)
    return QualificationInputs(
        python=python,
        wheelhouse=wheelhouse,
        workerbee_wheel=args.workerbee_wheel.resolve(),
        k1s_wheel=args.k1s_wheel.resolve(),
        workerbee=workerbee,
        k1s=k1s,
        wheelhouse_identity=wheelhouse_identity,
        source_roots=source_roots,
        run_root=run_root,
        runs=args.runs,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        inputs = _inputs_from_args(_parse_args(argv))
        receipts = [_qualify_one(inputs, run_number=index) for index in range(1, inputs.runs + 1)]
        aggregate = _aggregate(receipts)
        encoded = _canonical_bytes(aggregate)
        (inputs.run_root / "QUALIFICATION-RECEIPT.json").write_bytes(encoded + b"\n")
        print(encoded.decode("utf-8"))
        return 0
    except QualificationError as exc:
        print(f"installed consumer qualification failed: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
