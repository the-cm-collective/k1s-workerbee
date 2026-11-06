from __future__ import annotations

import hashlib
import inspect
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from workerbee.contract import WorkerBeeError
from workerbee.controlled_runs import (
    CONTROLLED_RUN_REQUIRED_ADAPTER_CAPABILITIES,
    CONTROLLED_RUN_TOOL_NAMES,
    ControlledRunService,
    agent_pbx_profile_sha256,
)
from workerbee.daemon import WorkerBeeDaemon
from workerbee.k1s_runtime import K1sRuntime
from workerbee.mcp_server import _register_controlled_run_tools

TOOL_SHA = "2" * 64
BASELINE = "a" * 40
CANDIDATE = "b" * 40
PROFILE_BODY: dict[str, Any] = {
    "profile_id": "isolated-runtime-validation",
    "version": 1,
    "label": "Isolated runtime validation",
    "enabled": True,
    "backend": "workerbee-standard-workspace",
    "required_authority": "trusted-workbench",
    "allowed_workerbee_backends": ["platform_debug"],
    "allowed_projects": ["k1s-workerbee-oss-v1"],
    "source_mode": "immutable-commit-export",
    "network_mode": "private-loopback",
    "external_egress": "deny",
    "credentials": "none",
    "recipe_id": "k1s-rc1-runtime-parity",
    "tool_manifest_sha256": TOOL_SHA,
    "max_seconds": 1800,
    "max_memory_mib": 4096,
    "max_output_bytes": 16 * 1024 * 1024,
    "max_concurrency": 1,
    "approval_mode": "campaign_lease",
    "lease_max_uses": 4,
    "lease_ttl_seconds": 7200,
    "retention_seconds": 604800,
    "cleanup_required": True,
    "allow_host_fallback": False,
}
PROFILE_SHA = agent_pbx_profile_sha256(PROFILE_BODY)
REQUEST_IDENTITY = {
    "action_class": "workerbee_controlled_run",
    "root_operator_agent_id": "operator-8",
    "target_fork_agent_id": "operator-8-fork",
    "campaign_id": "campaign-1",
    "workspace_set_digest": "1" * 64,
    "backend": "platform_debug",
    "backend_instance_id": "platform-debug-1",
    "backend_fingerprint": "f" * 64,
    "backend_generation": 1,
    "workspace_binding_id": "binding-1",
    "project": "k1s-workerbee-oss-v1",
    "profile_id": PROFILE_BODY["profile_id"],
    "profile_version": PROFILE_BODY["version"],
    "profile_sha256": PROFILE_SHA,
    "baseline_commit": BASELINE,
    "candidate_commit": CANDIDATE,
    "recipe_id": PROFILE_BODY["recipe_id"],
    "tool_manifest_sha256": TOOL_SHA,
}
REQUEST_SHA = hashlib.sha256(
    json.dumps(REQUEST_IDENTITY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


class FakeStrictAdapter:
    name = "platform-debug-fake"
    version = "1"

    def __init__(self) -> None:
        self.capability_overrides: dict[str, Any] = {}
        self.source_overrides: dict[str, Any] = {}
        self.start_calls = 0
        self.cancel_calls = 0
        self.recover_calls = 0
        self.state = "running"
        self.start_error: Exception | None = None
        self.preflight_error: Exception | None = None
        self.status_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.receipt_error: Exception | None = None
        self.recover_error: Exception | None = None
        self.allowed_profile_sha = PROFILE_SHA
        self.max_artifacts = 32
        self.max_artifact_bytes = 8 * 1024 * 1024
        self.cleanup = {"complete": True, "residue": []}
        self.evidence: dict[str, Any] = {"result": "bounded"}

    def capabilities(self) -> dict[str, Any]:
        return {
            "available": True,
            **dict.fromkeys(CONTROLLED_RUN_REQUIRED_ADAPTER_CAPABILITIES, True),
            **self.capability_overrides,
        }

    def recipes(self) -> dict[str, dict[str, Any]]:
        return {
            "k1s-rc1-runtime-parity": {
                "tool_manifest_sha256": TOOL_SHA,
                "profile_sha256": self.allowed_profile_sha,
                "max_artifacts": self.max_artifacts,
                "max_artifact_bytes": self.max_artifact_bytes,
                "max_receipt_bytes": 64 * 1024,
            }
        }

    def preflight(self, *, request, recipe, policy):  # type: ignore[no-untyped-def]
        if self.preflight_error is not None:
            raise self.preflight_error
        assert recipe["tool_manifest_sha256"] == request["tool_manifest_sha256"]
        assert policy["network"]["dns"] == "deny"
        return {
            "source": {
                "mode": "immutable-commit-export",
                "export_count": 1,
                "export_sha256": "e" * 64,
                "baseline_commit": request["baseline_commit"],
                "baseline_tree": "c" * 40,
                "candidate_commit": request["candidate_commit"],
                "candidate_tree": "d" * 40,
                "caller_worktree_read_only": True,
                "source_roots_read_only": True,
                **self.source_overrides,
            }
        }

    def start(
        self,
        *,
        run_id,
        run_root,
        scratch_dir,
        request,
        recipe,
        policy,
    ):  # type: ignore[no-untyped-def]
        _ = (run_root, scratch_dir, request, recipe, policy)
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return {
            "run_id": run_id,
            "workspace_id": "wb-space-1",
            "state": self.state,
        }

    def status(self, *, run_id):  # type: ignore[no-untyped-def]
        if self.status_error is not None:
            raise self.status_error
        return {"run_id": run_id, "workspace_id": "wb-space-1", "state": self.state}

    def cancel(self, *, run_id, reason):  # type: ignore[no-untyped-def]
        _ = reason
        self.cancel_calls += 1
        if self.cancel_error is not None:
            raise self.cancel_error
        self.state = "canceled"
        return {"run_id": run_id, "workspace_id": "wb-space-1", "state": self.state}

    def receipt(self, *, run_id):  # type: ignore[no-untyped-def]
        if self.receipt_error is not None:
            raise self.receipt_error
        return {
            "run_id": run_id,
            "workspace_id": "wb-space-1",
            "state": self.state,
            "evidence": dict(self.evidence),
            "cleanup": dict(self.cleanup),
        }

    def recover(self, *, run_id, journal):  # type: ignore[no-untyped-def]
        _ = journal
        self.recover_calls += 1
        if self.recover_error is not None:
            raise self.recover_error
        self.state = "canceled"
        return {"run_id": run_id, "workspace_id": "wb-space-1", "state": self.state}


def _profile(**overrides: Any) -> dict[str, Any]:
    profile = {
        **PROFILE_BODY,
        "sha256": PROFILE_SHA,
        **overrides,
    }
    return profile


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "request_id": "request-1",
        "idempotency_key": "request-1",
        "request_sha256": REQUEST_SHA,
        "root_operator_agent_id": "operator-8",
        "target_fork_agent_id": "operator-8-fork",
        "campaign_id": "campaign-1",
        "workspace_binding_id": "binding-1",
        "profile": _profile(),
        "profile_sha256": PROFILE_SHA,
        "baseline_commit": BASELINE,
        "candidate_commit": CANDIDATE,
        "recipe_id": "k1s-rc1-runtime-parity",
        "tool_manifest_sha256": TOOL_SHA,
        **overrides,
    }


def _service(tmp_path: Path, adapter: FakeStrictAdapter | None = None) -> ControlledRunService:
    return ControlledRunService(
        state_root=tmp_path / "state",
        adapter=adapter or FakeStrictAdapter(),
        environ={"WORKERBEE_CONTROLLED_RUN_MODE": "enabled"},
    )


def test_controlled_runs_are_default_disabled_and_not_advertised(tmp_path: Path) -> None:
    service = ControlledRunService(state_root=tmp_path / "state", environ={})

    capabilities = service.capabilities()

    assert capabilities["enabled"] is False
    assert capabilities["available"] is False
    assert capabilities["advertised_tools"] == []
    assert not service.root.exists()
    with pytest.raises(WorkerBeeError) as error:
        service.preflight(_payload())
    assert error.value.code == "WORKERBEE_CONTROLLED_RUN_DISABLED"


def test_capability_gate_requires_every_strict_adapter_capability(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    adapter.capability_overrides["dns_denied"] = False
    service = _service(tmp_path, adapter)

    capabilities = service.capabilities()

    assert capabilities["available"] is False
    assert capabilities["advertised_tools"] == []
    assert capabilities["adapter"]["missing_capabilities"] == ["dns_denied"]


def test_capability_gate_advertises_exact_five_tools_when_ready(tmp_path: Path) -> None:
    capabilities = _service(tmp_path).capabilities()

    assert capabilities["available"] is True
    assert tuple(capabilities["advertised_tools"]) == CONTROLLED_RUN_TOOL_NAMES
    assert len(capabilities["advertised_tools"]) == 5
    assert all(name.startswith("workerbee_v1_") for name in CONTROLLED_RUN_TOOL_NAMES)


def test_daemon_capabilities_hide_disabled_and_advertise_enabled_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.daemon.runtime_diagnostics",
        lambda *_args, **_kwargs: {"ok": True, "selected": "podman"},
    )
    monkeypatch.setattr(
        "workerbee.daemon.containerd_privilege_status",
        lambda **_kwargs: {"enabled": False},
    )
    monkeypatch.setattr(
        "workerbee.k1s_runtime.resolve_k1s_runtime",
        lambda **_kwargs: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python3",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    disabled = WorkerBeeDaemon(
        state_root=tmp_path / "disabled",
        controlled_run_adapter=FakeStrictAdapter(),
        controlled_run_environ={},
    ).capabilities()
    enabled = WorkerBeeDaemon(
        state_root=tmp_path / "enabled",
        controlled_run_adapter=FakeStrictAdapter(),
        controlled_run_environ={"WORKERBEE_CONTROLLED_RUN_MODE": "enabled"},
    ).capabilities()

    assert disabled["controlled_runs"]["available"] is False
    assert set(disabled["mcp_tools"]).isdisjoint(CONTROLLED_RUN_TOOL_NAMES)
    assert enabled["controlled_runs"]["available"] is True
    assert set(enabled["mcp_tools"]).intersection(CONTROLLED_RUN_TOOL_NAMES) == set(
        CONTROLLED_RUN_TOOL_NAMES
    )
    assert len(enabled["mcp_tools"]) == len(disabled["mcp_tools"]) + 5


def test_mcp_registration_has_exact_agent_pbx_schemas_without_argv_or_env(tmp_path: Path) -> None:
    service = _service(tmp_path)
    daemon = type("Daemon", (), {"controlled_runs": service})()

    class FakeMCP:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self):  # type: ignore[no-untyped-def]
            def decorator(function):  # type: ignore[no-untyped-def]
                self.tools[function.__name__] = function
                return function

            return decorator

    mcp = FakeMCP()

    registered = _register_controlled_run_tools(mcp, daemon)

    assert registered == CONTROLLED_RUN_TOOL_NAMES
    tools = mcp.tools
    assert set(tools) == set(CONTROLLED_RUN_TOOL_NAMES)
    preflight = inspect.signature(tools["workerbee_v1_controlled_run_preflight"])
    start = inspect.signature(tools["workerbee_v1_controlled_run_start"])
    expected = {
        "request_id",
        "idempotency_key",
        "request_sha256",
        "root_operator_agent_id",
        "target_fork_agent_id",
        "campaign_id",
        "workspace_binding_id",
        "profile",
        "profile_sha256",
        "baseline_commit",
        "candidate_commit",
        "recipe_id",
        "tool_manifest_sha256",
    }
    assert set(preflight.parameters) == expected
    assert set(start.parameters) == expected
    assert "argv" not in preflight.parameters
    assert "command" not in preflight.parameters
    assert "environment" not in start.parameters
    assert "env" not in start.parameters
    assert set(inspect.signature(tools["workerbee_v1_controlled_run_status"]).parameters) == {
        "run_id",
    }
    assert set(inspect.signature(tools["workerbee_v1_controlled_run_cancel"]).parameters) == {
        "run_id",
        "reason",
    }
    assert set(inspect.signature(tools["workerbee_v1_controlled_run_receipt"]).parameters) == {
        "run_id",
    }


def test_mcp_registration_is_absent_when_default_disabled(tmp_path: Path) -> None:
    service = ControlledRunService(state_root=tmp_path / "state", environ={})
    daemon = type("Daemon", (), {"controlled_runs": service})()

    class FakeMCP:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self):  # type: ignore[no-untyped-def]
            def decorator(function):  # type: ignore[no-untyped-def]
                self.tools[function.__name__] = function
                return function

            return decorator

    mcp = FakeMCP()

    assert _register_controlled_run_tools(mcp, daemon) == ()
    assert mcp.tools == {}


def test_registered_tools_accept_agent_pbx_payload_and_return_required_shapes(
    tmp_path: Path,
) -> None:
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)
    daemon = type("Daemon", (), {"controlled_runs": service})()

    class FakeMCP:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self):  # type: ignore[no-untyped-def]
            def decorator(function):  # type: ignore[no-untyped-def]
                self.tools[function.__name__] = function
                return function

            return decorator

    mcp = FakeMCP()
    _register_controlled_run_tools(mcp, daemon)

    payload = _payload()
    preflight = mcp.tools["workerbee_v1_controlled_run_preflight"](**payload)
    started = mcp.tools["workerbee_v1_controlled_run_start"](**payload)
    run_id = started["data"]["run_id"]
    adapter.state = "succeeded"
    status = mcp.tools["workerbee_v1_controlled_run_status"](run_id)
    receipt = mcp.tools["workerbee_v1_controlled_run_receipt"](run_id)

    assert preflight["ok"] is True
    assert preflight["data"]["request_sha256"] == REQUEST_SHA
    assert preflight["data"]["profile_sha256"] == PROFILE_SHA
    assert preflight["data"]["tool_manifest_sha256"] == TOOL_SHA
    assert started["data"]["run_id"]
    assert started["data"]["workspace_id"] == "wb-space-1"
    assert status["data"]["state"] == "succeeded"
    assert receipt["data"]["workspace_id"] == "wb-space-1"
    assert isinstance(receipt["data"]["evidence"], dict)
    assert receipt["data"]["cleanup"] == {"complete": True, "residue": []}


def test_mcp_wrapper_returns_stable_redacted_adapter_error(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    adapter.preflight_error = RuntimeError("private adapter token must not escape")
    daemon = type("Daemon", (), {"controlled_runs": _service(tmp_path, adapter)})()

    class FakeMCP:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self):  # type: ignore[no-untyped-def]
            def decorator(function):  # type: ignore[no-untyped-def]
                self.tools[function.__name__] = function
                return function

            return decorator

    mcp = FakeMCP()
    _register_controlled_run_tools(mcp, daemon)

    result = mcp.tools["workerbee_v1_controlled_run_preflight"](**_payload())

    assert result["ok"] is False
    assert result["error"]["code"] == "WORKERBEE_CONTROLLED_RUN_ADAPTER_ERROR"
    assert result["error"]["details"] == {
        "operation": "preflight",
        "error_type": "RuntimeError",
    }
    assert "private adapter token" not in str(result)


def test_preflight_echoes_exact_digests_and_strict_policy(tmp_path: Path) -> None:
    result = _service(tmp_path).preflight(_payload())

    assert result["ok"] is True
    assert result["request_sha256"] == REQUEST_SHA
    assert result["profile_sha256"] == PROFILE_SHA
    assert result["tool_manifest_sha256"] == TOOL_SHA
    assert result["source"]["export_count"] == 1
    assert result["source"]["baseline_commit"] == BASELINE
    assert result["source"]["candidate_commit"] == CANDIDATE
    assert result["policy"]["network"] == {
        "loopback_tcp": "allow",
        "pathname_af_unix": "allow",
        "dns": "deny",
        "non_loopback_interfaces": "deny",
        "non_loopback_routes": "deny",
        "non_loopback_connects": "deny",
        "external_egress": "deny",
        "abstract_af_unix": "deny",
        "host_control_sockets": "deny",
    }
    assert result["policy"]["environment"]["inherit"] is False
    assert result["policy"]["tools"]["package_index"] == "deny"
    assert result["policy"]["limits"]["artifact_count"] == 32


def test_agent_pbx_canonical_request_and_profile_algorithms_match_fixture() -> None:
    assert PROFILE_SHA == "292cea6c0b09ff0a56d4f3e62e3e2db8f565ad975dfbcb75693dda6d874c4fef"
    assert (
        hashlib.sha256(
            json.dumps(PROFILE_BODY, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == PROFILE_SHA
    )
    assert REQUEST_SHA == "dbe7b0e0e1a53d46f4bf066fd50286225878e2ca758e2f2ec4d273640078d9cf"
    assert (
        hashlib.sha256(
            json.dumps(REQUEST_IDENTITY, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == REQUEST_SHA
    )


@pytest.mark.parametrize(
    ("profile_change", "code"),
    [
        ({"source_mode": "working-tree"}, "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT"),
        ({"network_mode": "host"}, "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT"),
        ({"external_egress": "allow"}, "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT"),
        ({"credentials": "ambient"}, "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT"),
        ({"cleanup_required": False}, "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT"),
        ({"max_seconds": 0}, "WORKERBEE_CONTROLLED_RUN_PROFILE_INVALID"),
        ({"max_memory_mib": 0}, "WORKERBEE_CONTROLLED_RUN_PROFILE_INVALID"),
        ({"max_output_bytes": 0}, "WORKERBEE_CONTROLLED_RUN_PROFILE_INVALID"),
        ({"max_concurrency": 9}, "WORKERBEE_CONTROLLED_RUN_PROFILE_INVALID"),
    ],
)
def test_preflight_rejects_policy_and_resource_drift(
    tmp_path: Path,
    profile_change: dict[str, Any],
    code: str,
) -> None:
    profile = _profile(**profile_change)
    profile_sha = agent_pbx_profile_sha256(profile)
    profile["sha256"] = profile_sha
    payload = _payload(profile=profile, profile_sha256=profile_sha)
    adapter = FakeStrictAdapter()
    if code == "WORKERBEE_CONTROLLED_RUN_PROFILE_INVALID":
        adapter.allowed_profile_sha = profile_sha

    with pytest.raises(WorkerBeeError) as error:
        _service(tmp_path, adapter).preflight(payload)

    assert error.value.code == code


def test_preflight_rejects_profile_tool_recipe_and_source_drift(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(WorkerBeeError) as profile_error:
        service.preflight(_payload(profile_sha256="4" * 64))
    assert profile_error.value.code == "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT"

    with pytest.raises(WorkerBeeError) as recipe_error:
        recipe_profile = _profile(recipe_id="not-registered")
        recipe_sha = agent_pbx_profile_sha256(recipe_profile)
        recipe_profile["sha256"] = recipe_sha
        service.preflight(
            _payload(
                recipe_id="not-registered",
                profile=recipe_profile,
                profile_sha256=recipe_sha,
            )
        )
    assert recipe_error.value.code == "WORKERBEE_CONTROLLED_RUN_RECIPE_DENIED"

    with pytest.raises(WorkerBeeError) as tool_error:
        tool_profile = _profile(tool_manifest_sha256="4" * 64)
        tool_profile_sha = agent_pbx_profile_sha256(tool_profile)
        tool_profile["sha256"] = tool_profile_sha
        service.preflight(
            _payload(
                tool_manifest_sha256="4" * 64,
                profile=tool_profile,
                profile_sha256=tool_profile_sha,
            )
        )
    assert tool_error.value.code == "WORKERBEE_CONTROLLED_RUN_TOOL_MANIFEST_DRIFT"

    adapter = FakeStrictAdapter()
    adapter.source_overrides["candidate_commit"] = "f" * 40
    with pytest.raises(WorkerBeeError) as source_error:
        _service(tmp_path / "source", adapter).preflight(_payload())
    assert source_error.value.code == "WORKERBEE_CONTROLLED_RUN_SOURCE_DRIFT"


def test_preflight_recomputes_canonical_agent_pbx_profile_digest(tmp_path: Path) -> None:
    profile = _profile(label="tampered after Agent PBX hashing")

    with pytest.raises(WorkerBeeError) as error:
        _service(tmp_path).preflight(_payload(profile=profile))

    assert error.value.code == "WORKERBEE_CONTROLLED_RUN_PROFILE_DRIFT"


@pytest.mark.parametrize("field", ["argv", "command", "environment", "env", "shell"])
def test_caller_commands_and_environment_are_rejected(tmp_path: Path, field: str) -> None:
    payload = _payload(profile={**_profile(), field: ["unsafe"]})

    with pytest.raises(WorkerBeeError) as error:
        _service(tmp_path).preflight(payload)

    assert error.value.code == "WORKERBEE_CONTROLLED_RUN_CALLER_COMMAND_DENIED"


def test_start_is_idempotent_and_conflicts_on_canonical_request_drift(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)

    first = service.start(_payload())
    second = service.start(_payload())

    assert first["run_id"] == second["run_id"]
    assert first["workspace_id"] == "wb-space-1"
    assert second["idempotent_reuse"] is True
    assert adapter.start_calls == 1
    with pytest.raises(WorkerBeeError) as conflict:
        service.start(_payload(request_sha256="4" * 64))
    assert conflict.value.code == "WORKERBEE_CONTROLLED_RUN_IDEMPOTENCY_CONFLICT"
    with pytest.raises(WorkerBeeError) as canonical_conflict:
        service.start(_payload(candidate_commit="c" * 40))
    assert canonical_conflict.value.code == "WORKERBEE_CONTROLLED_RUN_IDEMPOTENCY_CONFLICT"


def test_start_creates_owner_private_run_root_and_scratch(tmp_path: Path) -> None:
    service = _service(tmp_path)

    started = service.start(_payload())
    run_root = service.runs_root / started["run_id"]

    assert stat.S_IMODE(run_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((run_root / "scratch").stat().st_mode) == 0o700
    assert stat.S_IMODE((run_root / "evidence").stat().st_mode) == 0o700
    assert stat.S_IMODE((run_root / "journal.json").stat().st_mode) == 0o600
    assert service.root == service.state_root / "controlled-runs"


def test_concurrency_limit_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.start(_payload())

    with pytest.raises(WorkerBeeError) as error:
        service.start(
            _payload(
                request_id="request-2",
                idempotency_key="request-2",
                request_sha256="4" * 64,
            )
        )

    assert error.value.code == "WORKERBEE_CONTROLLED_RUN_CONCURRENCY_LIMIT"


@pytest.mark.parametrize(
    "state",
    [
        "provisioning",
        "starting",
        "canary",
        "running",
        "collecting",
        "cleaning",
        "recovery_required",
        "failed",
        "canceled",
        "cancelled",
    ],
)
def test_status_supports_agent_pbx_states(tmp_path: Path, state: str) -> None:
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)
    started = service.start(_payload())
    adapter.state = state

    result = service.status(started["run_id"])

    assert result["state"] == state
    assert result["status"] == {
        "provisioning": "preparing",
        "starting": "preparing",
        "canary": "ready",
        "recovery_required": "recovering",
    }.get(state, state)
    assert result["status"] in {
        "preparing",
        "ready",
        "running",
        "collecting",
        "cleaning",
        "recovering",
        "succeeded",
        "failed",
        "canceled",
        "cancelled",
    }


def test_status_rejects_unknown_adapter_state(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)
    started = service.start(_payload())
    adapter.state = "unknown"

    with pytest.raises(WorkerBeeError) as error:
        service.status(started["run_id"])

    assert error.value.code == "WORKERBEE_CONTROLLED_RUN_STATUS_INVALID"


def test_cancel_is_idempotent_and_returns_cleanup(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)
    started = service.start(_payload())

    first = service.cancel(started["run_id"], "operator request")
    second = service.cancel(started["run_id"], "operator request")

    assert first["state"] == "canceled"
    assert first["cleanup"] == {"complete": True, "residue": []}
    assert second["cancel_reused"] is True
    assert adapter.cancel_calls == 1


def test_cleanup_residue_prevents_success(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)
    started = service.start(_payload())
    adapter.state = "succeeded"
    adapter.cleanup = {"complete": False, "residue": ["namespace/demo"]}

    status = service.status(started["run_id"])
    receipt = service.receipt(started["run_id"])

    assert status["state"] == "recovery_required"
    assert receipt["success"] is False
    assert receipt["cleanup"]["residue"] == ["namespace/demo"]


def test_receipt_is_redacted_and_bounded(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    adapter.evidence = {
        "summary": "ok",
        "access_token": "must-not-escape",
        **{f"large_{index}": "x" * (32 * 1024) for index in range(16)},
    }
    service = _service(tmp_path, adapter)
    started = service.start(_payload())

    receipt = service.receipt(started["run_id"])

    assert len(str(receipt).encode()) < 64 * 1024
    assert "must-not-escape" not in str(receipt)
    assert receipt["evidence"]["truncated"] is True
    assert len(receipt["receipt_sha256"]) == 64


@pytest.mark.parametrize(
    ("artifacts", "code"),
    [
        (
            [{"path": f"artifact-{index}", "size_bytes": 1} for index in range(2)],
            "WORKERBEE_CONTROLLED_RUN_ARTIFACT_LIMIT",
        ),
        (
            [{"path": "artifact", "size_bytes": 1025}],
            "WORKERBEE_CONTROLLED_RUN_ARTIFACT_LIMIT",
        ),
    ],
)
def test_receipt_enforces_recipe_artifact_bounds(
    tmp_path: Path,
    artifacts: list[dict[str, Any]],
    code: str,
) -> None:
    adapter = FakeStrictAdapter()
    adapter.max_artifacts = 1
    adapter.max_artifact_bytes = 1024
    adapter.evidence = {"artifacts": artifacts}
    service = _service(tmp_path, adapter)
    started = service.start(_payload())

    with pytest.raises(WorkerBeeError) as error:
        service.receipt(started["run_id"])

    assert error.value.code == code


def test_start_failure_always_attempts_cleanup_and_journals_terminal_state(
    tmp_path: Path,
) -> None:
    adapter = FakeStrictAdapter()
    adapter.start_error = RuntimeError("appliance start failed")
    service = _service(tmp_path, adapter)

    with pytest.raises(WorkerBeeError) as error:
        service.start(_payload())

    assert error.value.code == "WORKERBEE_CONTROLLED_RUN_ADAPTER_ERROR"
    assert "appliance start failed" not in str(error.value.public_dict())
    run_id = "crun-" + hashlib.sha256(b"request-1").hexdigest()[:24]
    journal = service._read_journal(run_id)  # noqa: SLF001
    assert journal["state"] == "failed"
    assert adapter.cancel_calls == 1
    assert adapter.recover_calls == 1


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ("status", TimeoutError("private timeout secret")),
        ("cancel", RuntimeError("private cancel secret")),
    ],
)
def test_adapter_lifecycle_errors_are_redacted_and_recovered(
    tmp_path: Path,
    operation: str,
    error: Exception,
) -> None:
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)
    started = service.start(_payload())
    if operation == "status":
        adapter.status_error = error
    else:
        adapter.cancel_error = error

    with pytest.raises(WorkerBeeError) as caught:
        if operation == "status":
            service.status(started["run_id"])
        else:
            service.cancel(started["run_id"])

    assert caught.value.code == (
        "WORKERBEE_CONTROLLED_RUN_ADAPTER_TIMEOUT"
        if isinstance(error, TimeoutError)
        else "WORKERBEE_CONTROLLED_RUN_ADAPTER_ERROR"
    )
    assert "secret" not in str(caught.value.public_dict())
    assert adapter.recover_calls == 1
    assert service._read_journal(started["run_id"])["state"] == "canceled"  # noqa: SLF001


def test_restart_recovery_failure_is_durable_and_reports_cleanup_unknown(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    first = _service(tmp_path, adapter)
    started = first.start(_payload())
    adapter.recover_error = RuntimeError("private recovery secret")

    recovered = _service(tmp_path, adapter).recover_incomplete()
    journal = first._read_journal(started["run_id"])  # noqa: SLF001

    assert recovered[0]["state"] == "recovery_required"
    assert recovered[0]["cleanup"] == {
        "complete": False,
        "residue": ["cleanup receipt unavailable"],
    }
    assert journal["state"] == "recovery_required"
    assert "private recovery secret" not in str(journal)


def test_receipt_success_is_bound_to_succeeded_journal_and_cleanup(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)
    started = service.start(_payload())
    adapter.state = "succeeded"

    before_status = service.receipt(started["run_id"])
    service.status(started["run_id"])
    succeeded = service.receipt(started["run_id"])
    adapter.cleanup = {"complete": False, "residue": ["run-root"]}
    residue = service.receipt(started["run_id"])

    assert before_status["state"] == "running"
    assert before_status["success"] is False
    assert succeeded["state"] == "succeeded"
    assert succeeded["success"] is True
    assert len(succeeded["canonical_payload_sha256"]) == 64
    assert succeeded["receipt_sha256"] != before_status["receipt_sha256"]
    assert residue["success"] is False


def test_aggregate_receipt_limit_fails_closed(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    adapter.source_overrides.update(
        {f"oversized_attestation_{index}": "x" * (20 * 1024) for index in range(5)}
    )
    service = _service(tmp_path, adapter)
    started = service.start(_payload())

    with pytest.raises(WorkerBeeError) as error:
        service.receipt(started["run_id"])

    assert error.value.code == "WORKERBEE_CONTROLLED_RUN_RECEIPT_LIMIT"


def test_receipt_adapter_error_is_redacted_and_read_only(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)
    started = service.start(_payload())
    before = service._read_journal(started["run_id"])  # noqa: SLF001
    adapter.receipt_error = RuntimeError("private receipt token")

    with pytest.raises(WorkerBeeError) as error:
        service.receipt(started["run_id"])

    after = service._read_journal(started["run_id"])  # noqa: SLF001
    assert error.value.code == "WORKERBEE_CONTROLLED_RUN_ADAPTER_ERROR"
    assert "private receipt token" not in str(error.value.public_dict())
    assert after == before


def test_restart_recovery_uses_durable_journal_and_cleanup_receipt(tmp_path: Path) -> None:
    adapter = FakeStrictAdapter()
    first = _service(tmp_path, adapter)
    started = first.start(_payload())
    assert started["state"] == "running"

    restarted = _service(tmp_path, adapter)
    recovered = restarted.recover_incomplete()

    assert recovered == [
        {
            "run_id": started["run_id"],
            "state": "canceled",
            "cleanup": {"complete": True, "residue": []},
        }
    ]
    assert adapter.recover_calls == 1
    assert restarted._read_journal(started["run_id"])["state"] == "canceled"  # noqa: SLF001


def test_agent_pbx_flow_does_not_modify_caller_worktree(tmp_path: Path) -> None:
    caller = tmp_path / "caller"
    caller.mkdir()
    source = caller / "source.txt"
    source.write_text("immutable caller content\n", encoding="utf-8")
    before = source.read_bytes()
    adapter = FakeStrictAdapter()
    service = _service(tmp_path, adapter)

    preflight = service.preflight(_payload())
    started = service.start(_payload())
    adapter.state = "succeeded"
    status = service.status(started["run_id"])
    receipt = service.receipt(started["run_id"])

    assert preflight["request_sha256"] == REQUEST_SHA
    assert started["workspace_id"] == "wb-space-1"
    assert status["state"] == "succeeded"
    assert receipt["evidence"] == {"result": "bounded"}
    assert receipt["cleanup"] == {"complete": True, "residue": []}
    assert receipt["success"] is True
    assert source.read_bytes() == before
