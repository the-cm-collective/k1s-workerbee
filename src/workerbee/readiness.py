"""Bounded, public-neutral readiness evidence receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError
from workerbee.paths import daemon_project_state_dir

READINESS_EVIDENCE_SCHEMA = "workerbee.readiness-evidence/v1"
READINESS_RECEIPT_SCHEMA = "workerbee.readiness-receipt/v1"
MAX_EVIDENCE_BYTES = 65_536
MAX_RECEIPT_BYTES = 65_536
MAX_SETTLE_SECONDS = 10.0
MAX_EVIDENCE_AGE_SECONDS = 86_400.0
_NAME = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
_SENSITIVE = re.compile(r"(?i)(secret|token|password|credential|api[_-]?key|private[_-]?key)")


def readiness_evidence_path(*, project: str, evidence_name: str, state_root: Path) -> Path:
    if not _NAME.fullmatch(evidence_name):
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_NAME_INVALID",
            message="Readiness evidence name must be a bounded safe identifier",
        )
    return (
        daemon_project_state_dir(project, state_root=state_root)
        / "artifacts"
        / "readiness"
        / f"{evidence_name}.json"
    )


def read_readiness_receipt(
    *,
    path: Path,
    project: str,
    generation: str,
    evidence_name: str,
    required_consumers: Mapping[str, Sequence[str]] | None = None,
    configured_runtime: str | None = None,
    max_age_seconds: float = 300.0,
    settle_timeout_seconds: float = 1.0,
    settle_poll_seconds: float = 0.05,
    now: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Read one owner-private evidence file and return a bounded receipt."""

    if not generation or len(generation) > 256:
        raise WorkerBeeError(
            code="READINESS_GENERATION_INVALID",
            message="A bounded expected generation is required",
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise WorkerBeeError(
            code="READINESS_NOFOLLOW_UNAVAILABLE",
            message="This platform cannot enforce O_NOFOLLOW readiness reads",
        )
    max_age = _bounded_number(max_age_seconds, "max_age_seconds", 0.0, MAX_EVIDENCE_AGE_SECONDS)
    settle_timeout = _bounded_number(
        settle_timeout_seconds, "settle_timeout_seconds", 0.01, MAX_SETTLE_SECONDS
    )
    settle_poll = _bounded_number(
        settle_poll_seconds, "settle_poll_seconds", 0.001, min(1.0, settle_timeout)
    )
    settled = _wait_until_settled(
        path,
        timeout=settle_timeout,
        interval=settle_poll,
        monotonic=monotonic,
        sleep=sleep,
    )
    _validate_stat(settled, path)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    deadline = monotonic() + min(1.0, settle_timeout)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_OPEN_FAILED",
            message="Readiness evidence could not be opened safely",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        _validate_stat(opened, path)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            settled.st_dev,
            settled.st_ino,
            settled.st_size,
            settled.st_mtime_ns,
        ):
            raise WorkerBeeError(
                code="READINESS_EVIDENCE_CHANGED",
                message="Readiness evidence changed while it was opened",
                retryable=True,
            )
        raw = _bounded_read(descriptor, deadline=deadline, monotonic=monotonic)
    finally:
        os.close(descriptor)

    try:
        evidence = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_INVALID",
            message="Readiness evidence is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(evidence, dict):
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_INVALID",
            message="Readiness evidence must be a JSON object",
        )
    _validate_evidence(evidence, generation=generation, max_age=max_age, now=now())
    dependency_gate = _dependency_gate(evidence, required_consumers or {})
    sanitized_evidence = _sanitize(evidence)
    runtime_state = _runtime_state(evidence, configured_runtime)
    evidence_state = evidence.get("state")
    ready = evidence_state == "ready" and dependency_gate["ready"]
    receipt: dict[str, Any] = {
        "schema": READINESS_RECEIPT_SCHEMA,
        "project": project,
        "generation": generation,
        "evidence_name": evidence_name,
        "state": evidence_state,
        "ready": ready,
        "runtime_state": runtime_state,
        "dependency_gate": dependency_gate,
        "evidence": sanitized_evidence,
        "provenance": {
            "source": "workerbee-managed project readiness evidence",
            "path_class": "project-state/artifacts/readiness",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "owner_uid": opened.st_uid,
            "mode": f"{stat.S_IMODE(opened.st_mode):04o}",
            "nofollow": True,
            "settled": True,
            "observed_at": now(),
        },
    }
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    if len(canonical) > MAX_RECEIPT_BYTES:
        raise WorkerBeeError(
            code="READINESS_RECEIPT_TOO_LARGE",
            message="Readiness receipt exceeds the public size limit",
        )
    receipt["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
    return receipt


def _wait_until_settled(
    path: Path,
    *,
    timeout: float,
    interval: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> os.stat_result:
    deadline = monotonic() + timeout
    previous: tuple[int, int, int, int] | None = None
    while True:
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise WorkerBeeError(
                code="READINESS_EVIDENCE_MISSING",
                message="Readiness evidence is unavailable",
                retryable=True,
            ) from exc
        identity = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        if identity == previous:
            return current
        previous = identity
        if monotonic() >= deadline:
            raise WorkerBeeError(
                code="READINESS_EVIDENCE_UNSETTLED",
                message="Readiness evidence did not settle before the bounded timeout",
                retryable=True,
            )
        sleep(interval)


def _validate_stat(value: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_TYPE_INVALID",
            message=f"Readiness evidence is not a regular file: {path.name}",
        )
    if value.st_uid != os.getuid():
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_OWNER_INVALID",
            message="Readiness evidence is not owned by the WorkerBee user",
        )
    if stat.S_IMODE(value.st_mode) & 0o077:
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_MODE_INVALID",
            message="Readiness evidence must be owner-only",
        )
    if value.st_size > MAX_EVIDENCE_BYTES:
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_TOO_LARGE",
            message="Readiness evidence exceeds the public size limit",
        )


def _bounded_read(descriptor: int, *, deadline: float, monotonic: Callable[[], float]) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        if monotonic() > deadline:
            raise WorkerBeeError(
                code="READINESS_EVIDENCE_TIMEOUT",
                message="Readiness evidence read exceeded its bounded deadline",
                retryable=True,
            )
        chunk = os.read(descriptor, min(8192, MAX_EVIDENCE_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_EVIDENCE_BYTES:
            raise WorkerBeeError(
                code="READINESS_EVIDENCE_TOO_LARGE",
                message="Readiness evidence exceeds the public size limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_evidence(
    evidence: Mapping[str, Any], *, generation: str, max_age: float, now: float
) -> None:
    if evidence.get("api_version") != READINESS_EVIDENCE_SCHEMA:
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_SCHEMA_INVALID",
            message="Readiness evidence schema is unsupported",
        )
    if evidence.get("generation") != generation:
        raise WorkerBeeError(
            code="READINESS_GENERATION_STALE",
            message="Readiness evidence belongs to a stale generation",
        )
    if evidence.get("state") not in {"ready", "not_ready", "unknown"}:
        raise WorkerBeeError(
            code="READINESS_STATE_INVALID", message="Readiness state is unsupported"
        )
    observed_at = evidence.get("observed_at")
    if not isinstance(observed_at, (int, float)):
        raise WorkerBeeError(
            code="READINESS_TIMESTAMP_INVALID", message="Readiness timestamp is required"
        )
    age = now - float(observed_at)
    if age < -60.0 or age > max_age:
        raise WorkerBeeError(
            code="READINESS_EVIDENCE_STALE",
            message="Readiness evidence is outside the accepted age window",
        )


def _dependency_gate(
    evidence: Mapping[str, Any], required: Mapping[str, Sequence[str]]
) -> dict[str, Any]:
    dependencies = evidence.get("dependencies")
    by_name: dict[str, Mapping[str, Any]] = {}
    if isinstance(dependencies, list):
        for item in dependencies:
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                by_name[item["name"]] = item
    checks: list[dict[str, Any]] = []
    for dependency_name in sorted(required):
        item = by_name.get(dependency_name)
        consumers: dict[str, str] = {}
        if item is not None and isinstance(item.get("consumers"), list):
            for consumer in item["consumers"]:
                if isinstance(consumer, Mapping) and isinstance(consumer.get("name"), str):
                    consumers[consumer["name"]] = str(consumer.get("state", "unknown"))
        required_names = sorted(set(required[dependency_name]))
        missing = [name for name in required_names if name not in consumers]
        unready = [name for name in required_names if consumers.get(name) != "ready"]
        dependency_ready = bool(item and item.get("state") == "ready")
        checks.append(
            {
                "dependency": dependency_name,
                "dependency_ready": dependency_ready,
                "required_consumers": required_names,
                "missing_consumers": missing,
                "unready_consumers": unready,
                "ready": dependency_ready and not missing and not unready,
            }
        )
    return {"ready": all(item["ready"] for item in checks), "checks": checks}


def _runtime_state(evidence: Mapping[str, Any], configured: str | None) -> dict[str, Any]:
    runtime = evidence.get("runtime")
    measured = runtime.get("measured") if isinstance(runtime, Mapping) else None
    configured_value = runtime.get("configured") if isinstance(runtime, Mapping) else None
    if configured_value is None:
        configured_value = configured
    return {
        "configured": {
            "state": "configured" if configured_value else "unknown",
            "value": configured_value,
        },
        "measured": {"state": "measured" if measured else "unknown", "value": measured},
    }


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[depth-limit]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value, key=lambda item: str(item))[:128]:
            name = str(key)[:128]
            result[name] = (
                "[redacted]" if _SENSITIVE.search(name) else _sanitize(value[key], depth=depth + 1)
            )
        return result
    if isinstance(value, list):
        return [_sanitize(item, depth=depth + 1) for item in value[:128]]
    if isinstance(value, str):
        return value[:2048]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2048]


def _bounded_number(value: float, name: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise WorkerBeeError(
            code="READINESS_LIMIT_INVALID", message=f"{name} must be numeric"
        ) from exc
    if not minimum <= number <= maximum:
        raise WorkerBeeError(
            code="READINESS_LIMIT_INVALID",
            message=f"{name} must be between {minimum} and {maximum}",
        )
    return number
