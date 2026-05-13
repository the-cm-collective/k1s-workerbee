import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from workerbee.contract import (
    AGENT_FEEDBACK_SCHEMA,
    API_VERSION,
    MCP_TOOL_NAMES,
    WorkerBeeError,
    fail,
    ok,
)
from workerbee.mcp_server import INGRESS_PROBE_INPUT_SCHEMA, _publish_explicit_tool_schema


def test_contract_tool_names_are_v1_only() -> None:
    assert MCP_TOOL_NAMES
    assert all(name.startswith("workerbee_v1_") for name in MCP_TOOL_NAMES)
    assert "workerbee_start" not in MCP_TOOL_NAMES
    assert "workerbee_v1_session_start" in MCP_TOOL_NAMES
    assert "workerbee_v1_ingress_status" in MCP_TOOL_NAMES
    assert "workerbee_v1_ingress_probe" in MCP_TOOL_NAMES
    assert "workerbee_v1_security_assess" in MCP_TOOL_NAMES
    assert "workerbee_v1_security_review_project" in MCP_TOOL_NAMES
    assert "workerbee_v1_secret_policy_status" in MCP_TOOL_NAMES
    assert "workerbee_v1_project_mode_set" in MCP_TOOL_NAMES


def test_result_envelopes_are_stable_and_mask_errors() -> None:
    success = ok(kind="Demo", project="alpha", data={"value": 1})
    assert success["api_version"] == API_VERSION
    assert success["ok"] is True
    assert success["error"] is None
    assert success["data"]["agent_feedback"]["schema"] == AGENT_FEEDBACK_SCHEMA

    error = fail(ValueError("bad token value"), kind="Demo", project="alpha")
    assert error["api_version"] == API_VERSION
    assert error["ok"] is False
    assert error["error"]["code"] == "VALIDATION_FAILED"
    feedback = error["data"]["agent_feedback"]
    assert feedback["severity"] == "error"
    assert "token" not in json.dumps(feedback).lower()


def test_agent_feedback_summarizes_status_with_safe_links_and_artifacts() -> None:
    result = ok(
        kind="ProjectStatus",
        project="alpha",
        data={
            "running": True,
            "mode": "start",
            "stack": {
                "dashboard_url": (
                    "https://app.alpha.workerbee.localhost:19443/dashboard?token=hidden#frag"
                )
            },
            "latest_deployment": {
                "id": "deploy-1",
                "stage_dir": "/var/lib/workerbee/stage",
                "secret_path": "/var/lib/workerbee/secret",
            },
        },
    )

    feedback = result["data"]["agent_feedback"]
    assert feedback["severity"] == "ok"
    assert feedback["summary"] == "WorkerBee project `alpha` is running."
    assert "https://app.alpha.workerbee.localhost:19443/dashboard" in feedback["links"]
    assert all("hidden" not in item for item in feedback["links"])
    assert "/var/lib/workerbee/stage" in feedback["artifacts"]
    assert "/var/lib/workerbee/secret" not in feedback["artifacts"]
    assert len(feedback["observations"]) <= 6
    assert len(feedback["next_actions"]) <= 6
    assert feedback["next_actions"][0]["tool"] == "workerbee_v1_logs"


def test_agent_feedback_recommends_diagnostics_for_probe_mismatch() -> None:
    result = ok(
        kind="IngressProbe",
        project="alpha",
        data={
            "ok": False,
            "url": "https://app.alpha.workerbee.localhost:19443/?token=hidden",
            "status": 503,
            "expected_status": 200,
            "status_matches": False,
            "body_matches": True,
        },
    )

    feedback = result["data"]["agent_feedback"]
    assert feedback["severity"] == "warning"
    assert "did not match expectations" in feedback["summary"]
    assert feedback["links"] == ["https://app.alpha.workerbee.localhost:19443/"]
    assert [item["tool"] for item in feedback["next_actions"]] == [
        "workerbee_v1_project_status",
        "workerbee_v1_logs",
    ]


def test_agent_feedback_calls_out_running_control_plane_without_workload() -> None:
    result = ok(
        kind="ProjectStart",
        project="alpha",
        data={"running": True, "app_status": {"state": "no_workload_deployed"}},
    )

    feedback = result["data"]["agent_feedback"]

    assert feedback["severity"] == "ok"
    assert "no app workload is deployed" in feedback["summary"]
    assert "app: no_workload_deployed" in feedback["observations"]


def test_agent_feedback_recommends_prune_for_orphaned_deploy() -> None:
    result = ok(
        kind="ManifestDeployLocal",
        project="alpha",
        data={
            "ok": True,
            "deployment": {"id": "deploy-1", "stage_dir": "/var/lib/workerbee/stage"},
            "app_status": {
                "state": "orphaned",
                "declared_workload_count": 1,
                "orphaned_workload_count": 1,
            },
        },
    )

    feedback = result["data"]["agent_feedback"]

    assert feedback["severity"] == "warning"
    assert "orphaned workloads" in feedback["summary"]
    assert feedback["next_actions"][1] == {
        "tool": "workerbee_v1_manifest_deploy_local",
        "args": {"project": "alpha", "stage": "/var/lib/workerbee/stage", "prune": True},
        "reason": "remove workloads from the previous stage that are absent now",
    }


def test_agent_feedback_prioritizes_app_ingress_links_for_deploy() -> None:
    result = ok(
        kind="ManifestDeployLocal",
        project="alpha",
        data={
            "ok": True,
            "events": [
                {
                    "dashboard_url": "https://k1s.alpha.workerbee.localhost:19443/dashboard",
                    "controller_url": "https://k1s-api.alpha.workerbee.localhost:19443/",
                }
            ],
            "app_status": {
                "state": "ready",
                "ingress_urls": ["https://rawform.alpha.workerbee.localhost:19443/"],
            },
            "deployment": {
                "ingress_urls": ["https://rawform.alpha.workerbee.localhost:19443/"]
            },
        },
    )

    feedback = result["data"]["agent_feedback"]

    assert feedback["links"][0] == "https://rawform.alpha.workerbee.localhost:19443/"
    assert "https://k1s.alpha.workerbee.localhost:19443/dashboard" in feedback["links"]


def test_exec_command_failure_feedback_surfaces_stderr() -> None:
    result = fail(
        WorkerBeeError(
            code="EXEC_COMMAND_FAILED",
            message="command failed in demo/api with exit code 2",
            details={
                "resolved_namespace": "demo",
                "resolved_app": "api",
                "returncode": 2,
                "stdout": "",
                "stderr": "missing bucket rawform-records\nfull detail",
            },
            remediation="Inspect command stderr/stdout, fix the command or workload, and retry.",
        ),
        kind="Exec",
        project="alpha",
    )

    assert result["error"]["code"] == "EXEC_COMMAND_FAILED"
    assert result["error"]["details"]["stderr"].startswith("missing bucket")
    feedback = result["data"]["agent_feedback"]
    assert feedback["summary"] == (
        "Exec for `alpha` failed with EXEC_COMMAND_FAILED: missing bucket rawform-records"
    )


def test_agent_feedback_observes_image_build_summary() -> None:
    result = ok(
        kind="ImageBuild",
        project="alpha",
        data={
            "ok": True,
            "tag": "workerbee-alpha-api:dev",
            "build_summary": {
                "line_count": 150,
                "warning_count": 2,
                "error_count": 0,
            },
        },
    )

    feedback = result["data"]["agent_feedback"]

    assert "build output: lines=150, warnings=2, errors=0" in feedback["observations"]


def test_ingress_probe_tool_schema_exposes_body_and_headers() -> None:
    mcp = FastMCP("workerbee-test")

    @mcp.tool()
    def workerbee_v1_ingress_probe(
        project: str = "default",
        json_body: dict[str, Any] | None = None,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        _ = (project, json_body, body, headers)
        return {}

    assert _publish_explicit_tool_schema(
        mcp,
        "workerbee_v1_ingress_probe",
        INGRESS_PROBE_INPUT_SCHEMA,
    )
    tool = mcp._tool_manager._tools["workerbee_v1_ingress_probe"]  # noqa: SLF001
    properties = tool.parameters["properties"]

    assert {"json_body", "body", "headers"}.issubset(properties)
    assert properties["headers"]["anyOf"][0]["additionalProperties"] == {"type": "string"}
