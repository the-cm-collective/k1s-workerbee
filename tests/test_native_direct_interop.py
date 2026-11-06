from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from workerbee.agent import (
    NATIVE_DIRECT_BINDING_SCHEMA,
    NATIVE_INTEROPERABILITY_CAPABILITIES_SCHEMA,
    derive_session_project_info,
    native_direct_binding,
    native_interoperability_capabilities,
)
from workerbee.contract import MCP_TOOL_NAMES, WorkerBeeError
from workerbee.controlled_runs import CONTROLLED_RUN_TOOL_NAMES, ControlledRunService
from workerbee.daemon import WorkerBeeDaemon
from workerbee.k1s_runtime import K1sRuntime

NATIVE_LOCAL_TOOLS = {
    "workerbee_v1_session_start",
    "workerbee_v1_project_mode_get",
    "workerbee_v1_project_mode_set",
    "workerbee_v1_project_start",
    "workerbee_v1_project_status",
    "workerbee_v1_project_stop",
    "workerbee_v1_logs",
    "workerbee_v1_exec",
    "workerbee_v1_ingress_status",
    "workerbee_v1_ingress_probe",
    "workerbee_v1_image_build",
    "workerbee_v1_manifest_prepare",
    "workerbee_v1_manifest_validate",
    "workerbee_v1_manifest_deploy_local",
}


def _runtime() -> K1sRuntime:
    return K1sRuntime(
        source="installed",
        python_executable="/usr/bin/python",
        k1s_root=None,
        pythonpath=None,
        ae_origin="/site-packages/ae/__init__.py",
    )


def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workerbee.supervisor.resolve_k1s_runtime", lambda **_: _runtime())


def test_native_interoperability_capabilities_are_orthogonal_and_fail_closed() -> None:
    capabilities = native_interoperability_capabilities()
    encoded = json.dumps(capabilities, sort_keys=True, separators=(",", ":")).encode("utf-8")

    assert capabilities == native_interoperability_capabilities()
    assert len(encoded) < 4096
    assert capabilities["schema"] == NATIVE_INTEROPERABILITY_CAPABILITIES_SCHEMA
    assert capabilities["version"] == 1
    assert capabilities["default_binding"] == {
        "source_mode": "native-direct",
        "execution_lane": "native-local",
    }
    assert capabilities["source_modes"]["native-direct"] == {
        "supported": True,
        "owner": "workerbee-oss",
        "uses_exact_host_checkout": True,
    }
    assert capabilities["source_binding_modes"]["trusted-direct-bind"] == {
        "supported": False,
        "owner": "platform-debug-adapter",
    }
    assert capabilities["access_profile_contract"] == {
        "owner": "agent-pbx",
        "workerbee_selects": False,
        "selected": None,
    }
    assert capabilities["execution_lanes"]["native-local"]["supported"] is True
    for mode in ("snapshot-import", "managed-clone"):
        assert capabilities["source_modes"][mode] == {
            "supported": False,
            "owner": "platform-debug-adapter",
        }
    assert capabilities["execution_lanes"]["strict-appliance"]["supported"] is False
    assert capabilities["controlled_run_relation"] == {
        "separate_contract": True,
        "native_binding_satisfies_appliance_gate": False,
        "capability_path": "controlled_runs",
    }
    assert len(capabilities["capability_sha256"]) == 64
    assert "environment" not in encoded.decode("utf-8")


def test_native_binding_is_versioned_bounded_and_deterministic(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    info = derive_session_project_info(checkout)

    first = native_direct_binding(info)
    second = native_direct_binding(info)
    encoded = json.dumps(first, sort_keys=True, separators=(",", ":")).encode("utf-8")

    assert first == second
    assert len(encoded) < 4096
    assert first["schema"] == NATIVE_DIRECT_BINDING_SCHEMA
    assert first["version"] == 1
    assert first["state"] == "bound"
    assert first["source_mode"] == "native-direct"
    assert first["execution_lane"] == "native-local"
    assert "access_mode" not in first
    assert "access_profile" not in first
    assert first["checkout"]["canonical_path_ref"] == "cwd"
    assert (
        first["checkout"]["path_sha256"]
        == hashlib.sha256(str(checkout.resolve()).encode("utf-8")).hexdigest()
    )
    assert first["helper"]["adapter"] is None
    assert first["invariants"] == {
        "snapshot_imported": False,
        "managed_clone_created": False,
        "trusted_direct_bind_created": False,
        "controlled_run_appliance": False,
    }
    assert "cleanup" not in first
    assert "environment" not in encoded.decode("utf-8")


def test_session_start_binds_exact_host_checkout_without_snapshot_or_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init", "-b", "dev", str(checkout)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    daemon = WorkerBeeDaemon(state_root=tmp_path / "state", cwd=tmp_path)

    result = daemon.session_start(cwd=checkout, goal="native direct unit seam")

    assert result["cwd"] == str(checkout.resolve())
    assert result["git_root"] == str(checkout.resolve())
    assert result["project_identity"]["cwd"] == str(checkout.resolve())
    assert result["source_binding"]["workspace_id"] == result["project"]
    assert result["source_binding"]["source_mode"] == "native-direct"
    assert result["source_binding"]["execution_lane"] == "native-local"
    assert "access_mode" not in result["source_binding"]
    assert "access_profile" not in result["source_binding"]
    assert result["source_binding"]["invariants"]["snapshot_imported"] is False
    assert result["source_binding"]["invariants"]["managed_clone_created"] is False


def test_native_local_tool_surface_is_preserved_and_has_no_private_launcher() -> None:
    assert set(MCP_TOOL_NAMES) >= NATIVE_LOCAL_TOOLS
    assert set(MCP_TOOL_NAMES).isdisjoint(CONTROLLED_RUN_TOOL_NAMES)
    assert not any(
        marker in tool
        for tool in MCP_TOOL_NAMES
        for marker in ("snapshot_import", "managed_clone", "appliance_launch")
    )


def test_daemon_capabilities_keep_native_tools_and_controlled_runs_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("workerbee.k1s_runtime.resolve_k1s_runtime", lambda **_: _runtime())
    monkeypatch.setattr("workerbee.daemon.runtime_diagnostics", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "workerbee.daemon.containerd_privilege_status",
        lambda **_kwargs: {"enabled": False, "effective_mode": "off", "runtime": "auto"},
    )
    daemon = WorkerBeeDaemon(state_root=tmp_path / "state", cwd=tmp_path)

    capabilities = daemon.capabilities()

    assert set(capabilities["mcp_tools"]) >= NATIVE_LOCAL_TOOLS
    assert set(capabilities["mcp_tools"]) == set(MCP_TOOL_NAMES)
    assert capabilities["native_interoperability"] == native_interoperability_capabilities()
    assert capabilities["controlled_runs"]["enabled"] is False
    assert capabilities["controlled_runs"]["advertised_tools"] == []


def test_native_binding_cannot_satisfy_controlled_run_appliance_gate(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    binding = native_direct_binding(derive_session_project_info(checkout))
    controlled_runs = ControlledRunService(
        state_root=tmp_path / "state",
        environ={"WORKERBEE_CONTROLLED_RUN_MODE": "enabled"},
    )

    assert controlled_runs.available is False
    assert controlled_runs.capabilities()["advertised_tools"] == []
    with pytest.raises(WorkerBeeError) as error:
        controlled_runs.preflight(binding)
    assert error.value.code == "WORKERBEE_CONTROLLED_RUN_CAPABILITY_UNAVAILABLE"
