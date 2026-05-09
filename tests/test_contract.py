from typing import Any

from mcp.server.fastmcp import FastMCP

from workerbee.contract import API_VERSION, MCP_TOOL_NAMES, fail, ok
from workerbee.mcp_server import INGRESS_PROBE_INPUT_SCHEMA, _publish_explicit_tool_schema


def test_contract_tool_names_are_v1_only() -> None:
    assert MCP_TOOL_NAMES
    assert all(name.startswith("workerbee_v1_") for name in MCP_TOOL_NAMES)
    assert "workerbee_start" not in MCP_TOOL_NAMES
    assert "workerbee_v1_session_start" in MCP_TOOL_NAMES
    assert "workerbee_v1_ingress_probe" in MCP_TOOL_NAMES
    assert "workerbee_v1_project_mode_set" in MCP_TOOL_NAMES


def test_result_envelopes_are_stable_and_mask_errors() -> None:
    success = ok(kind="Demo", project="alpha", data={"value": 1})
    assert success["api_version"] == API_VERSION
    assert success["ok"] is True
    assert success["error"] is None

    error = fail(ValueError("bad token value"), kind="Demo", project="alpha")
    assert error["api_version"] == API_VERSION
    assert error["ok"] is False
    assert error["error"]["code"] == "VALIDATION_FAILED"


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
