"""Versioned public runtime and MCP tool-policy readback."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from workerbee.controlled_runs import CONTROLLED_RUN_TOOL_NAMES

RUNTIME_POLICY_SCHEMA = "workerbee.runtime-policy/v1"
TOOL_POLICY_SCHEMA = "workerbee.mcp-tool-policy/v1"

_READ_ONLY_LOW = frozenset(
    {
        "workerbee_v1_capabilities",
        "workerbee_v1_projects",
        "workerbee_v1_project_mode_get",
        "workerbee_v1_project_status",
        "workerbee_v1_project_runbook_status",
        "workerbee_v1_profile_list",
        "workerbee_v1_profile_status",
        "workerbee_v1_profile_validate",
        "workerbee_v1_profile_workload_status",
        "workerbee_v1_profile_workload_validate",
        "workerbee_v1_edge_link_status",
        "workerbee_v1_edge_link_validate",
        "workerbee_v1_logs",
        "workerbee_v1_ingress_status",
        "workerbee_v1_manifest_validate",
        "workerbee_v1_secret_policy_status",
        "workerbee_v1_trust_status",
        "workerbee_v1_readiness_receipt",
        "workerbee_v1_controlled_run_preflight",
        "workerbee_v1_controlled_run_status",
        "workerbee_v1_controlled_run_receipt",
    }
)
_READ_ONLY_LOCAL_STATE = frozenset({"workerbee_v1_k1s_profile_evidence"})
_READ_ONLY_NETWORK = frozenset({"workerbee_v1_ingress_probe", "workerbee_v1_security_assess"})
_PROJECT_STATE = frozenset(
    {
        "workerbee_v1_session_start",
        "workerbee_v1_project_mode_set",
        "workerbee_v1_project_runbook_update",
        "workerbee_v1_project_runbook_import",
        "workerbee_v1_project_runbook_export",
        "workerbee_v1_manifest_prepare",
        "workerbee_v1_bundle_export",
        "workerbee_v1_security_review_project",
    }
)
_WORKLOAD_LIFECYCLE = frozenset(
    {
        "workerbee_v1_project_start",
        "workerbee_v1_project_stop",
        "workerbee_v1_project_reset",
        "workerbee_v1_profile_start",
        "workerbee_v1_profile_stop",
        "workerbee_v1_edge_link_start",
        "workerbee_v1_edge_link_stop",
        "workerbee_v1_workload_restart",
        "workerbee_v1_image_build",
        "workerbee_v1_manifest_deploy_local",
    }
)
_HIGH_RISK = frozenset(
    {
        "workerbee_v1_exec",
        "workerbee_v1_ingress_ca_regenerate",
        "workerbee_v1_manifest_deploy_remote_k1s",
        "workerbee_v1_cleanup",
        "workerbee_v1_trust_install",
        "workerbee_v1_trust_uninstall",
        "workerbee_v1_controlled_run_start",
        "workerbee_v1_controlled_run_cancel",
    }
)


def runtime_state_readback(
    configured: str | None,
    diagnostics: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Separate configured intent from measured runtime evidence."""

    diagnostic_map = dict(diagnostics or {})
    configured_value = configured.strip().lower() if isinstance(configured, str) else None
    measured_value = diagnostic_map.get("selected")
    if not isinstance(measured_value, str) or not measured_value:
        measured_value = None

    configured_entry = {
        "state": "configured" if configured_value else "unknown",
        "value": configured_value,
        "provenance": "daemon configuration" if configured_value else "not configured",
    }
    measured_entry = {
        "state": "measured" if measured_value else "unknown",
        "value": measured_value,
        "provenance": (
            "runtime diagnostics probe" if measured_value else "runtime probe produced no selection"
        ),
    }
    effective = measured_entry if measured_value else configured_entry
    return {
        "schema": RUNTIME_POLICY_SCHEMA,
        "configured": configured_entry,
        "measured": measured_entry,
        "effective": {"state": effective["state"], "value": effective["value"]},
        "evidence": {
            "probe_ok": (
                diagnostic_map.get("ok") if isinstance(diagnostic_map.get("ok"), bool) else None
            ),
            "error_code": _diagnostic_error_code(diagnostic_map),
        },
    }


def tool_policy_descriptors(advertised_tools: Iterable[str]) -> dict[str, Any]:
    """Return a complete, stable risk classification for advertised tools."""

    tools = sorted(set(advertised_tools))
    descriptors: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in tools:
        descriptor = _descriptor(name)
        if descriptor is None:
            missing.append(name)
        else:
            descriptors.append(descriptor)
    if missing:
        raise ValueError(f"unclassified advertised MCP tools: {', '.join(missing)}")
    return {
        "schema": TOOL_POLICY_SCHEMA,
        "complete": True,
        "count": len(descriptors),
        "tools": descriptors,
    }


def _descriptor(name: str) -> dict[str, Any] | None:
    common: dict[str, Any] = {
        "name": name,
        "availability": "conditional" if name in CONTROLLED_RUN_TOOL_NAMES else "standard",
        "evidence_provenance": "WorkerBee v1 MCP registration and static policy map",
    }
    if name in _READ_ONLY_LOW:
        return {
            **common,
            "mutation_intent": "read-only",
            "risk": "low",
            "state_scope": "none",
            "network_scope": "none",
            "requires_explicit_confirmation": False,
        }
    if name in _READ_ONLY_LOCAL_STATE:
        return {
            **common,
            "mutation_intent": "read-only",
            "risk": "low",
            "state_scope": "local-state",
            "network_scope": "none",
            "requires_explicit_confirmation": False,
        }
    if name in _READ_ONLY_NETWORK:
        return {
            **common,
            "mutation_intent": "read-only",
            "risk": "medium",
            "state_scope": "none",
            "network_scope": "bounded-probe",
            "requires_explicit_confirmation": False,
        }
    if name in _PROJECT_STATE:
        return {
            **common,
            "mutation_intent": "project-state",
            "risk": "medium",
            "state_scope": "workerbee-managed",
            "network_scope": "none",
            "requires_explicit_confirmation": False,
        }
    if name in _WORKLOAD_LIFECYCLE:
        return {
            **common,
            "mutation_intent": "workload-lifecycle",
            "risk": "high",
            "state_scope": "workerbee-managed-runtime",
            "network_scope": "local-runtime",
            "requires_explicit_confirmation": True,
        }
    if name in _HIGH_RISK:
        return {
            **common,
            "mutation_intent": "privileged-or-external",
            "risk": "critical",
            "state_scope": "host-or-external",
            "network_scope": "tool-specific",
            "requires_explicit_confirmation": True,
        }
    return None


def _diagnostic_error_code(diagnostics: Mapping[str, Any]) -> str | None:
    error = diagnostics.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None
