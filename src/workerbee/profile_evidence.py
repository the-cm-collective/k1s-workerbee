"""Bounded public consumer for K1s profile-adapter evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast

from workerbee.contract import WorkerBeeError

ADAPTER_API_VERSION = "k1s.dev/workerbee-profile-adapter/v1"
EVIDENCE_API_VERSION = "k1s.dev/workerbee-profile-evidence/v1"
QUALIFICATION_POSTURE = "source-validated/unqualified"
CONSUMER_SCHEMA = "workerbee.k1s-profile-evidence-consumer/v1"
CAPABILITY_SCHEMA = "workerbee.k1s-profile-evidence-capabilities/v1"
QUALIFICATION_RECEIPT_SCHEMA = "workerbee.k1s-profile-evidence-qualification/v1"
MCP_TOOL_NAME = "workerbee_v1_k1s_profile_evidence"

MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
MAX_STDERR_BYTES = 16 * 1024
MAX_NESTING = 8
SUBPROCESS_TIMEOUT_SECONDS = 20.0

PublicOperation = Literal["list", "doctor", "plan", "status", "logs"]
AdapterOperation = Literal["list", "doctor", "plan", "status", "logs", "bootstrap"]

PUBLIC_OPERATIONS: tuple[PublicOperation, ...] = ("list", "doctor", "plan", "status", "logs")
ADAPTER_OPERATIONS: tuple[AdapterOperation, ...] = (*PUBLIC_OPERATIONS, "bootstrap")
PORTABLE_PROFILES = ("local", "core", "edge-gateway", "node")
PROFILE_NAMES = (*PORTABLE_PROFILES, "edge-dev", "core-ha")
PROFILE_COMPONENTS: dict[str, tuple[str, ...]] = {
    "local": ("controller", "node-local"),
    "core": ("controller",),
    "edge-gateway": ("gateway",),
    "node": ("node-agent",),
}

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!-]{0,63}\Z")
_EVIDENCE_KEYS = {
    "apiVersion",
    "evidenceId",
    "kind",
    "nodeIdentity",
    "operation",
    "payload",
    "provenance",
    "qualificationPosture",
    "schemaVersion",
}
_PROVENANCE_KEYS = {
    "adapterApiVersion",
    "distribution",
    "distributionVersion",
    "sourceApiVersion",
    "sourceExitCode",
    "sourceKind",
    "sourceMode",
    "sourceSchemaVersion",
}
_NODE_IDENTITY_KEYS = {
    "generation",
    "generationProvenance",
    "identityMode",
    "identityVersion",
    "nodeId",
}
_PAYLOAD_KEYS: dict[AdapterOperation, set[str]] = {
    "list": {"profiles"},
    "doctor": {
        "alias",
        "canonicalName",
        "checks",
        "compatibilityVariant",
        "contractPosture",
        "executionStatus",
        "productionEligible",
        "qualificationPosture",
        "requestedName",
        "status",
    },
    "plan": {
        "alias",
        "bootstrapRequired",
        "canonicalName",
        "compatibilityVariant",
        "components",
        "contractPosture",
        "executionStatus",
        "productionEligible",
        "qualificationPosture",
        "requestedName",
        "requirements",
        "supportedBackends",
        "warningCodes",
    },
    "status": {
        "backend",
        "lifecycleStatus",
        "phase",
        "profile",
        "profileGeneration",
        "qualificationPosture",
        "runRef",
    },
    "logs": {
        "component",
        "contentPolicy",
        "lineCount",
        "profile",
        "runRef",
        "sourceTruncated",
    },
    "bootstrap": {"backendId", "bootstrapId", "connections", "profile", "siteId"},
}
_SOURCE_CONTRACT: dict[AdapterOperation, tuple[str | None, str, str, int | None]] = {
    "list": ("k1s.dev/v1alpha1", "ProfileCatalog", "profile-list-json", 1),
    "doctor": (
        "k1s.dev/v1alpha1",
        "ProfileDoctorReport",
        "profile-doctor-catalog-json",
        1,
    ),
    "plan": ("k1s.dev/v1alpha1", "ProfilePlan", "profile-plan-json", 1),
    "status": (None, "PortableLifecycleResult", "profile-status-portable-json", None),
    "logs": ("k1s.dev/v1alpha1", "ProfileLogSnapshot", "profile-logs-owned-json", None),
    "bootstrap": (None, "ProfileBootstrapPublicProjection", "profile-bootstrap-create-json", None),
}
_FORBIDDEN_KEYS = {
    "credential",
    "credentialDigest",
    "createdAt",
    "engineCapability",
    "environment",
    "executable",
    "fd",
    "host",
    "hostPlanDigest",
    "hostReceiptDigest",
    "lines",
    "logDigest",
    "message",
    "ownerUid",
    "path",
    "pid",
    "privateBootstrapDigest",
    "raw",
    "runId",
    "secret",
    "session",
    "socket",
    "stderr",
    "stdout",
    "token",
    "url",
}


@dataclass(frozen=True, slots=True)
class OperationSpec:
    name: AdapterOperation
    public: bool
    profile_required: bool
    component_required: bool


OPERATION_TABLE: dict[AdapterOperation, OperationSpec] = {
    "list": OperationSpec("list", True, False, False),
    "doctor": OperationSpec("doctor", True, True, False),
    "plan": OperationSpec("plan", True, True, False),
    "status": OperationSpec("status", True, True, False),
    "logs": OperationSpec("logs", True, True, True),
    "bootstrap": OperationSpec("bootstrap", False, False, False),
}


class ProfileEvidenceError(WorkerBeeError):
    """Stable, redacted profile-evidence failure."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(
            code="K1S_PROFILE_EVIDENCE_FAILED_CLOSED",
            message="K1s profile evidence request failed closed",
            details={"reason": reason},
            retryable=retryable,
            remediation="Verify the exact installed K1s provider and request contract.",
        )


def _fail(reason: str, *, retryable: bool = False) -> NoReturn:
    raise ProfileEvidenceError(reason, retryable=retryable)


def validate_public_request(
    operation: str,
    *,
    profile: str | None = None,
    component: str | None = None,
) -> tuple[PublicOperation, str | None, str | None]:
    """Validate the fixed public operation table without accepting paths or commands."""

    if operation not in PUBLIC_OPERATIONS:
        _fail("unsupported-operation")
    typed_operation = cast(PublicOperation, operation)
    spec = OPERATION_TABLE[typed_operation]
    if spec.profile_required:
        if profile not in (PORTABLE_PROFILES if operation in {"status", "logs"} else PROFILE_NAMES):
            _fail("invalid-profile")
    elif profile is not None:
        _fail("unexpected-profile")
    if spec.component_required:
        if profile is None or component not in PROFILE_COMPONENTS.get(profile, ()):
            _fail("invalid-component")
    elif component is not None:
        _fail("unexpected-component")
    return typed_operation, profile, component


def consume_projected_evidence(value: bytes | str | dict[str, Any]) -> dict[str, Any]:
    """Verify the bounded metadata-safe envelope returned by the selected K1s adapter."""

    raw: bytes | None = None
    if isinstance(value, str):
        raw = value.encode("utf-8")
        evidence = _load_json(raw)
    elif isinstance(value, bytes):
        raw = value
        evidence = _load_json(raw)
    elif isinstance(value, dict):
        evidence = dict(value)
    else:
        _fail("unsupported-schema")
    _bounded_tree(evidence)
    _exact_keys(evidence, _EVIDENCE_KEYS)
    if (
        evidence["apiVersion"] != EVIDENCE_API_VERSION
        or evidence["kind"] != "WorkerBeeProfileEvidence"
        or evidence["qualificationPosture"] != QUALIFICATION_POSTURE
        or evidence["schemaVersion"] != 1
    ):
        _fail("unsupported-schema")
    operation = evidence["operation"]
    if operation not in ADAPTER_OPERATIONS:
        _fail("unsupported-operation")
    typed_operation = cast(AdapterOperation, operation)
    provenance = _mapping(evidence["provenance"])
    _exact_keys(provenance, _PROVENANCE_KEYS)
    source_api, source_kind, source_mode, source_schema = _SOURCE_CONTRACT[typed_operation]
    if (
        provenance["adapterApiVersion"] != ADAPTER_API_VERSION
        or provenance["distribution"] != "k1s-core"
        or not isinstance(provenance["distributionVersion"], str)
        or _VERSION.fullmatch(provenance["distributionVersion"]) is None
        or provenance["sourceApiVersion"] != source_api
        or provenance["sourceKind"] != source_kind
        or provenance["sourceMode"] != source_mode
        or provenance["sourceSchemaVersion"] != source_schema
        or type(provenance["sourceExitCode"]) is not int
        or provenance["sourceExitCode"] not in ((0, 1) if operation == "doctor" else (0,))
    ):
        _fail("unsupported-schema")
    payload = _mapping(evidence["payload"])
    _exact_keys(payload, _PAYLOAD_KEYS[typed_operation])
    _validate_operation_safety(typed_operation, payload, evidence["nodeIdentity"])
    evidence_id = evidence["evidenceId"]
    if not isinstance(evidence_id, str) or _DIGEST.fullmatch(evidence_id) is None:
        _fail("unsupported-schema")
    unsigned = {key: item for key, item in evidence.items() if key != "evidenceId"}
    expected_id = _evidence_id(unsigned)
    if evidence_id != expected_id:
        _fail("digest-mismatch")
    canonical = _canonical_json(evidence)
    if len(canonical) > MAX_OUTPUT_BYTES:
        _fail("size-limit")
    if raw is not None and raw != canonical:
        _fail("noncanonical-evidence")
    return evidence


def profile_evidence_capabilities(
    *,
    ready: bool,
    provider_version: str | None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Return stable capability readback without executable or filesystem disclosure."""

    return {
        "schema": CAPABILITY_SCHEMA,
        "version": 1,
        "adapter_api_version": ADAPTER_API_VERSION,
        "evidence_api_version": EVIDENCE_API_VERSION,
        "qualification_posture": QUALIFICATION_POSTURE,
        "public_operations": list(PUBLIC_OPERATIONS),
        "internal_projection_operations": ["bootstrap"],
        "bootstrap_mode": "pure-projection-only",
        "same_runtime_required": True,
        "source_acquisition": "fixed-k1s-cli-json",
        "mcp": {
            "tool": MCP_TOOL_NAME,
            "arguments": ["operation", "profile", "component"],
            "bootstrap_exposed": False,
        },
        "readiness": {
            "ready": ready,
            "provider_distribution": "k1s-core",
            "provider_version": provider_version,
            "error_code": error_code,
        },
    }


def _validate_operation_safety(
    operation: AdapterOperation,
    payload: dict[str, Any],
    node_identity: object,
) -> None:
    if operation == "logs" and (
        payload["contentPolicy"] != "omitted"
        or type(payload["lineCount"]) is not int
        or not 0 <= payload["lineCount"] <= 200
        or type(payload["sourceTruncated"]) is not bool
        or not isinstance(payload["runRef"], str)
        or _DIGEST.fullmatch(payload["runRef"]) is None
    ):
        _fail("unsafe-metadata")
    if (
        operation == "status"
        and payload["runRef"] is not None
        and (not isinstance(payload["runRef"], str) or _DIGEST.fullmatch(payload["runRef"]) is None)
    ):
        _fail("unsafe-metadata")
    if operation != "bootstrap":
        if node_identity is not None:
            _fail("unsafe-metadata")
        return
    identity = _mapping(node_identity)
    _exact_keys(identity, _NODE_IDENTITY_KEYS)
    connections = _mapping(payload["connections"])
    _exact_keys(connections, {"advertise", "controller", "listen", "transport"})
    for connection in connections.values():
        _exact_keys(
            _mapping(connection),
            {"addressClass", "portPresent", "presence", "schemeClass", "securityClass"},
        )


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _fail("unsupported-schema")
    return cast(dict[str, Any], value)


def _exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        _fail("unsupported-schema")


def _load_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_OUTPUT_BYTES:
        _fail("size-limit")

    def reject_constant(_value: str) -> NoReturn:
        _fail("unsupported-schema")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                _fail("unsupported-schema")
            result[key] = item
        return result

    try:
        loaded = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except ProfileEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileEvidenceError("unsupported-schema") from exc
    return _mapping(loaded)


def _bounded_tree(value: object, depth: int = 0) -> None:
    if depth > MAX_NESTING:
        _fail("size-limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_KEYS:
                _fail("unsafe-metadata")
            _bounded_tree(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 512:
            _fail("size-limit")
        for item in value:
            _bounded_tree(item, depth + 1)
    elif isinstance(value, str) and len(value.encode("utf-8")) > MAX_INPUT_BYTES:
        _fail("size-limit")
    elif not isinstance(value, str | int | float | bool | None):
        _fail("unsupported-schema")


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProfileEvidenceError("unsupported-schema") from exc


def _evidence_id(unsigned: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        b"k1s-workerbee-profile-evidence-v1\0" + _canonical_json(unsigned)
    ).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "ADAPTER_API_VERSION",
    "ADAPTER_OPERATIONS",
    "CAPABILITY_SCHEMA",
    "CONSUMER_SCHEMA",
    "EVIDENCE_API_VERSION",
    "MCP_TOOL_NAME",
    "OPERATION_TABLE",
    "PROFILE_COMPONENTS",
    "PROFILE_NAMES",
    "PUBLIC_OPERATIONS",
    "ProfileEvidenceError",
    "consume_projected_evidence",
    "profile_evidence_capabilities",
    "validate_public_request",
]
