"""WorkerBee secret policy helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - exercised when PyYAML is unavailable
    yaml = None  # type: ignore[assignment]

from workerbee.contract import WorkerBeeError

WORKERBEE_ALLOW_PLAINTEXT_ENV = "WORKERBEE_ALLOW_PLAINTEXT_SECRETS"
WORKERBEE_SOPS_KEY_ENV = "WORKERBEE_SOPS_AGE_KEY_FILE"
SOPS_AGE_KEY_ENV = "SOPS_AGE_KEY_FILE"
SOPS_BIN_ENV = "AE_SOPS_BIN"


def plaintext_secrets_allowed(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(WORKERBEE_ALLOW_PLAINTEXT_ENV) or "").strip() == "1"


def secret_env_for_project(project_state: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if plaintext_secrets_allowed():
        env["AE_ALLOW_PLAINTEXT_SECRETS"] = "1"
        return env
    key_file = ensure_sops_age_key_file(project_state)
    env[SOPS_AGE_KEY_ENV] = str(key_file)
    return env


def secret_policy_status(project_state: Path) -> dict[str, Any]:
    plaintext = plaintext_secrets_allowed()
    key_file: Path | None = None
    key_error: str | None = None
    if not plaintext:
        try:
            key_file = resolve_sops_age_key_file(project_state, generate=False)
        except WorkerBeeError as exc:
            key_error = exc.message
    return {
        "mode": "plaintext-opt-in" if plaintext else "sops",
        "plaintext_opt_in_env": WORKERBEE_ALLOW_PLAINTEXT_ENV,
        "sops_available": shutil.which(_sops_binary()) is not None,
        "age_keygen_available": shutil.which("age-keygen") is not None,
        "key_file": str(key_file) if key_file else None,
        "key_ready": bool(key_file and key_file.is_file()),
        "key_error": key_error,
    }


def resolve_sops_age_key_file(project_state: Path, *, generate: bool = True) -> Path | None:
    configured = (
        os.getenv(WORKERBEE_SOPS_KEY_ENV)
        or os.getenv(SOPS_AGE_KEY_ENV)
        or ""
    ).strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise WorkerBeeError(
                code="SOPS_REQUIRED",
                message="configured SOPS age key file does not exist",
                details={"key_file": str(path)},
                remediation=(
                    f"Create the key file or unset {WORKERBEE_SOPS_KEY_ENV}/"
                    f"{SOPS_AGE_KEY_ENV} so WorkerBee can generate a project key."
                ),
            )
        return path
    path = project_state.expanduser().resolve() / "secrets" / "age" / "keys.txt"
    if path.is_file():
        return path
    if generate:
        return ensure_sops_age_key_file(project_state)
    return path


def ensure_sops_age_key_file(project_state: Path) -> Path:
    configured = (
        os.getenv(WORKERBEE_SOPS_KEY_ENV)
        or os.getenv(SOPS_AGE_KEY_ENV)
        or ""
    ).strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise WorkerBeeError(
                code="SOPS_REQUIRED",
                message="configured SOPS age key file does not exist",
                details={"key_file": str(path)},
                remediation="Create the configured SOPS age identity file and retry.",
            )
        return path
    path = project_state.expanduser().resolve() / "secrets" / "age" / "keys.txt"
    if path.is_file():
        _chmod_private(path)
        return path
    age_keygen = shutil.which("age-keygen")
    if age_keygen is None:
        raise WorkerBeeError(
            code="SOPS_REQUIRED",
            message="age-keygen is required to create WorkerBee SOPS keys",
            remediation=(
                "Install age, set WORKERBEE_SOPS_AGE_KEY_FILE to an existing age identity, "
                "or set WORKERBEE_ALLOW_PLAINTEXT_SECRETS=1 for an insecure local-only run."
            ),
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [age_keygen, "-o", str(path)],
        check=True,
        text=True,
        capture_output=True,
        timeout=20,
    )
    _chmod_private(path)
    return path


def seal_yaml_mapping(
    path: Path,
    data: dict[str, Any],
    *,
    project_state: Path,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if plaintext_secrets_allowed():
        path.write_text(_dump_mapping(data), encoding="utf-8")
        _chmod_private(path)
        return path
    sops = shutil.which(_sops_binary())
    if sops is None:
        raise WorkerBeeError(
            code="SOPS_REQUIRED",
            message="sops is required to write WorkerBee-managed secret files",
            remediation=(
                "Install sops or set WORKERBEE_ALLOW_PLAINTEXT_SECRETS=1 for an "
                "insecure local-only run."
            ),
        )
    key_file = ensure_sops_age_key_file(project_state)
    recipient = _age_recipient(key_file)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=path.suffix or ".yaml",
            delete=False,
        ) as handle:
            handle.write(_dump_mapping(data))
            tmp_path = Path(handle.name)
        _chmod_private(tmp_path)
        env = os.environ.copy()
        env[SOPS_AGE_KEY_ENV] = str(key_file)
        subprocess.run(
            [sops, "--encrypt", "--age", recipient, "-i", str(tmp_path)],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
            env=env,
        )
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()
    path.chmod(0o600)
    return path


def file_is_sops_encrypted(path: Path) -> bool:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        if yaml is None:
            return "sops:" in raw and "ENC[" in raw
        try:
            data = yaml.safe_load(raw)
        except Exception:
            return "sops:" in raw and "ENC[" in raw
    return isinstance(data, dict) and isinstance(data.get("sops"), dict) and "ENC[" in raw


def _age_recipient(key_file: Path) -> str:
    age_keygen = shutil.which("age-keygen")
    if age_keygen is None:
        raise WorkerBeeError(
            code="SOPS_REQUIRED",
            message="age-keygen is required to derive a SOPS recipient",
            remediation="Install age or set WORKERBEE_ALLOW_PLAINTEXT_SECRETS=1.",
        )
    proc = subprocess.run(
        [age_keygen, "-y", str(key_file)],
        check=True,
        text=True,
        capture_output=True,
        timeout=20,
    )
    recipient = proc.stdout.strip()
    if not recipient.startswith("age1"):
        raise WorkerBeeError(
            code="SOPS_REQUIRED",
            message="failed to derive an age recipient from the configured key file",
            details={"key_file": str(key_file)},
            remediation="Check that the key file is an age identity file.",
        )
    return recipient


def _sops_binary() -> str:
    return os.getenv(SOPS_BIN_ENV, "sops")


def _dump_mapping(data: dict[str, Any]) -> str:
    if yaml is not None:
        return yaml.safe_dump(data, sort_keys=True)
    return "".join(f"{key}: {value}\n" for key, value in sorted(data.items()))


def _chmod_private(path: Path) -> None:
    with suppress(OSError):
        path.chmod(0o600)
