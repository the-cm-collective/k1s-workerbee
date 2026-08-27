from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path

from workerbee.complementary_architecture import (
    APPLICATION_PROFILE_API_VERSION,
    REQUIRED_MCP_RESOURCES,
    REQUIRED_PROBE_PACK_TYPES,
    REQUIRED_PROOF_PANELS,
    REQUIRED_TARGETS,
    REQUIRED_TRACE_PHASES,
    load_json_contract,
    mcp_resource_contract,
    validate_application_profile,
    validate_generic_ai_runtime_profile,
    validate_mcp_resource_contract,
    validate_operation_trace,
    validate_proof_surface,
    validate_runtime_probe_pack,
    validate_target_manager,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_FIXTURE = ROOT / "examples" / "complementary-architecture" / "application-profile.json"
TARGET_FIXTURE = ROOT / "examples" / "complementary-architecture" / "target-manager.json"
MCP_RESOURCE_FIXTURE = (
    ROOT / "examples" / "complementary-architecture" / "mcp-resource-contracts.json"
)
TRACE_FIXTURE = ROOT / "examples" / "complementary-architecture" / "operation-trace.json"
PROBE_PACK_FIXTURE = (
    ROOT / "examples" / "complementary-architecture" / "runtime-probe-pack.json"
)
GENERIC_AI_PROFILE_FIXTURE = (
    ROOT / "examples" / "complementary-architecture" / "generic-ai-runtime-profile.json"
)
PROOF_SUMMARY_FIXTURE = ROOT / "examples" / "complementary-architecture" / "proof-summary.json"
PROOF_HTML_FIXTURE = ROOT / "examples" / "complementary-architecture" / "proof-surface.html"


class _ProofHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.hrefs: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = tag
        attr_map = dict(attrs)
        if attr_map.get("id"):
            self.ids.add(str(attr_map["id"]))
        if attr_map.get("href"):
            self.hrefs.add(str(attr_map["href"]))

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text.append(data.strip())


def test_wbrr01_application_profile_fixture_is_valid() -> None:
    profile = load_json_contract(PROFILE_FIXTURE)

    result = validate_application_profile(profile)

    assert result["ok"] is True
    assert result["api_version"] == APPLICATION_PROFILE_API_VERSION
    assert result["service_names"] == ["api", "store", "worker"]
    assert result["service_count"] == 3


def test_wbrr01_profile_rejects_raw_secret_values() -> None:
    profile = load_json_contract(PROFILE_FIXTURE)
    profile["spec"]["services"][0]["secrets"][0]["value"] = "do-not-store"

    result = validate_application_profile(profile)

    assert result["ok"] is False
    assert any("raw secret values" in error["message"] for error in result["errors"])


def test_wbrr01_profile_requires_dependency_targets_to_exist() -> None:
    profile = load_json_contract(PROFILE_FIXTURE)
    profile["spec"]["services"][0]["dependencies"].append(
        {"service": "missing", "required": True}
    )

    result = validate_application_profile(profile)

    assert result["ok"] is False
    assert any("unknown service missing" in error["message"] for error in result["errors"])


def test_wbrr01_profile_requires_supported_exports_and_ports() -> None:
    profile = deepcopy(load_json_contract(PROFILE_FIXTURE))
    profile["spec"]["export"]["formats"].append("compose")
    profile["spec"]["services"][0]["ports"][0]["container_port"] = 70000

    result = validate_application_profile(profile)

    assert result["ok"] is False
    messages = {error["message"] for error in result["errors"]}
    assert "unsupported export format compose" in messages
    assert "container port must be between 1 and 65535" in messages


def test_wbrr02_target_manager_fixture_is_valid() -> None:
    manager = load_json_contract(TARGET_FIXTURE)

    result = validate_target_manager(manager)

    assert result["ok"] is True
    assert set(result["target_names"]) == REQUIRED_TARGETS
    assert result["target_count"] == len(REQUIRED_TARGETS)


def test_wbrr02_export_only_target_cannot_mutate_live_state() -> None:
    manager = load_json_contract(TARGET_FIXTURE)
    export_only = next(
        target for target in manager["spec"]["targets"] if target["name"] == "export-only"
    )
    export_only["allowed_mutations"].append("remote apply")

    result = validate_target_manager(manager)

    assert result["ok"] is False
    assert any("export-only target must not" in error["message"] for error in result["errors"])


def test_wbrr02_remote_k1s_forbids_cluster_scoped_webhook_mutation() -> None:
    manager = load_json_contract(TARGET_FIXTURE)
    remote = next(target for target in manager["spec"]["targets"] if target["name"] == "remote-k1s")
    remote["forbidden_mutations"].remove("cluster-scoped webhook")

    result = validate_target_manager(manager)

    assert result["ok"] is False
    assert any("cluster-scoped webhook" in error["message"] for error in result["errors"])


def test_wbrr03_mcp_resource_fixture_matches_static_contract() -> None:
    fixture = load_json_contract(MCP_RESOURCE_FIXTURE)

    assert fixture == mcp_resource_contract()
    result = validate_mcp_resource_contract(fixture)

    assert result["ok"] is True
    assert set(result["resource_names"]) == REQUIRED_MCP_RESOURCES


def test_wbrr03_mcp_resources_are_read_only() -> None:
    contract = mcp_resource_contract()
    resources = contract["spec"]["resources"]

    assert all(resource["read_only"] is True for resource in resources)
    assert all(resource["mutation_tool_refs"] for resource in resources)
    assert {
        "projects",
        "status",
        "routes",
        "security",
        "trace",
        "profiles",
        "targets",
    } == {resource["name"] for resource in resources}


def test_wbrr03_mcp_resource_validator_rejects_mutating_resource() -> None:
    contract = mcp_resource_contract()
    contract["spec"]["resources"][0]["read_only"] = False

    result = validate_mcp_resource_contract(contract)

    assert result["ok"] is False
    assert any("must be read-only" in error["message"] for error in result["errors"])


def test_wbrr04_operation_trace_fixture_is_valid() -> None:
    trace = load_json_contract(TRACE_FIXTURE)

    result = validate_operation_trace(trace)

    assert result["ok"] is True
    assert set(result["phases"]) == REQUIRED_TRACE_PHASES
    assert result["event_count"] == len(REQUIRED_TRACE_PHASES)


def test_wbrr04_operation_trace_requires_all_lifecycle_phases() -> None:
    trace = load_json_contract(TRACE_FIXTURE)
    trace["spec"]["timeline"] = [
        event for event in trace["spec"]["timeline"] if event["phase"] != "promotion"
    ]

    result = validate_operation_trace(trace)

    assert result["ok"] is False
    assert any("missing trace phase promotion" in error["message"] for error in result["errors"])


def test_wbrr04_operation_trace_rejects_raw_secret_retention() -> None:
    trace = load_json_contract(TRACE_FIXTURE)
    trace["spec"]["timeline"][0]["redaction"]["raw_secret_values_retained"] = True

    result = validate_operation_trace(trace)

    assert result["ok"] is False
    assert any(
        "raw secret values are not retained" in error["message"]
        for error in result["errors"]
    )


def test_wbrr05_runtime_probe_pack_fixture_is_valid() -> None:
    pack = load_json_contract(PROBE_PACK_FIXTURE)

    result = validate_runtime_probe_pack(pack)

    assert result["ok"] is True
    assert set(result["probe_types"]) == REQUIRED_PROBE_PACK_TYPES
    assert result["probe_count"] == len(REQUIRED_PROBE_PACK_TYPES)


def test_wbrr05_runtime_probe_pack_entries_are_non_mutating() -> None:
    pack = load_json_contract(PROBE_PACK_FIXTURE)
    pack["spec"]["probes"][0]["mutation"] = True

    result = validate_runtime_probe_pack(pack)

    assert result["ok"] is False
    assert any("non-mutating" in error["message"] for error in result["errors"])


def test_wbrr05_auth_negative_probe_requires_denial_expectations() -> None:
    pack = load_json_contract(PROBE_PACK_FIXTURE)
    auth_negative = next(
        probe for probe in pack["spec"]["probes"] if probe["type"] == "auth-negative"
    )
    auth_negative["denial_expectations"] = []

    result = validate_runtime_probe_pack(pack)

    assert result["ok"] is False
    assert any("denial expectations" in error["message"] for error in result["errors"])


def test_wbrr06_generic_ai_runtime_profile_fixture_is_valid() -> None:
    profile = load_json_contract(GENERIC_AI_PROFILE_FIXTURE)

    result = validate_generic_ai_runtime_profile(profile)

    assert result["ok"] is True
    assert result["roles"] == ["ai-router", "model-runtime", "queue", "vector-store"]


def test_wbrr06_generic_ai_runtime_profile_rejects_rocketride_branding() -> None:
    profile = load_json_contract(GENERIC_AI_PROFILE_FIXTURE)
    profile["metadata"]["description"] = "RocketRide-specific model serving profile"

    result = validate_generic_ai_runtime_profile(profile)

    assert result["ok"] is False
    assert any("branded term rocketride" in error["message"] for error in result["errors"])


def test_wbrr06_generic_ai_runtime_profile_requires_model_and_gpu_probe_metadata() -> None:
    profile = load_json_contract(GENERIC_AI_PROFILE_FIXTURE)
    model_runtime = next(
        service for service in profile["spec"]["services"] if service["name"] == "model-runtime"
    )
    model_runtime["probes"] = []

    result = validate_generic_ai_runtime_profile(profile)

    assert result["ok"] is False
    messages = {error["message"] for error in result["errors"]}
    assert "generic AI profile must include model probe metadata" in messages
    assert "generic AI profile must include gpu probe metadata" in messages


def test_wbrr07_proof_summary_fixture_is_valid() -> None:
    summary = load_json_contract(PROOF_SUMMARY_FIXTURE)

    result = validate_proof_surface(summary)

    assert result["ok"] is True
    assert set(result["panel_names"]) == REQUIRED_PROOF_PANELS
    assert result["panel_count"] == len(REQUIRED_PROOF_PANELS)


def test_wbrr07_proof_summary_rejects_missing_panel() -> None:
    summary = load_json_contract(PROOF_SUMMARY_FIXTURE)
    summary["spec"]["panels"] = [
        panel for panel in summary["spec"]["panels"] if panel["name"] != "security_findings"
    ]

    result = validate_proof_surface(summary)

    assert result["ok"] is False
    assert any(
        "missing proof panel security_findings" in error["message"]
        for error in result["errors"]
    )


def test_wbrr07_static_html_includes_required_review_panels() -> None:
    html = PROOF_HTML_FIXTURE.read_text(encoding="utf-8")
    parser = _ProofHtmlParser()
    parser.feed(html)
    text = " ".join(parser.text).lower()

    assert {
        "target-manager",
        "profile-topology",
        "operation-trace",
        "probe-results",
        "security-findings",
        "export-promotion-state",
    }.issubset(parser.ids)
    assert "target manager" in text
    assert "profile topology" in text
    assert "operation trace" in text
    assert "probe results" in text
    assert "security findings" in text
    assert "export and promotion" in text
    assert {
        "proof-summary.json",
        "target-manager.json",
        "application-profile.json",
        "generic-ai-runtime-profile.json",
        "operation-trace.json",
        "runtime-probe-pack.json",
        "mcp-resource-contracts.json",
    }.issubset(parser.hrefs)
