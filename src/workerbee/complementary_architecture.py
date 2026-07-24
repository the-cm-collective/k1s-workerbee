"""WorkerBee complementary architecture contracts.

The helpers in this module are data contracts for docs, fixtures, MCP resources,
and proof surfaces. They do not start projects, deploy workloads, or mutate
runtime state.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

APPLICATION_PROFILE_API_VERSION = "workerbee.application-profile/v1"
APPLICATION_PROFILE_KIND = "WorkerBeeApplicationProfile"
TARGET_MANAGER_API_VERSION = "workerbee.target-manager/v1"
TARGET_MANAGER_KIND = "WorkerBeeTargetManager"
MCP_RESOURCE_CONTRACT_API_VERSION = "workerbee.mcp-resource-contract/v1"
MCP_RESOURCE_CONTRACT_KIND = "WorkerBeeMCPResourceContract"
OPERATION_TRACE_API_VERSION = "workerbee.operation-trace/v1"
OPERATION_TRACE_KIND = "WorkerBeeOperationTrace"
RUNTIME_PROBE_PACK_API_VERSION = "workerbee.runtime-probe-pack/v1"
RUNTIME_PROBE_PACK_KIND = "WorkerBeeRuntimeProbePack"
PROOF_SURFACE_API_VERSION = "workerbee.complementary-proof/v1"
PROOF_SURFACE_KIND = "WorkerBeeComplementaryProof"

VALID_PROTOCOLS = {"http", "https", "grpc", "tcp", "udp", "websocket"}
VALID_EXPORT_FORMATS = {"k1s", "k8s", "helm"}
VALID_PROBE_TYPES = {
    "http",
    "tcp",
    "websocket",
    "command",
    "callback",
    "queue",
    "long-running",
    "model",
    "gpu",
    "auth-negative",
}
REQUIRED_TARGETS = {
    "local-dev",
    "local-validation",
    "remote-k1s",
    "openstack-k1s-env",
    "export-only",
}
REQUIRED_MCP_RESOURCES = {
    "projects",
    "status",
    "routes",
    "security",
    "trace",
    "profiles",
    "targets",
}
REQUIRED_TRACE_PHASES = {
    "session",
    "build",
    "manifest",
    "validation",
    "deploy",
    "route",
    "probe",
    "security",
    "export",
    "promotion",
}
REQUIRED_PROBE_PACK_TYPES = {
    "websocket",
    "callback",
    "queue",
    "long-running",
    "model",
    "gpu",
    "auth-negative",
}
FORBIDDEN_CORE_BRANDING_TERMS = {"rocketride"}
REQUIRED_PROOF_PANELS = {
    "target_manager",
    "profile_topology",
    "operation_trace",
    "probe_results",
    "security_findings",
    "export_promotion_state",
}


def load_json_contract(path: Path) -> dict[str, Any]:
    """Load a JSON contract document from disk."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"contract document must be a JSON object: {path}")
    return payload


def validate_application_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Validate a WorkerBee application profile contract."""

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    _expect_equal(
        errors,
        profile.get("api_version"),
        APPLICATION_PROFILE_API_VERSION,
        "api_version",
    )
    _expect_equal(errors, profile.get("kind"), APPLICATION_PROFILE_KIND, "kind")

    metadata = _object(profile.get("metadata"))
    spec = _object(profile.get("spec"))
    if not str(metadata.get("name") or "").strip():
        errors.append({"field": "metadata.name", "message": "profile name is required"})

    services = _list(spec.get("services"))
    if not services:
        errors.append({"field": "spec.services", "message": "at least one service is required"})
    service_names: set[str] = set()
    for index, service in enumerate(services):
        if not isinstance(service, dict):
            errors.append(
                {"field": f"spec.services[{index}]", "message": "service must be an object"}
            )
            continue
        name = str(service.get("name") or "").strip()
        if not name:
            errors.append(
                {"field": f"spec.services[{index}].name", "message": "service name is required"}
            )
        elif name in service_names:
            errors.append(
                {"field": f"spec.services[{index}].name", "message": f"duplicate service {name}"}
            )
        service_names.add(name)
        if not str(service.get("role") or "").strip():
            errors.append(
                {"field": f"spec.services[{index}].role", "message": "service role is required"}
            )
        if not str(service.get("image") or "").strip():
            errors.append(
                {"field": f"spec.services[{index}].image", "message": "service image is required"}
            )
        _validate_ports(errors, service, index)
        _validate_protocols(errors, service, index)
        _validate_dependencies(errors, service, index)
        _validate_secret_refs(errors, service, index)
        _validate_probes(errors, service, index)
        _validate_gpu_hints(errors, service, index)

    _validate_export(errors, spec)
    _validate_dependencies_resolve(errors, services, service_names)

    if not _list(spec.get("probes")) and not any(
        _list(service.get("probes")) for service in services if isinstance(service, dict)
    ):
        warnings.append({"field": "spec.probes", "message": "no probes declared"})

    return {
        "ok": not errors,
        "api_version": APPLICATION_PROFILE_API_VERSION,
        "kind": APPLICATION_PROFILE_KIND,
        "errors": errors,
        "warnings": warnings,
        "service_count": len(services),
        "service_names": sorted(service_names),
    }


def validate_target_manager(manager: dict[str, Any]) -> dict[str, Any]:
    """Validate target manager records and mutation boundaries."""

    errors: list[dict[str, Any]] = []
    _expect_equal(errors, manager.get("api_version"), TARGET_MANAGER_API_VERSION, "api_version")
    _expect_equal(errors, manager.get("kind"), TARGET_MANAGER_KIND, "kind")

    targets = _list(_object(manager.get("spec")).get("targets"))
    by_name: dict[str, dict[str, Any]] = {}
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            errors.append({"field": f"spec.targets[{index}]", "message": "target must be an object"})
            continue
        name = str(target.get("name") or "").strip()
        if not name:
            errors.append(
                {"field": f"spec.targets[{index}].name", "message": "target name is required"}
            )
            continue
        if name in by_name:
            errors.append(
                {"field": f"spec.targets[{index}].name", "message": f"duplicate target {name}"}
            )
        by_name[name] = target
        _validate_target_record(errors, target, index)

    missing = sorted(REQUIRED_TARGETS - set(by_name))
    for name in missing:
        errors.append({"field": "spec.targets", "message": f"missing required target {name}"})

    export_only = by_name.get("export-only")
    if export_only and _list(export_only.get("allowed_mutations")):
        errors.append(
            {
                "field": "spec.targets[export-only].allowed_mutations",
                "message": "export-only target must not declare live mutations",
            }
        )

    remote = by_name.get("remote-k1s")
    if remote and "cluster-scoped webhook" not in _list(remote.get("forbidden_mutations")):
        errors.append(
            {
                "field": "spec.targets[remote-k1s].forbidden_mutations",
                "message": "remote-k1s boundary must forbid cluster-scoped webhook mutation",
            }
        )

    return {
        "ok": not errors,
        "api_version": TARGET_MANAGER_API_VERSION,
        "kind": TARGET_MANAGER_KIND,
        "errors": errors,
        "target_names": sorted(by_name),
        "target_count": len(by_name),
    }


def mcp_resource_contract() -> dict[str, Any]:
    """Return the static WorkerBee read-only MCP resource contract."""

    resources = [
        {
            "name": "projects",
            "uri": "workerbee://projects/v1",
            "read_only": True,
            "describes": "known project identities, cwd, branch, mode, and dashboard links",
            "mutation_tool_refs": [
                "workerbee_v1_project_start",
                "workerbee_v1_project_stop",
                "workerbee_v1_project_mode_set",
                "workerbee_v1_project_reset",
            ],
        },
        {
            "name": "status",
            "uri": "workerbee://projects/{project}/status/v1",
            "read_only": True,
            "describes": "project stack, workload, ingress, runbook, and mode status",
            "mutation_tool_refs": ["workerbee_v1_project_start", "workerbee_v1_manifest_deploy_local"],
        },
        {
            "name": "routes",
            "uri": "workerbee://projects/{project}/routes/v1",
            "read_only": True,
            "describes": "WorkerBee-managed dashboard, API, docs, and workload ingress routes",
            "mutation_tool_refs": ["workerbee_v1_manifest_deploy_local", "workerbee_v1_ingress_ca_regenerate"],
        },
        {
            "name": "security",
            "uri": "workerbee://projects/{project}/security/v1",
            "read_only": True,
            "describes": "secret policy state, advisory security findings, and hardening metadata",
            "mutation_tool_refs": ["workerbee_v1_security_assess", "workerbee_v1_security_review_project"],
        },
        {
            "name": "trace",
            "uri": "workerbee://projects/{project}/trace/v1",
            "read_only": True,
            "describes": "per-project operation timeline and evidence references",
            "mutation_tool_refs": [
                "workerbee_v1_session_start",
                "workerbee_v1_image_build",
                "workerbee_v1_manifest_deploy_local",
                "workerbee_v1_bundle_export",
            ],
        },
        {
            "name": "profiles",
            "uri": "workerbee://profiles/v1",
            "read_only": True,
            "describes": "built-in k1s profile shapes and runtime requirements",
            "mutation_tool_refs": [
                "workerbee_v1_profile_start",
                "workerbee_v1_profile_stop",
                "workerbee_v1_profile_workload_validate",
            ],
        },
        {
            "name": "targets",
            "uri": "workerbee://targets/v1",
            "read_only": True,
            "describes": (
                "target manager mutation boundaries for local, remote, "
                "OpenStack-compatible k1s, and export lanes"
            ),
            "mutation_tool_refs": [
                "workerbee_v1_manifest_deploy_local",
                "workerbee_v1_manifest_deploy_remote_k1s",
                "workerbee_v1_bundle_export",
            ],
        },
    ]
    return {
        "api_version": MCP_RESOURCE_CONTRACT_API_VERSION,
        "kind": MCP_RESOURCE_CONTRACT_KIND,
        "metadata": {
            "name": "workerbee-read-only-resource-contracts",
            "description": "Read-only MCP resource contracts with bounded mutation tool references.",
        },
        "spec": {
            "resources": resources,
            "mutation_model": (
                "Resources expose read-only project context. Mutations remain behind explicit, "
                "bounded WorkerBee tools with project/target scope."
            ),
        },
    }


def validate_mcp_resource_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate read-only MCP resource contract records."""

    errors: list[dict[str, Any]] = []
    _expect_equal(
        errors,
        contract.get("api_version"),
        MCP_RESOURCE_CONTRACT_API_VERSION,
        "api_version",
    )
    _expect_equal(errors, contract.get("kind"), MCP_RESOURCE_CONTRACT_KIND, "kind")
    resources = _list(_object(contract.get("spec")).get("resources"))
    by_name: dict[str, dict[str, Any]] = {}
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict):
            errors.append(
                {"field": f"spec.resources[{index}]", "message": "resource must be an object"}
            )
            continue
        name = str(resource.get("name") or "").strip()
        uri = str(resource.get("uri") or "").strip()
        if not name:
            errors.append(
                {"field": f"spec.resources[{index}].name", "message": "resource name is required"}
            )
        elif name in by_name:
            errors.append(
                {"field": f"spec.resources[{index}].name", "message": f"duplicate resource {name}"}
            )
        by_name[name] = resource
        if not uri.startswith("workerbee://"):
            errors.append(
                {
                    "field": f"spec.resources[{index}].uri",
                    "message": "resource uri must use workerbee://",
                }
            )
        if resource.get("read_only") is not True:
            errors.append(
                {
                    "field": f"spec.resources[{index}].read_only",
                    "message": "MCP resources must be read-only",
                }
            )
        if not _list(resource.get("mutation_tool_refs")):
            errors.append(
                {
                    "field": f"spec.resources[{index}].mutation_tool_refs",
                    "message": "resource must point to bounded mutation tools",
                }
            )
    for name in sorted(REQUIRED_MCP_RESOURCES - set(by_name)):
        errors.append({"field": "spec.resources", "message": f"missing required resource {name}"})
    return {
        "ok": not errors,
        "api_version": MCP_RESOURCE_CONTRACT_API_VERSION,
        "kind": MCP_RESOURCE_CONTRACT_KIND,
        "errors": errors,
        "resource_names": sorted(by_name),
        "resource_count": len(by_name),
    }


def validate_operation_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Validate a per-project operation trace timeline."""

    errors: list[dict[str, Any]] = []
    _expect_equal(errors, trace.get("api_version"), OPERATION_TRACE_API_VERSION, "api_version")
    _expect_equal(errors, trace.get("kind"), OPERATION_TRACE_KIND, "kind")
    metadata = _object(trace.get("metadata"))
    spec = _object(trace.get("spec"))
    if not str(metadata.get("project") or "").strip():
        errors.append({"field": "metadata.project", "message": "project is required"})
    events = _list(spec.get("timeline"))
    phases: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append({"field": f"spec.timeline[{index}]", "message": "event must be an object"})
            continue
        phase = str(event.get("phase") or "").strip()
        if phase:
            phases.add(phase)
        _validate_trace_event(errors, event, index)
    for phase in sorted(REQUIRED_TRACE_PHASES - phases):
        errors.append({"field": "spec.timeline", "message": f"missing trace phase {phase}"})
    return {
        "ok": not errors,
        "api_version": OPERATION_TRACE_API_VERSION,
        "kind": OPERATION_TRACE_KIND,
        "errors": errors,
        "phases": sorted(phases),
        "event_count": len(events),
    }


def validate_runtime_probe_pack(pack: dict[str, Any]) -> dict[str, Any]:
    """Validate a runtime probe pack contract."""

    errors: list[dict[str, Any]] = []
    _expect_equal(
        errors,
        pack.get("api_version"),
        RUNTIME_PROBE_PACK_API_VERSION,
        "api_version",
    )
    _expect_equal(errors, pack.get("kind"), RUNTIME_PROBE_PACK_KIND, "kind")
    probes = _list(_object(pack.get("spec")).get("probes"))
    by_type: dict[str, dict[str, Any]] = {}
    for index, probe in enumerate(probes):
        if not isinstance(probe, dict):
            errors.append({"field": f"spec.probes[{index}]", "message": "probe must be an object"})
            continue
        name = str(probe.get("name") or "").strip()
        probe_type = str(probe.get("type") or "").strip()
        if not name:
            errors.append(
                {"field": f"spec.probes[{index}].name", "message": "probe name is required"}
            )
        if probe_type not in REQUIRED_PROBE_PACK_TYPES:
            errors.append(
                {
                    "field": f"spec.probes[{index}].type",
                    "message": f"unsupported probe pack type {probe_type or '<missing>'}",
                }
            )
        elif probe_type in by_type:
            errors.append(
                {
                    "field": f"spec.probes[{index}].type",
                    "message": f"duplicate probe pack type {probe_type}",
                }
            )
        else:
            by_type[probe_type] = probe
        if probe.get("mutation") is not False:
            errors.append(
                {
                    "field": f"spec.probes[{index}].mutation",
                    "message": "probe pack entries must be non-mutating by default",
                }
            )
        if not isinstance(probe.get("evidence"), dict):
            errors.append(
                {
                    "field": f"spec.probes[{index}].evidence",
                    "message": "probe evidence contract is required",
                }
            )
        if probe_type == "auth-negative" and not _list(probe.get("denial_expectations")):
            errors.append(
                {
                    "field": f"spec.probes[{index}].denial_expectations",
                    "message": "auth-negative probe must declare denial expectations",
                }
            )
    for probe_type in sorted(REQUIRED_PROBE_PACK_TYPES - set(by_type)):
        errors.append({"field": "spec.probes", "message": f"missing probe type {probe_type}"})
    return {
        "ok": not errors,
        "api_version": RUNTIME_PROBE_PACK_API_VERSION,
        "kind": RUNTIME_PROBE_PACK_KIND,
        "errors": errors,
        "probe_types": sorted(by_type),
        "probe_count": len(probes),
    }


def validate_generic_ai_runtime_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Validate the generic AI runtime sample profile."""

    base = validate_application_profile(profile)
    errors = list(base["errors"])
    text = json.dumps(profile, sort_keys=True).lower()
    for term in sorted(FORBIDDEN_CORE_BRANDING_TERMS):
        if term in text:
            errors.append(
                {
                    "field": "<document>",
                    "message": f"generic core profile must not contain branded term {term}",
                }
            )
    services = _list(_object(profile.get("spec")).get("services"))
    roles = {str(service.get("role") or "") for service in services if isinstance(service, dict)}
    required_roles = {"ai-router", "model-runtime", "vector-store", "queue"}
    for role in sorted(required_roles - roles):
        errors.append({"field": "spec.services[].role", "message": f"missing AI role {role}"})
    if not any(
        isinstance(service, dict)
        and _object(service.get("gpu_hints")).get("required") is True
        for service in services
    ):
        errors.append(
            {
                "field": "spec.services[].gpu_hints.required",
                "message": "generic AI profile must include at least one GPU-capable model service",
            }
        )
    probe_types = {
        str(probe.get("type") or "")
        for service in services
        if isinstance(service, dict)
        for probe in _list(service.get("probes"))
        if isinstance(probe, dict)
    }
    for probe_type in ("model", "gpu"):
        if probe_type not in probe_types:
            errors.append(
                {
                    "field": "spec.services[].probes[].type",
                    "message": f"generic AI profile must include {probe_type} probe metadata",
                }
            )
    return {
        **base,
        "ok": not errors,
        "errors": errors,
        "required_roles": sorted(required_roles),
        "roles": sorted(roles),
    }


def validate_proof_surface(surface: dict[str, Any]) -> dict[str, Any]:
    """Validate a static WorkerBee complementary proof surface summary."""

    errors: list[dict[str, Any]] = []
    _expect_equal(errors, surface.get("api_version"), PROOF_SURFACE_API_VERSION, "api_version")
    _expect_equal(errors, surface.get("kind"), PROOF_SURFACE_KIND, "kind")
    spec = _object(surface.get("spec"))
    panels = _list(spec.get("panels"))
    by_name: dict[str, dict[str, Any]] = {}
    for index, panel in enumerate(panels):
        if not isinstance(panel, dict):
            errors.append({"field": f"spec.panels[{index}]", "message": "panel must be an object"})
            continue
        name = str(panel.get("name") or "").strip()
        if not name:
            errors.append(
                {"field": f"spec.panels[{index}].name", "message": "panel name is required"}
            )
            continue
        by_name[name] = panel
        if not str(panel.get("status") or "").strip():
            errors.append(
                {"field": f"spec.panels[{index}].status", "message": "panel status is required"}
            )
        if not _list(panel.get("source_refs")):
            errors.append(
                {
                    "field": f"spec.panels[{index}].source_refs",
                    "message": "panel must reference source evidence",
                }
            )
        if not str(panel.get("summary") or "").strip():
            errors.append(
                {"field": f"spec.panels[{index}].summary", "message": "panel summary is required"}
            )
    for panel in sorted(REQUIRED_PROOF_PANELS - set(by_name)):
        errors.append({"field": "spec.panels", "message": f"missing proof panel {panel}"})
    if spec.get("raw_secret_values_retained") is not False:
        errors.append(
            {
                "field": "spec.raw_secret_values_retained",
                "message": "proof surface must not retain raw secret values",
            }
        )
    return {
        "ok": not errors,
        "api_version": PROOF_SURFACE_API_VERSION,
        "kind": PROOF_SURFACE_KIND,
        "errors": errors,
        "panel_names": sorted(by_name),
        "panel_count": len(by_name),
    }


def _validate_trace_event(
    errors: list[dict[str, Any]],
    event: dict[str, Any],
    index: int,
) -> None:
    for field in ("id", "phase", "event", "status", "timestamp"):
        if not str(event.get(field) or "").strip():
            errors.append(
                {"field": f"spec.timeline[{index}].{field}", "message": f"{field} is required"}
            )
    if str(event.get("phase") or "") not in REQUIRED_TRACE_PHASES:
        errors.append(
            {
                "field": f"spec.timeline[{index}].phase",
                "message": f"unsupported trace phase {event.get('phase')!r}",
            }
        )
    if event.get("mutation") not in {True, False}:
        errors.append(
            {
                "field": f"spec.timeline[{index}].mutation",
                "message": "mutation must be boolean",
            }
        )
    if not isinstance(event.get("evidence_refs"), list):
        errors.append(
            {
                "field": f"spec.timeline[{index}].evidence_refs",
                "message": "evidence_refs must be a list",
            }
        )
    redaction = _object(event.get("redaction"))
    if redaction.get("raw_secret_values_retained") is not False:
        errors.append(
            {
                "field": f"spec.timeline[{index}].redaction.raw_secret_values_retained",
                "message": "trace events must prove raw secret values are not retained",
            }
        )


def _validate_target_record(
    errors: list[dict[str, Any]],
    target: dict[str, Any],
    index: int,
) -> None:
    for field in ("target_type", "runtime_owner", "mutation_boundary"):
        if not str(target.get(field) or "").strip():
            errors.append(
                {
                    "field": f"spec.targets[{index}].{field}",
                    "message": f"{field} is required",
                }
            )
    for field in ("allowed_mutations", "forbidden_mutations", "evidence_surfaces"):
        if not isinstance(target.get(field), list):
            errors.append(
                {
                    "field": f"spec.targets[{index}].{field}",
                    "message": f"{field} must be a list",
                }
            )
    if not isinstance(target.get("requires_confirmation", False), bool):
        errors.append(
            {
                "field": f"spec.targets[{index}].requires_confirmation",
                "message": "requires_confirmation must be boolean",
            }
        )


def _validate_ports(errors: list[dict[str, Any]], service: dict[str, Any], index: int) -> None:
    ports = _list(service.get("ports"))
    if not ports:
        errors.append(
            {"field": f"spec.services[{index}].ports", "message": "service must expose ports"}
        )
        return
    seen: set[str] = set()
    for port_index, port in enumerate(ports):
        if not isinstance(port, dict):
            errors.append(
                {
                    "field": f"spec.services[{index}].ports[{port_index}]",
                    "message": "port must be an object",
                }
            )
            continue
        name = str(port.get("name") or "").strip()
        if not name:
            errors.append(
                {
                    "field": f"spec.services[{index}].ports[{port_index}].name",
                    "message": "port name is required",
                }
            )
        elif name in seen:
            errors.append(
                {
                    "field": f"spec.services[{index}].ports[{port_index}].name",
                    "message": f"duplicate port {name}",
                }
            )
        seen.add(name)
        try:
            container_port = int(port.get("container_port"))
        except (TypeError, ValueError):
            container_port = 0
        if container_port < 1 or container_port > 65535:
            errors.append(
                {
                    "field": f"spec.services[{index}].ports[{port_index}].container_port",
                    "message": "container port must be between 1 and 65535",
                }
            )


def _validate_protocols(errors: list[dict[str, Any]], service: dict[str, Any], index: int) -> None:
    protocols = _list(service.get("protocols"))
    if not protocols:
        errors.append(
            {
                "field": f"spec.services[{index}].protocols",
                "message": "service must declare protocols",
            }
        )
        return
    for protocol in protocols:
        if str(protocol).lower() not in VALID_PROTOCOLS:
            errors.append(
                {
                    "field": f"spec.services[{index}].protocols",
                    "message": f"unsupported protocol {protocol}",
                }
            )


def _validate_dependencies(errors: list[dict[str, Any]], service: dict[str, Any], index: int) -> None:
    for dep_index, dependency in enumerate(_list(service.get("dependencies"))):
        if not isinstance(dependency, dict) or not str(dependency.get("service") or "").strip():
            errors.append(
                {
                    "field": f"spec.services[{index}].dependencies[{dep_index}]",
                    "message": "dependency must name a service",
                }
            )


def _validate_dependencies_resolve(
    errors: list[dict[str, Any]],
    services: list[Any],
    service_names: set[str],
) -> None:
    for index, service in enumerate(services):
        if not isinstance(service, dict):
            continue
        service_name = str(service.get("name") or "")
        for dep_index, dependency in enumerate(_list(service.get("dependencies"))):
            if not isinstance(dependency, dict):
                continue
            target = str(dependency.get("service") or "").strip()
            if target and target not in service_names:
                errors.append(
                    {
                        "field": f"spec.services[{index}].dependencies[{dep_index}].service",
                        "message": f"{service_name} depends on unknown service {target}",
                    }
                )


def _validate_secret_refs(errors: list[dict[str, Any]], service: dict[str, Any], index: int) -> None:
    for secret_index, secret in enumerate(_list(service.get("secrets"))):
        if not isinstance(secret, dict):
            errors.append(
                {
                    "field": f"spec.services[{index}].secrets[{secret_index}]",
                    "message": "secret reference must be an object",
                }
            )
            continue
        if any(key in secret for key in ("value", "raw", "private_key", "token")):
            errors.append(
                {
                    "field": f"spec.services[{index}].secrets[{secret_index}]",
                    "message": "secret references must not retain raw secret values",
                }
            )
        if not str(secret.get("name") or "").strip():
            errors.append(
                {
                    "field": f"spec.services[{index}].secrets[{secret_index}].name",
                    "message": "secret name is required",
                }
            )


def _validate_probes(errors: list[dict[str, Any]], service: dict[str, Any], index: int) -> None:
    for probe_index, probe in enumerate(_list(service.get("probes"))):
        if not isinstance(probe, dict):
            errors.append(
                {
                    "field": f"spec.services[{index}].probes[{probe_index}]",
                    "message": "probe must be an object",
                }
            )
            continue
        probe_type = str(probe.get("type") or "").strip()
        if probe_type not in VALID_PROBE_TYPES:
            errors.append(
                {
                    "field": f"spec.services[{index}].probes[{probe_index}].type",
                    "message": f"unsupported probe type {probe_type or '<missing>'}",
                }
            )


def _validate_gpu_hints(errors: list[dict[str, Any]], service: dict[str, Any], index: int) -> None:
    hints = service.get("gpu_hints")
    if hints is None:
        return
    if not isinstance(hints, dict):
        errors.append(
            {
                "field": f"spec.services[{index}].gpu_hints",
                "message": "gpu hints must be an object",
            }
        )
        return
    if not isinstance(hints.get("required", False), bool):
        errors.append(
            {
                "field": f"spec.services[{index}].gpu_hints.required",
                "message": "gpu required hint must be boolean",
            }
        )


def _validate_export(errors: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    export = spec.get("export")
    if not isinstance(export, dict):
        errors.append({"field": "spec.export", "message": "export behavior is required"})
        return
    formats = _list(export.get("formats"))
    if not formats:
        errors.append({"field": "spec.export.formats", "message": "export formats are required"})
    for fmt in formats:
        if str(fmt) not in VALID_EXPORT_FORMATS:
            errors.append(
                {"field": "spec.export.formats", "message": f"unsupported export format {fmt}"}
            )
    if export.get("include_secret_values") is True:
        errors.append(
            {
                "field": "spec.export.include_secret_values",
                "message": "exports must not include raw secret values",
            }
        )


def _expect_equal(
    errors: list[dict[str, Any]],
    actual: Any,
    expected: str,
    field: str,
) -> None:
    if actual != expected:
        errors.append({"field": field, "message": f"expected {expected}, got {actual!r}"})


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _all_names(items: Iterable[dict[str, Any]]) -> list[str]:
    return sorted(str(item.get("name") or "") for item in items if str(item.get("name") or ""))
