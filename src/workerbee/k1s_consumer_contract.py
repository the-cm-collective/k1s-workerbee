"""WorkerBee-owned policy for consuming the public K1s v1 surface.

This module inventories stable public commands and their authority boundaries.
It does not execute K1s, duplicate K1s implementation, or grant lifecycle or
host-mutation authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from workerbee.profile_evidence import (
    ADAPTER_OPERATIONS,
    PROFILE_COMPONENTS,
    PUBLIC_OPERATIONS,
    QUALIFICATION_POSTURE,
)
from workerbee.runtime_provider import RUNTIME_REQUIREMENT

CONTRACT_SCHEMA = "workerbee.k1s-v1-consumer-contract/v1"
CONTRACT_VERSION = 1

PORTABLE_ROLES = ("core", "edge-gateway", "node")
CATALOG_ROLES = ("local", *PORTABLE_ROLES, "edge-dev", "core-ha")

MutationIntent = Literal["read-only", "mutating"]
WorkerBeeUse = Literal[
    "metadata-safe-source",
    "pure-projection-only",
    "direct-cli-only",
]


@dataclass(frozen=True, slots=True)
class K1sConsumerSurface:
    """One compact K1s command family and its WorkerBee consumption boundary."""

    surface_id: str
    command: tuple[str, ...]
    profiles: tuple[str, ...]
    mutation_intent: MutationIntent
    workerbee_use: WorkerBeeUse
    evidence_level: str
    boundary: str

    def public(self) -> dict[str, object]:
        """Return a deterministic, path-free public descriptor."""

        return {
            "boundary": self.boundary,
            "command": list(self.command),
            "evidence_level": self.evidence_level,
            "mutation_intent": self.mutation_intent,
            "profiles": list(self.profiles),
            "surface_id": self.surface_id,
            "workerbee_use": self.workerbee_use,
        }


K1S_CONSUMER_SURFACES: tuple[K1sConsumerSurface, ...] = (
    K1sConsumerSurface(
        surface_id="profile.catalog",
        command=("k1s", "profile", "list", "--output", "json"),
        profiles=CATALOG_ROLES,
        mutation_intent="read-only",
        workerbee_use="metadata-safe-source",
        evidence_level="installed-contract",
        boundary="Installed catalog metadata only; no engine, profile state, or network probe.",
    ),
    K1sConsumerSurface(
        surface_id="profile.doctor",
        command=("k1s", "profile", "doctor", "ROLE", "--output", "json"),
        profiles=CATALOG_ROLES,
        mutation_intent="read-only",
        workerbee_use="metadata-safe-source",
        evidence_level="installed-contract",
        boundary="WorkerBee never requests the optional engine probe.",
    ),
    K1sConsumerSurface(
        surface_id="profile.plan",
        command=("k1s", "profile", "plan", "ROLE", "--output", "json"),
        profiles=CATALOG_ROLES,
        mutation_intent="read-only",
        workerbee_use="metadata-safe-source",
        evidence_level="installed-contract",
        boundary="Plan output describes a contract; it is not live readiness evidence.",
    ),
    K1sConsumerSurface(
        surface_id="profile.status",
        command=("k1s", "profile", "status", "ROLE", "--output", "json"),
        profiles=("local", *PORTABLE_ROLES),
        mutation_intent="read-only",
        workerbee_use="metadata-safe-source",
        evidence_level="source-validated",
        boundary="Reads exact owned local state; no lifecycle transition is authorized.",
    ),
    K1sConsumerSurface(
        surface_id="profile.logs",
        command=(
            "k1s",
            "profile",
            "logs",
            "ROLE",
            "--component",
            "COMPONENT",
            "--tail",
            "200",
            "--output",
            "json",
        ),
        profiles=("local", *PORTABLE_ROLES),
        mutation_intent="read-only",
        workerbee_use="metadata-safe-source",
        evidence_level="source-validated",
        boundary="Adapter output retains metadata only; raw log lines never cross the boundary.",
    ),
    K1sConsumerSurface(
        surface_id="profile.bootstrap.create",
        command=("k1s", "profile", "bootstrap", "create", "ROLE"),
        profiles=("edge-gateway", "node"),
        mutation_intent="mutating",
        workerbee_use="pure-projection-only",
        evidence_level="deferred-live",
        boundary=(
            "WorkerBee never creates bootstrap state; it may only project an already selected "
            "public bootstrap result from an independently authorized workflow."
        ),
    ),
    K1sConsumerSurface(
        surface_id="profile.host.plan.strict-cri",
        command=("k1s", "profile", "host", "plan", "strict-cri"),
        profiles=("node",),
        mutation_intent="read-only",
        workerbee_use="direct-cli-only",
        evidence_level="source-validated",
        boundary="Pure digest-pinned planning is outside the profile-evidence MCP surface.",
    ),
    K1sConsumerSurface(
        surface_id="profile.host.prepare.strict-cri",
        command=("k1s", "profile", "host", "prepare", "strict-cri"),
        profiles=("node",),
        mutation_intent="mutating",
        workerbee_use="direct-cli-only",
        evidence_level="deferred-live",
        boundary=(
            "Requires explicit transaction identity, evidence digests, confirmation, and a "
            "separately authorized pre-opened helper boundary."
        ),
    ),
    K1sConsumerSurface(
        surface_id="profile.host.rollback.strict-cri",
        command=("k1s", "profile", "host", "rollback", "strict-cri"),
        profiles=("node",),
        mutation_intent="mutating",
        workerbee_use="direct-cli-only",
        evidence_level="deferred-live",
        boundary=(
            "Requires an exact applied receipt and explicit confirmation; no automatic "
            "WorkerBee recovery authority is implied."
        ),
    ),
)

ADVANCED_AE_ENTRYPOINTS = (
    "ae",
    "ae-gateway",
    "ae-node",
    "ae-rotate-certs",
    "ae-worker-stub",
)


def k1s_v1_consumer_contract() -> dict[str, object]:
    """Return the bounded public contract used by WorkerBee tests and docs."""

    return {
        "advanced_ae": {
            "entrypoints": list(ADVANCED_AE_ENTRYPOINTS),
            "policy": "only-when-no-compact-k1s-equivalent",
            "profile_evidence_exposed": False,
        },
        "provider_requirement": RUNTIME_REQUIREMENT,
        "qualification_posture": QUALIFICATION_POSTURE,
        "schema": CONTRACT_SCHEMA,
        "surfaces": [surface.public() for surface in K1S_CONSUMER_SURFACES],
        "version": CONTRACT_VERSION,
        "workerbee_profile_evidence": {
            "bootstrap_creation": False,
            "internal_projection_operations": [
                operation for operation in ADAPTER_OPERATIONS if operation not in PUBLIC_OPERATIONS
            ],
            "profile_components": {
                profile: list(components) for profile, components in PROFILE_COMPONENTS.items()
            },
            "public_operations": list(PUBLIC_OPERATIONS),
            "strict_cri_host_transactions": False,
        },
    }


__all__ = [
    "ADVANCED_AE_ENTRYPOINTS",
    "CATALOG_ROLES",
    "CONTRACT_SCHEMA",
    "CONTRACT_VERSION",
    "K1S_CONSUMER_SURFACES",
    "PORTABLE_ROLES",
    "K1sConsumerSurface",
    "k1s_v1_consumer_contract",
]
