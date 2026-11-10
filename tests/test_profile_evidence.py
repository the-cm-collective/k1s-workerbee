from __future__ import annotations

import copy
import hashlib
import json

import pytest

from workerbee.profile_evidence import (
    ADAPTER_API_VERSION,
    ADAPTER_OPERATIONS,
    CAPABILITY_SCHEMA,
    EVIDENCE_API_VERSION,
    PUBLIC_OPERATIONS,
    ProfileEvidenceError,
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
