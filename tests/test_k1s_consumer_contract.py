from __future__ import annotations

import json
from pathlib import Path

from workerbee.k1s_consumer_contract import (
    ADVANCED_AE_ENTRYPOINTS,
    CATALOG_ROLES,
    CONTRACT_SCHEMA,
    K1S_CONSUMER_SURFACES,
    PORTABLE_ROLES,
    k1s_v1_consumer_contract,
)
from workerbee.profile_evidence import ADAPTER_OPERATIONS, PUBLIC_OPERATIONS

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/k1s-consumer-contract.md"
V1_DOC = ROOT / "docs/v1-contract.md"


def test_contract_is_versioned_bounded_and_matches_exact_provider() -> None:
    contract = k1s_v1_consumer_contract()
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()

    assert contract["schema"] == CONTRACT_SCHEMA
    assert contract["version"] == 1
    assert contract["provider_requirement"] == "k1s-core==1.0.0"
    assert contract["qualification_posture"] == "source-validated/unqualified"
    assert len(encoded) < 16 * 1024
    assert not ({"environment", "executable", "path", "credential"} & set(contract))


def test_core_gateway_node_and_evidence_operation_boundaries_are_exact() -> None:
    contract = k1s_v1_consumer_contract()
    evidence = contract["workerbee_profile_evidence"]

    assert PORTABLE_ROLES == ("core", "edge-gateway", "node")
    assert CATALOG_ROLES == ("local", "core", "edge-gateway", "node", "edge-dev", "core-ha")
    assert evidence == {
        "bootstrap_creation": False,
        "internal_projection_operations": ["bootstrap"],
        "profile_components": {
            "local": ["controller", "node-local"],
            "core": ["controller"],
            "edge-gateway": ["gateway"],
            "node": ["node-agent"],
        },
        "public_operations": list(PUBLIC_OPERATIONS),
        "strict_cri_host_transactions": False,
    }
    assert (*PUBLIC_OPERATIONS, "bootstrap") == ADAPTER_OPERATIONS


def test_bootstrap_and_strict_cri_mutation_cannot_enter_read_only_evidence() -> None:
    surfaces = {surface.surface_id: surface for surface in K1S_CONSUMER_SURFACES}

    bootstrap = surfaces["profile.bootstrap.create"]
    assert bootstrap.profiles == ("edge-gateway", "node")
    assert bootstrap.mutation_intent == "mutating"
    assert bootstrap.workerbee_use == "pure-projection-only"

    host_plan = surfaces["profile.host.plan.strict-cri"]
    assert host_plan.mutation_intent == "read-only"
    assert host_plan.workerbee_use == "direct-cli-only"

    for surface_id in (
        "profile.host.prepare.strict-cri",
        "profile.host.rollback.strict-cri",
    ):
        surface = surfaces[surface_id]
        assert surface.mutation_intent == "mutating"
        assert surface.workerbee_use == "direct-cli-only"
        assert surface.evidence_level == "deferred-live"

    evidence_commands = {
        surface.surface_id
        for surface in K1S_CONSUMER_SURFACES
        if surface.workerbee_use == "metadata-safe-source"
    }
    assert evidence_commands == {
        "profile.catalog",
        "profile.doctor",
        "profile.plan",
        "profile.status",
        "profile.logs",
    }


def test_advanced_ae_is_narrow_and_never_an_ambient_fallback() -> None:
    contract = k1s_v1_consumer_contract()

    assert ADVANCED_AE_ENTRYPOINTS == (
        "ae",
        "ae-gateway",
        "ae-node",
        "ae-rotate-certs",
        "ae-worker-stub",
    )
    assert contract["advanced_ae"] == {
        "entrypoints": list(ADVANCED_AE_ENTRYPOINTS),
        "policy": "only-when-no-compact-k1s-equivalent",
        "profile_evidence_exposed": False,
    }
    encoded = json.dumps(contract)
    assert "PYTHONPATH" not in encoded
    assert "k1s_root" not in encoded
    assert "argv" not in encoded


def test_public_docs_record_portable_and_host_authority_boundaries() -> None:
    doc = DOC.read_text(encoding="utf-8")
    v1_doc = V1_DOC.read_text(encoding="utf-8")

    for value in (
        "`core`",
        "`edge-gateway`",
        "`node`",
        "`k1s profile bootstrap create`",
        "`k1s profile host plan strict-cri`",
        "`k1s profile host prepare strict-cri`",
        "`k1s profile host rollback strict-cri`",
        "source-validated/unqualified",
        "only when the compact `k1s` CLI has no equivalent",
    ):
        assert value in doc
    assert "does not qualify live runtime behavior" in doc
    assert "k1s-consumer-contract.md" in v1_doc
