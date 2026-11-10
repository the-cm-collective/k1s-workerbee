from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from workerbee.k1s_runtime import K1sRuntime
from workerbee.profile_evidence import (
    ADAPTER_API_VERSION,
    ADAPTER_OPERATIONS,
    CAPABILITY_SCHEMA,
    EVIDENCE_API_VERSION,
    PUBLIC_OPERATIONS,
    BoundedProcessResult,
    K1sProfileEvidenceBridge,
    ProfileEvidenceError,
    _run_bounded,
    consume_projected_evidence,
    profile_evidence_capabilities,
    validate_public_request,
)


def _payload(operation: str) -> dict[str, object]:
    digest = "sha256:" + "1" * 64
    payloads: dict[str, dict[str, object]] = {
        "list": {"profiles": []},
        "doctor": {
            "alias": False,
            "canonicalName": "core",
            "checks": [],
            "compatibilityVariant": None,
            "contractPosture": "ga",
            "executionStatus": "portable",
            "productionEligible": True,
            "qualificationPosture": "source-validated/unqualified",
            "requestedName": "core",
            "status": "pass",
        },
        "plan": {
            "alias": False,
            "bootstrapRequired": False,
            "canonicalName": "core",
            "compatibilityVariant": None,
            "components": ["controller"],
            "contractPosture": "ga",
            "executionStatus": "portable",
            "productionEligible": True,
            "qualificationPosture": "source-validated/unqualified",
            "requestedName": "core",
            "requirements": [],
            "supportedBackends": ["podman", "docker"],
            "warningCodes": [],
        },
        "status": {
            "backend": "podman",
            "lifecycleStatus": "running",
            "phase": "running",
            "profile": "node",
            "profileGeneration": 1,
            "qualificationPosture": "source-validated/unqualified",
            "runRef": digest,
        },
        "logs": {
            "component": "node-agent",
            "contentPolicy": "omitted",
            "lineCount": 2,
            "profile": "node",
            "runRef": digest,
            "sourceTruncated": False,
        },
        "bootstrap": {
            "backendId": "podman",
            "bootstrapId": "b1-0123456789abcdef0123456789abcdef",
            "connections": {
                name: {
                    "addressClass": "hostname",
                    "portPresent": True,
                    "presence": "present",
                    "schemeClass": "https",
                    "securityClass": "tls",
                }
                for name in ("advertise", "controller", "listen", "transport")
            },
            "profile": "node",
            "siteId": None,
        },
    }
    return payloads[operation]


def _source_contract(operation: str) -> tuple[str | None, str, str, int | None]:
    return {
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
        "bootstrap": (
            None,
            "ProfileBootstrapPublicProjection",
            "profile-bootstrap-create-json",
            None,
        ),
    }[operation]


def _fixture(operation: str) -> dict[str, object]:
    source_api, source_kind, source_mode, source_schema = _source_contract(operation)
    node_identity = None
    if operation == "bootstrap":
        node_identity = {
            "generation": None,
            "generationProvenance": "unobserved",
            "identityMode": "explicit",
            "identityVersion": 1,
            "nodeId": "node-a",
        }
    evidence: dict[str, object] = {
        "apiVersion": EVIDENCE_API_VERSION,
        "kind": "WorkerBeeProfileEvidence",
        "nodeIdentity": node_identity,
        "operation": operation,
        "payload": _payload(operation),
        "provenance": {
            "adapterApiVersion": ADAPTER_API_VERSION,
            "distribution": "k1s-core",
            "distributionVersion": "1.0.0a3",
            "sourceApiVersion": source_api,
            "sourceExitCode": 0,
            "sourceKind": source_kind,
            "sourceMode": source_mode,
            "sourceSchemaVersion": source_schema,
        },
        "qualificationPosture": "source-validated/unqualified",
        "schemaVersion": 1,
    }
    raw = (
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()
    evidence["evidenceId"] = (
        "sha256:" + hashlib.sha256(b"k1s-workerbee-profile-evidence-v1\0" + raw).hexdigest()
    )
    return evidence


def _canonical(evidence: dict[str, object]) -> bytes:
    return (
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _provider_status(state: str = "exact") -> dict[str, object]:
    return {
        "schema": "workerbee.runtime-provider-status/v1",
        "state": state,
        "ok": True,
        "providers": ["k1s-core"] if state == "exact" else [],
        "expected": "k1s-core",
        "forbidden": "k1s-workerbee-runtime",
        "import_package": "ae",
        "requirement": "k1s-core==1.0.0a3",
    }


def _installed_runtime() -> K1sRuntime:
    return K1sRuntime(
        source="installed",
        python_executable="/opt/workerbee/bin/python",
        k1s_root=None,
        pythonpath=None,
        ae_origin="/opt/workerbee/site-packages/ae/__init__.py",
        ae_version="1.0.0a3",
        provider_status=_provider_status(),
    )


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        env: Any,
        stdin: bytes | None,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> BoundedProcessResult:
        self.calls.append(
            {
                "argv": argv,
                "env": dict(env),
                "stdin": stdin,
                "timeout": timeout,
                "stdout_limit": stdout_limit,
                "stderr_limit": stderr_limit,
            }
        )
        if argv[1:4] == ("-m", "ae.kctl", "profile"):
            return BoundedProcessResult(returncode=0, stdout=b"{}\n")
        request = json.loads((stdin or b"").decode())
        evidence = _fixture(request["operation"])
        evidence["provenance"]["sourceExitCode"] = request["source_exit_code"]  # type: ignore[index]
        unsigned = {key: item for key, item in evidence.items() if key != "evidenceId"}
        raw = (
            json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
        evidence["evidenceId"] = (
            "sha256:" + hashlib.sha256(b"k1s-workerbee-profile-evidence-v1\0" + raw).hexdigest()
        )
        return BoundedProcessResult(returncode=0, stdout=_canonical(evidence))


@pytest.mark.parametrize("operation", ADAPTER_OPERATIONS)
def test_consumer_accepts_pure_six_operation_fixtures(operation: str) -> None:
    evidence = _fixture(operation)

    assert consume_projected_evidence(_canonical(evidence)) == evidence


def test_public_request_has_exact_nonmutating_operation_table() -> None:
    assert PUBLIC_OPERATIONS == ("list", "doctor", "plan", "status", "logs")
    assert validate_public_request("list") == ("list", None, None)
    assert validate_public_request("doctor", profile="core") == ("doctor", "core", None)
    assert validate_public_request("status", profile="node") == ("status", "node", None)
    assert validate_public_request("logs", profile="node", component="node-agent") == (
        "logs",
        "node",
        "node-agent",
    )


@pytest.mark.parametrize(
    ("operation", "profile", "component"),
    [
        ("bootstrap", None, None),
        ("list", "core", None),
        ("plan", None, None),
        ("status", "edge-dev", None),
        ("logs", "node", None),
        ("logs", "node", "controller"),
        ("doctor", "core", "controller"),
    ],
)
def test_public_request_rejects_unsupported_or_ambiguous_inputs(
    operation: str,
    profile: str | None,
    component: str | None,
) -> None:
    with pytest.raises(ProfileEvidenceError) as raised:
        validate_public_request(operation, profile=profile, component=component)

    assert raised.value.code == "K1S_PROFILE_EVIDENCE_FAILED_CLOSED"
    assert str(raised.value) == "K1s profile evidence request failed closed"


def test_consumer_rejects_digest_drift_raw_metadata_and_noncanonical_bytes() -> None:
    evidence = _fixture("logs")
    drifted = copy.deepcopy(evidence)
    drifted["payload"]["lineCount"] = 3  # type: ignore[index]
    with pytest.raises(ProfileEvidenceError):
        consume_projected_evidence(drifted)

    unsafe = _fixture("logs")
    unsafe["payload"]["lines"] = ["token=private"]  # type: ignore[index]
    with pytest.raises(ProfileEvidenceError) as raised:
        consume_projected_evidence(unsafe)
    assert raised.value.details == {"reason": "unsafe-metadata"}
    assert "private" not in str(raised.value)

    with pytest.raises(ProfileEvidenceError):
        consume_projected_evidence(json.dumps(evidence))


def test_capability_contract_is_versioned_bounded_and_excludes_bootstrap_creation() -> None:
    capability = profile_evidence_capabilities(ready=True, provider_version="1.0.0a3")
    encoded = json.dumps(capability, sort_keys=True, separators=(",", ":")).encode()

    assert len(encoded) < 4096
    assert capability["schema"] == CAPABILITY_SCHEMA
    assert capability["public_operations"] == list(PUBLIC_OPERATIONS)
    assert capability["internal_projection_operations"] == ["bootstrap"]
    assert capability["bootstrap_mode"] == "pure-projection-only"
    assert capability["qualification_posture"] == "source-validated/unqualified"
    assert capability["mcp"] == {
        "tool": "workerbee_v1_k1s_profile_evidence",
        "arguments": ["operation", "profile", "component"],
        "bootstrap_exposed": False,
    }
    assert "path" not in encoded.decode()
    assert "executable" not in encoded.decode()


def test_installed_bridge_resolves_once_and_uses_one_python_for_source_and_adapter() -> None:
    resolutions = 0
    runner = _FakeRunner()

    def resolve(**_kwargs: object) -> K1sRuntime:
        nonlocal resolutions
        resolutions += 1
        return _installed_runtime()

    result = K1sProfileEvidenceBridge(
        runtime_resolver=resolve,
        process_runner=runner,
    ).collect("status", profile="node")

    assert resolutions == 1
    assert len(runner.calls) == 2
    assert {call["argv"][0] for call in runner.calls} == {"/opt/workerbee/bin/python"}
    assert runner.calls[0]["argv"] == (
        "/opt/workerbee/bin/python",
        "-m",
        "ae.kctl",
        "profile",
        "status",
        "node",
        "--output",
        "json",
    )
    assert result["diagnostics"]["state"] == {
        "lifecycle_status": "running",
        "phase": "running",
        "profile_generation": 1,
        "run_ref": "sha256:" + "1" * 64,
    }
    assert result["diagnostics"]["same_runtime"] is True


def test_explicit_checkout_bridge_pins_pythonpath_for_both_subprocesses(tmp_path: Path) -> None:
    root = tmp_path / "k1s"
    runtime = K1sRuntime(
        source="sibling",
        python_executable=str(root / ".venv/bin/python"),
        k1s_root=root,
        pythonpath=str(root / "src"),
        ae_origin=str(root / "src/ae/__init__.py"),
        ae_version="1.0.0a3",
        provider_status=_provider_status("source-checkout"),
    )
    runner = _FakeRunner()

    result = K1sProfileEvidenceBridge(
        cwd=tmp_path,
        runtime_resolver=lambda **_kwargs: runtime,
        process_runner=runner,
    ).collect("logs", profile="node", component="node-agent")

    assert len(runner.calls) == 2
    assert {call["argv"][0] for call in runner.calls} == {runtime.python_executable}
    assert all(
        call["env"]["PYTHONPATH"].split(os.pathsep)[0] == runtime.pythonpath
        for call in runner.calls
    )
    assert runner.calls[0]["argv"][-8:] == (
        "logs",
        "node",
        "--component",
        "node-agent",
        "--tail",
        "200",
        "--output",
        "json",
    )
    assert result["diagnostics"]["logs"] == {
        "content_policy": "omitted",
        "line_count": 2,
        "run_ref": "sha256:" + "1" * 64,
        "source_truncated": False,
    }
    assert "lines" not in json.dumps(result)


def test_one_session_retains_w4_six_operation_same_runtime_qualification() -> None:
    resolutions = 0
    runner = _FakeRunner()

    def resolve(**_kwargs: object) -> K1sRuntime:
        nonlocal resolutions
        resolutions += 1
        return _installed_runtime()

    session = K1sProfileEvidenceBridge(
        runtime_resolver=resolve,
        process_runner=runner,
    ).session()
    results = [
        session.collect("list"),
        session.collect("doctor", profile="core"),
        session.collect("plan", profile="core"),
        session.collect("status", profile="node"),
        session.collect("logs", profile="node", component="node-agent"),
        {
            "evidence": session.project_bootstrap(
                {
                    "advertiseEndpoint": "https://node.example:9109",
                    "backendId": "podman",
                    "bootstrapId": "b1-0123456789abcdef0123456789abcdef",
                    "controllerUrl": "https://controller.example:9443",
                    "identityVersion": 1,
                    "listenHost": "127.0.0.1",
                    "listenPort": 9109,
                    "nodeId": "node-a",
                    "profile": "node",
                    "siteId": None,
                    "transportUrl": None,
                }
            )
        },
    ]

    assert resolutions == 1
    assert [result["evidence"]["operation"] for result in results] == list(ADAPTER_OPERATIONS)
    assert {call["argv"][0] for call in runner.calls} == {"/opt/workerbee/bin/python"}
    assert sum(call["argv"][1:4] == ("-m", "ae.kctl", "profile") for call in runner.calls) == 5
    assert all("bootstrap" not in call["argv"] for call in runner.calls)


@pytest.mark.parametrize(
    "runtime",
    [
        K1sRuntime(
            source="installed",
            python_executable="/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site/ae/__init__.py",
            ae_version="1.0.0a2",
            provider_status=_provider_status(),
        ),
        K1sRuntime(
            source="installed",
            python_executable="/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site/ae/__init__.py",
            ae_version="1.0.0a3",
            provider_status={**_provider_status(), "ok": False, "state": "mixed"},
        ),
    ],
)
def test_bridge_rejects_provider_version_or_ownership_drift(runtime: K1sRuntime) -> None:
    runner = _FakeRunner()
    bridge = K1sProfileEvidenceBridge(
        runtime_resolver=lambda **_kwargs: runtime,
        process_runner=runner,
    )

    with pytest.raises(ProfileEvidenceError) as raised:
        bridge.collect("list")

    assert raised.value.code == "K1S_PROFILE_EVIDENCE_FAILED_CLOSED"
    assert runner.calls == []


def test_bridge_redacts_private_runner_exception_text() -> None:
    def fail_runner(*_args: object, **_kwargs: object) -> BoundedProcessResult:
        raise RuntimeError("token=private /home/operator/.config")

    bridge = K1sProfileEvidenceBridge(
        runtime_resolver=lambda **_kwargs: _installed_runtime(),
        process_runner=fail_runner,
    )

    with pytest.raises(ProfileEvidenceError) as raised:
        bridge.collect("list")

    public = json.dumps(raised.value.public_dict())
    assert "private" not in public
    assert "/home" not in public
    assert raised.value.details == {"reason": "source-acquisition-failed"}


def test_bounded_runner_caps_stdout_stderr_and_wall_time() -> None:
    environment = os.environ.copy()
    ok = _run_bounded(
        (sys.executable, "-c", "print('ok')"),
        env=environment,
        stdin=None,
        timeout=2.0,
        stdout_limit=8,
        stderr_limit=8,
    )
    assert ok == BoundedProcessResult(returncode=0, stdout=b"ok\n")

    with pytest.raises(ProfileEvidenceError) as stdout_error:
        _run_bounded(
            (sys.executable, "-c", "print('0123456789')"),
            env=environment,
            stdin=None,
            timeout=2.0,
            stdout_limit=8,
            stderr_limit=8,
        )
    assert stdout_error.value.details == {"reason": "size-limit"}

    with pytest.raises(ProfileEvidenceError) as stderr_error:
        _run_bounded(
            (sys.executable, "-c", "import sys; sys.stderr.write('0123456789')"),
            env=environment,
            stdin=None,
            timeout=2.0,
            stdout_limit=8,
            stderr_limit=8,
        )
    assert stderr_error.value.details == {"reason": "size-limit"}

    with pytest.raises(ProfileEvidenceError) as timeout_error:
        _run_bounded(
            (sys.executable, "-c", "import time; time.sleep(2)"),
            env=environment,
            stdin=None,
            timeout=0.1,
            stdout_limit=8,
            stderr_limit=8,
        )
    assert timeout_error.value.details == {"reason": "subprocess-timeout"}
