"""Stable WorkerBee v1 result envelopes and errors."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

API_VERSION = "workerbee.mcp/v1"
MCP_TOOL_NAMES = [
    "workerbee_v1_capabilities",
    "workerbee_v1_session_start",
    "workerbee_v1_projects",
    "workerbee_v1_project_mode_get",
    "workerbee_v1_project_mode_set",
    "workerbee_v1_project_start",
    "workerbee_v1_project_status",
    "workerbee_v1_project_stop",
    "workerbee_v1_project_reset",
    "workerbee_v1_profile_list",
    "workerbee_v1_profile_start",
    "workerbee_v1_profile_status",
    "workerbee_v1_profile_stop",
    "workerbee_v1_profile_validate",
    "workerbee_v1_profile_workload_status",
    "workerbee_v1_profile_workload_validate",
    "workerbee_v1_logs",
    "workerbee_v1_exec",
    "workerbee_v1_ingress_probe",
    "workerbee_v1_image_build",
    "workerbee_v1_manifest_prepare",
    "workerbee_v1_manifest_validate",
    "workerbee_v1_manifest_deploy_local",
    "workerbee_v1_manifest_deploy_remote_k1s",
    "workerbee_v1_bundle_export",
    "workerbee_v1_cleanup",
    "workerbee_v1_trust_status",
    "workerbee_v1_trust_install",
    "workerbee_v1_trust_uninstall",
]


@dataclass(slots=True)
class WorkerBeeError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    remediation: str | None = None

    def __str__(self) -> str:
        return self.message

    def public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": _mask_sensitive(self.details),
            "retryable": self.retryable,
            "remediation": self.remediation,
        }


def ok(
    *,
    kind: str,
    data: dict[str, Any] | None = None,
    project: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "api_version": API_VERSION,
        "kind": kind,
        "ok": True,
        "project": project,
        "data": _jsonable(data or {}),
        "warnings": warnings or [],
        "error": None,
    }


def fail(
    exc: Exception,
    *,
    kind: str,
    project: str | None = None,
) -> dict[str, Any]:
    if isinstance(exc, WorkerBeeError):
        error = exc
    elif isinstance(exc, FileNotFoundError):
        error = WorkerBeeError(
            code="NOT_FOUND",
            message=str(exc),
            remediation="Check the path and retry.",
        )
    elif isinstance(exc, PermissionError):
        error = WorkerBeeError(
            code="PERMISSION_DENIED",
            message=str(exc),
            remediation="Check filesystem permissions and retry.",
        )
    elif isinstance(exc, TimeoutError):
        error = WorkerBeeError(
            code="TIMEOUT",
            message=str(exc),
            retryable=True,
            remediation="Retry after checking runtime and service health.",
        )
    elif isinstance(exc, ValueError):
        error = WorkerBeeError(
            code="VALIDATION_FAILED",
            message=str(exc),
            remediation="Review the input and run validation again.",
        )
    else:
        error = WorkerBeeError(
            code="INTERNAL_ERROR",
            message=str(exc),
            remediation="Inspect WorkerBee logs or rerun with a narrower operation.",
        )
    return {
        "api_version": API_VERSION,
        "kind": kind,
        "ok": False,
        "project": project,
        "data": {},
        "warnings": [],
        "error": error.public_dict(),
    }


def protect(kind: str, project: str | None, fn) -> dict[str, Any]:  # noqa: ANN001
    try:
        data = fn()
        payload = data if isinstance(data, dict) else {"value": data}
        return ok(kind=kind, data=payload, project=project)
    except Exception as exc:  # noqa: BLE001
        return fail(exc, kind=kind, project=project)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    if hasattr(value, "public_dict"):
        return _jsonable(value.public_dict())
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return value


def _mask_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            key_str = str(key)
            if any(marker in key_str.lower() for marker in ("token", "password", "secret", "key")):
                out[key_str] = "***"
            else:
                out[key_str] = _mask_sensitive(val)
        return out
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return value
        if isinstance(parsed, dict):
            return json.dumps(_mask_sensitive(parsed), sort_keys=True)
    return value
