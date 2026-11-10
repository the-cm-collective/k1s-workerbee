"""Bounded public consumer for K1s profile-adapter evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol, cast

from workerbee.contract import WorkerBeeError
from workerbee.k1s_runtime import K1sRuntime, resolve_k1s_runtime
from workerbee.runtime_provider import RUNTIME_VERSION

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


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes


class ProcessRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        stdin: bytes | None,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> BoundedProcessResult: ...


OPERATION_TABLE: dict[AdapterOperation, OperationSpec] = {
    "list": OperationSpec("list", True, False, False),
    "doctor": OperationSpec("doctor", True, True, False),
    "plan": OperationSpec("plan", True, True, False),
    "status": OperationSpec("status", True, True, False),
    "logs": OperationSpec("logs", True, True, True),
    "bootstrap": OperationSpec("bootstrap", False, False, False),
}

_ADAPTER_BRIDGE_SCRIPT = r"""
import json
import sys

from ae.kctl.profile_adapter import (
    adapt_workerbee_profile_evidence,
    render_workerbee_profile_evidence_json,
)

request = json.loads(sys.stdin.buffer.read())
if set(request) != {"node_observation", "operation", "source", "source_exit_code"}:
    raise ValueError("invalid bridge request")
evidence = adapt_workerbee_profile_evidence(
    request["operation"],
    request["source"],
    source_exit_code=request["source_exit_code"],
    node_observation=request["node_observation"],
)
sys.stdout.write(render_workerbee_profile_evidence_json(evidence))
"""


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


class K1sProfileEvidenceBridge:
    """Resolve one exact K1s runtime and create same-runtime evidence sessions."""

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        runtime_resolver: Callable[..., K1sRuntime] = resolve_k1s_runtime,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self._cwd = cwd
        self._runtime_resolver = runtime_resolver
        self._process_runner = process_runner or _run_bounded

    def session(self) -> K1sProfileEvidenceSession:
        """Resolve exactly once; no caller-selected executable, root, or environment exists."""

        try:
            runtime = self._runtime_resolver(cwd=self._cwd)
            _validate_runtime(runtime)
        except ProfileEvidenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - public failures are deliberately redacted
            raise ProfileEvidenceError("provider-unavailable", retryable=True) from exc
        return K1sProfileEvidenceSession(runtime=runtime, process_runner=self._process_runner)

    def collect(
        self,
        operation: str,
        *,
        profile: str | None = None,
        component: str | None = None,
    ) -> dict[str, Any]:
        return self.session().collect(operation, profile=profile, component=component)

    def capabilities(self) -> dict[str, Any]:
        """Probe only provider binding; never acquire profile state for capability readback."""

        try:
            session = self.session()
        except ProfileEvidenceError as exc:
            return profile_evidence_capabilities(
                ready=False,
                provider_version=None,
                error_code=cast(str, exc.details["reason"]),
            )
        return profile_evidence_capabilities(
            ready=True,
            provider_version=session.provider_version,
        )


class K1sProfileEvidenceSession:
    """One selected K1s runtime shared by source acquisition and projection."""

    def __init__(self, *, runtime: K1sRuntime, process_runner: ProcessRunner) -> None:
        self._runtime = runtime
        self._process_runner = process_runner
        self._environment = runtime.apply_env(os.environ.copy())

    @property
    def provider_version(self) -> str:
        return cast(str, self._runtime.ae_version)

    def collect(
        self,
        operation: str,
        *,
        profile: str | None = None,
        component: str | None = None,
    ) -> dict[str, Any]:
        typed_operation, profile, component = validate_public_request(
            operation,
            profile=profile,
            component=component,
        )
        command = _source_command(
            self._runtime,
            typed_operation,
            profile=profile,
            component=component,
        )
        source = self._run(
            command,
            stdin=None,
            stdout_limit=MAX_INPUT_BYTES,
            accepted_exit_codes=((0, 1) if typed_operation == "doctor" else (0,)),
            failure_reason="source-acquisition-failed",
        )
        evidence = self._project(
            typed_operation,
            source_payload=source.stdout,
            source_exit_code=source.returncode,
            node_observation=None,
        )
        return {
            "evidence": evidence,
            "diagnostics": _safe_diagnostics(evidence, provider_version=self.provider_version),
        }

    def project_bootstrap(
        self,
        source_projection: bytes | str | dict[str, Any],
        *,
        node_observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Purely adapt an already-selected public bootstrap projection; never create one."""

        if isinstance(source_projection, dict):
            raw_source = _canonical_json(source_projection)
        elif isinstance(source_projection, str):
            raw_source = source_projection.encode("utf-8")
        elif isinstance(source_projection, bytes):
            raw_source = source_projection
        else:
            _fail("unsupported-schema")
        if not raw_source or len(raw_source) > MAX_INPUT_BYTES:
            _fail("size-limit")
        return self._project(
            "bootstrap",
            source_payload=raw_source,
            source_exit_code=0,
            node_observation=node_observation,
        )

    def _project(
        self,
        operation: AdapterOperation,
        *,
        source_payload: bytes,
        source_exit_code: int,
        node_observation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            source_text = source_payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProfileEvidenceError("source-shape-mismatch") from exc
        request = _canonical_json(
            {
                "node_observation": node_observation,
                "operation": operation,
                "source": source_text,
                "source_exit_code": source_exit_code,
            }
        )
        projected = self._run(
            (self._runtime.python_executable, "-c", _ADAPTER_BRIDGE_SCRIPT),
            stdin=request,
            stdout_limit=MAX_OUTPUT_BYTES,
            accepted_exit_codes=(0,),
            failure_reason="adapter-projection-failed",
        )
        evidence = consume_projected_evidence(projected.stdout)
        provenance = cast(dict[str, Any], evidence["provenance"])
        if provenance["distributionVersion"] != self.provider_version:
            _fail("provider-version-drift")
        return evidence

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        stdin: bytes | None,
        stdout_limit: int,
        accepted_exit_codes: tuple[int, ...],
        failure_reason: str,
    ) -> BoundedProcessResult:
        try:
            result = self._process_runner(
                argv,
                env=self._environment,
                stdin=stdin,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                stdout_limit=stdout_limit,
                stderr_limit=MAX_STDERR_BYTES,
            )
        except ProfileEvidenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter details must never escape
            raise ProfileEvidenceError(failure_reason, retryable=True) from exc
        if result.returncode not in accepted_exit_codes:
            _fail(failure_reason)
        if not result.stdout or len(result.stdout) > stdout_limit:
            _fail("size-limit")
        return result


def _validate_runtime(runtime: K1sRuntime) -> None:
    if runtime.source not in {"installed", "sibling"}:
        _fail("unsupported-provider-source")
    if runtime.ae_version != RUNTIME_VERSION or not runtime.ae_origin:
        _fail("provider-version-mismatch")
    status = runtime.provider_status
    if not isinstance(status, dict) or status.get("ok") is not True:
        _fail("provider-ownership-mismatch")
    if status.get("state") not in {"exact", "source-checkout"}:
        _fail("provider-ownership-mismatch")
    if runtime.source == "installed":
        if runtime.k1s_root is not None or runtime.pythonpath is not None:
            _fail("provider-binding-mismatch")
        if status.get("state") != "exact" or status.get("providers") != ["k1s-core"]:
            _fail("provider-ownership-mismatch")
        return
    if runtime.k1s_root is None or runtime.pythonpath is None:
        _fail("provider-binding-mismatch")
    expected_pythonpath = (runtime.k1s_root / "src").resolve()
    if Path(runtime.pythonpath).resolve() != expected_pythonpath:
        _fail("provider-binding-mismatch")
    try:
        origin = Path(runtime.ae_origin).resolve()
        origin.relative_to(expected_pythonpath / "ae")
    except (OSError, ValueError) as exc:
        raise ProfileEvidenceError("provider-binding-mismatch") from exc


def _source_command(
    runtime: K1sRuntime,
    operation: PublicOperation,
    *,
    profile: str | None,
    component: str | None,
) -> tuple[str, ...]:
    command = [runtime.python_executable, "-m", "ae.kctl", "profile", operation]
    if profile is not None:
        command.append(profile)
    if operation == "logs":
        command.extend(("--component", cast(str, component), "--tail", "200"))
    command.extend(("--output", "json"))
    return tuple(command)


def _safe_diagnostics(evidence: dict[str, Any], *, provider_version: str) -> dict[str, Any]:
    operation = cast(str, evidence["operation"])
    payload = cast(dict[str, Any], evidence["payload"])
    diagnostics: dict[str, Any] = {
        "operation": operation,
        "provider_distribution": "k1s-core",
        "provider_version": provider_version,
        "qualification_posture": QUALIFICATION_POSTURE,
        "same_runtime": True,
    }
    if operation == "status":
        diagnostics["state"] = {
            "lifecycle_status": payload["lifecycleStatus"],
            "phase": payload["phase"],
            "profile_generation": payload["profileGeneration"],
            "run_ref": payload["runRef"],
        }
    elif operation == "logs":
        diagnostics["logs"] = {
            "content_policy": payload["contentPolicy"],
            "line_count": payload["lineCount"],
            "run_ref": payload["runRef"],
            "source_truncated": payload["sourceTruncated"],
        }
    return diagnostics


def _run_bounded(
    argv: tuple[str, ...],
    *,
    env: Mapping[str, str],
    stdin: bytes | None,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> BoundedProcessResult:
    """Run one fixed argv without a shell while bounding time and both output pipes."""

    if not argv or timeout <= 0 or stdout_limit <= 0 or stderr_limit <= 0:
        _fail("invalid-process-contract")
    if stdin is not None and len(stdin) > (MAX_INPUT_BYTES * 2 + MAX_OUTPUT_BYTES):
        _fail("size-limit")
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv is validated by the caller
            list(argv),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ProfileEvidenceError("subprocess-unavailable", retryable=True) from exc
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    input_view = memoryview(stdin or b"")
    input_offset = 0
    for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)
    if input_view:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                _fail("subprocess-timeout", retryable=True)
            events = selector.select(remaining)
            if not events:
                _stop_process(process)
                _fail("subprocess-timeout", retryable=True)
            for key, _mask in events:
                stream = key.fileobj
                label = cast(str, key.data)
                if label == "stdin":
                    try:
                        written = os.write(
                            stream.fileno(), input_view[input_offset : input_offset + 65536]
                        )
                    except BrokenPipeError:
                        written = len(input_view) - input_offset
                    input_offset += written
                    if input_offset >= len(input_view):
                        selector.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                target = stdout if label == "stdout" else stderr
                target.extend(chunk)
                limit = stdout_limit if label == "stdout" else stderr_limit
                if len(target) > limit:
                    _stop_process(process)
                    _fail("size-limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process(process)
            _fail("subprocess-timeout", retryable=True)
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        _stop_process(process)
        raise ProfileEvidenceError("subprocess-timeout", retryable=True) from exc
    finally:
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    return BoundedProcessResult(returncode=returncode, stdout=bytes(stdout))


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1.0)


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
