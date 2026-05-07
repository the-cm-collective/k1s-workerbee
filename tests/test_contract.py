from workerbee.contract import API_VERSION, MCP_TOOL_NAMES, fail, ok


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
