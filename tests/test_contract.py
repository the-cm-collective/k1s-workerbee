import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from workerbee.contract import AGENT_FEEDBACK_SCHEMA, API_VERSION, MCP_TOOL_NAMES, fail, ok
from workerbee.mcp_server import INGRESS_PROBE_INPUT_SCHEMA, _publish_explicit_tool_schema


def test_contract_tool_names_are_v1_only() -> None:
    assert MCP_TOOL_NAMES
    assert all(name.startswith("workerbee_v1_") for name in MCP_TOOL_NAMES)
    assert "workerbee_start" not in MCP_TOOL_NAMES
    assert "workerbee_v1_session_start" in MCP_TOOL_NAMES
    assert "workerbee_v1_ingress_probe" in MCP_TOOL_NAMES
    assert "workerbee_v1_security_assess" in MCP_TOOL_NAMES
    assert "workerbee_v1_security_review_project" in MCP_TOOL_NAMES
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
