from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError
from workerbee.mcp_daemon import (
    MCPDaemonConfig,
    _dashboard_health_url,
    _wait_for_port_release,
    _wait_ready,
    mcp_daemon_status,
    remote_mcp_allowed,
    restart_mcp_daemon,
    start_mcp_daemon,
    stop_mcp_daemon,
)


def test_mcp_daemon_status_reports_stale_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="docker", port=9876)
    config.global_dir.mkdir(parents=True)
    config.metadata_file.write_text(
        json.dumps({"pid": 99999, "state_root": str(tmp_path)}),
        encoding="utf-8",
    )
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda _pid: False)

    status = mcp_daemon_status(config)

    assert status["running"] is False
    assert status["stale"] is True
    assert status["mcp_url"] == "http://127.0.0.1:9876/mcp"


def test_start_mcp_daemon_writes_detached_workerbee_argv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakePopen:
        pid = 4321

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            calls["argv"] = argv
            calls["kwargs"] = kwargs

    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", project="demo", port=9876)
    monkeypatch.setattr("workerbee.mcp_daemon.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "workerbee.mcp_daemon._wait_ready",
        lambda _config, **_kwargs: _ready_payload(),
    )
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr("workerbee.mcp_daemon._pid_matches_metadata", lambda *_args: True)

    result = start_mcp_daemon(config, timeout=1)

    assert result["ok"] is True
    assert result["started"] is True
    assert calls["kwargs"]["start_new_session"] is True
    assert calls["argv"][-5:] == ["serve", "--host", "127.0.0.1", "--port", "9876"]
    metadata = json.loads(config.metadata_file.read_text(encoding="utf-8"))
    assert metadata["pid"] == 4321
    assert metadata["project"] == "demo"


def test_start_mcp_daemon_fails_fast_when_port_is_in_use(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", port=9876)
    monkeypatch.setattr(
        "workerbee.mcp_daemon._mcp_port_available",
        lambda _config: {
            "ok": False,
            "error": {
                "code": "MCP_PORT_IN_USE",
                "message": "port busy",
                "details": {"port": 9876},
                "retryable": True,
            },
        },
    )

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("daemon should not spawn when the requested port is busy")

    monkeypatch.setattr("workerbee.mcp_daemon.subprocess.Popen", fail_popen)

    result = start_mcp_daemon(config, timeout=1)

    assert result["ok"] is False
    assert result["started"] is False
    assert result["error"]["code"] == "MCP_PORT_IN_USE"


def test_start_mcp_daemon_refuses_remote_bind_without_opt_in(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", host="0.0.0.0", port=9876)

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("daemon should not spawn for remote MCP bind without opt-in")

    monkeypatch.setattr("workerbee.mcp_daemon.subprocess.Popen", fail_popen)

    result = start_mcp_daemon(config, timeout=1)

    assert result["ok"] is False
    assert result["started"] is False
    assert result["error"]["code"] == "MCP_REMOTE_BIND_REQUIRES_AUTH"


def test_start_mcp_daemon_allows_remote_bind_with_explicit_opt_in(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakePopen:
        pid = 4321

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            calls["argv"] = argv
            calls["kwargs"] = kwargs

    config = MCPDaemonConfig(
        state_root=tmp_path,
        runtime="podman",
        host="0.0.0.0",
        port=9876,
        allow_remote_mcp=True,
    )
    monkeypatch.setattr("workerbee.mcp_daemon.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "workerbee.mcp_daemon._wait_ready",
        lambda _config, **_kwargs: _ready_payload(),
    )
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr("workerbee.mcp_daemon._pid_matches_metadata", lambda *_args: True)

    result = start_mcp_daemon(config, timeout=1)

    assert result["ok"] is True
    assert "--allow-remote-mcp" in calls["argv"]


def test_remote_mcp_env_allows_remote_bind(monkeypatch) -> None:
    monkeypatch.setenv("WORKERBEE_ALLOW_REMOTE_MCP", "1")

    assert remote_mcp_allowed() is True


def test_start_mcp_daemon_uses_effective_remote_mcp_opt_in(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakePopen:
        pid = 4322

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            calls["argv"] = argv
            calls["kwargs"] = kwargs

    monkeypatch.setenv("WORKERBEE_ALLOW_REMOTE_MCP", "1")
    monkeypatch.setattr("workerbee.mcp_daemon.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "workerbee.mcp_daemon._wait_ready",
        lambda _config, **_kwargs: _ready_payload(),
    )
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda pid: pid == 4322)
    monkeypatch.setattr("workerbee.mcp_daemon._pid_matches_metadata", lambda *_args: True)

    config = MCPDaemonConfig(
        state_root=tmp_path,
        runtime="podman",
        host="0.0.0.0",
        port=9876,
    )
    result = start_mcp_daemon(config, timeout=1)
    metadata = json.loads(config.metadata_file.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert "--allow-remote-mcp" in calls["argv"]
    assert metadata["allow_remote_mcp"] is True


def test_restart_mcp_daemon_waits_for_port_release(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", port=9876)
    checks = [
        {"ok": False, "error": {"code": "MCP_PORT_IN_USE", "message": "busy"}},
        {"ok": True},
    ]
    starts: list[float] = []
    monkeypatch.setattr(
        "workerbee.mcp_daemon.stop_mcp_daemon",
        lambda _config: {"ok": True, "stopped": True},
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon._mcp_port_available",
        lambda _config: checks.pop(0),
    )
    monkeypatch.setattr("workerbee.mcp_daemon.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "workerbee.mcp_daemon.start_mcp_daemon",
        lambda _config, *, timeout: starts.append(timeout) or {"ok": True, "started": True},
    )

    result = restart_mcp_daemon(config, timeout=3)

    assert result["ok"] is True
    assert result["port_release"]["ok"] is True
    assert starts == [3]


def test_restart_mcp_daemon_refuses_remote_bind_before_stop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", host="0.0.0.0", port=9876)

    def fail_stop(*_args, **_kwargs):
        raise AssertionError("restart must not stop an existing daemon before bind guard passes")

    monkeypatch.setattr("workerbee.mcp_daemon.stop_mcp_daemon", fail_stop)

    result = restart_mcp_daemon(config, timeout=3)

    assert result["ok"] is False
    assert result["stop"] is None
    assert result["start"]["error"]["code"] == "MCP_REMOTE_BIND_REQUIRES_AUTH"


def test_status_and_stop_are_not_blocked_by_remote_bind_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", host="0.0.0.0", port=9876)
    monkeypatch.setattr("workerbee.mcp_daemon._orphan_mcp_pids", lambda _config: [])
    monkeypatch.setattr("workerbee.mcp_daemon._ensure_stop_privilege", lambda _config: {})
    monkeypatch.setattr(
        "workerbee.mcp_daemon._metadata_with_privilege",
        lambda _config, metadata, _privilege: metadata,
    )
    monkeypatch.setattr("workerbee.mcp_daemon._stop_global_ingress", lambda *_args: None)
    monkeypatch.setattr("workerbee.mcp_daemon._stop_temporary_helper", lambda *_args: None)

    status = mcp_daemon_status(config)
    stopped = stop_mcp_daemon(config)

    assert status["host"] == "0.0.0.0"
    assert status["running"] is False
    assert stopped["running"] is False


def test_wait_for_port_release_stops_matching_orphan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", port=9876)
    checks = [
        {"ok": False, "error": {"code": "MCP_PORT_IN_USE", "message": "busy"}},
        {"ok": True},
    ]
    killed: list[int] = []
    alive = {"4321": True}
    monkeypatch.setattr(
        "workerbee.mcp_daemon._mcp_port_available",
        lambda _config: checks.pop(0),
    )
    monkeypatch.setattr("workerbee.mcp_daemon._orphan_mcp_pids", lambda _config: [4321])
    monkeypatch.setattr(
        "workerbee.mcp_daemon._terminate_process_group",
        lambda pid, *, timeout: (killed.append(pid), alive.update({"4321": False})),  # noqa: ARG005
    )
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda _pid: alive["4321"])
    monkeypatch.setattr("workerbee.mcp_daemon.time.sleep", lambda _seconds: None)

    result = _wait_for_port_release(config, timeout=3)

    assert result["ok"] is True
    assert killed == [4321]
    assert result["orphan_cleanup"]["stopped_orphan_pids"] == [4321]


def test_restart_mcp_daemon_does_not_start_when_port_stays_busy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", port=9876)
    monkeypatch.setattr(
        "workerbee.mcp_daemon.stop_mcp_daemon",
        lambda _config: {"ok": True, "stopped": True},
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon._wait_for_port_release",
        lambda _config, *, timeout: {  # noqa: ARG005
            "ok": False,
            "error": {"code": "MCP_PORT_IN_USE", "message": "busy"},
        },
    )

    def fail_start(*_args, **_kwargs):
        raise AssertionError("restart should not spawn while the MCP port is busy")

    monkeypatch.setattr("workerbee.mcp_daemon.start_mcp_daemon", fail_start)

    result = restart_mcp_daemon(config, timeout=3)

    assert result["ok"] is False
    assert result["start"]["error"]["code"] == "MCP_PORT_IN_USE"


def test_start_mcp_daemon_returns_structured_containerd_privilege_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(
        state_root=tmp_path,
        runtime="containerd",
        containerd_privilege="sudo-helper",
        port=9876,
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon.containerd_privilege_status",
        lambda **_kwargs: {"enabled": True, "helper": {"responsive": False}},
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon.ensure_containerd_privilege",
        lambda **_kwargs: (_ for _ in ()).throw(
            WorkerBeeError(
                code="CONTAINERD_HELPER_START_FAILED",
                message="helper did not start",
                retryable=True,
            )
        ),
    )

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("daemon should not spawn after privilege setup failure")

    monkeypatch.setattr("workerbee.mcp_daemon.subprocess.Popen", fail_popen)

    result = start_mcp_daemon(config, timeout=1)

    assert result["ok"] is False
    assert result["started"] is False
    assert result["error"]["code"] == "CONTAINERD_HELPER_START_FAILED"


def test_wait_ready_raises_when_child_exits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", port=9876)
    config.global_dir.mkdir(parents=True)
    config.metadata_file.write_text(
        json.dumps({"pid": 4321, "state_root": str(tmp_path)}),
        encoding="utf-8",
    )
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda _pid: False)

    try:
        _wait_ready(config, timeout=0.01)
    except RuntimeError as exc:
        assert "exited early" in str(exc)
    else:
        raise AssertionError("expected _wait_ready to fail when child exits")


def test_wait_ready_probes_dashboard_health_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", port=9876)
    urls: list[str] = []
    monkeypatch.setattr("workerbee.mcp_daemon._raise_if_dead", lambda _config: None)
    monkeypatch.setattr("workerbee.mcp_daemon._daemon_process_ready", lambda _config: True)
    monkeypatch.setattr("workerbee.mcp_daemon._tcp_ready", lambda _host, _port: True)
    monkeypatch.setattr(
        "workerbee.mcp_daemon.load_global_ingress_info",
        lambda _root: {"dashboard_url": "https://dashboard.workerbee.localhost:19443/"},
    )

    def fake_request(url: str, **_kwargs):
        urls.append(url)
        return object()

    monkeypatch.setattr("workerbee.mcp_daemon.request", fake_request)

    result = _wait_ready(config, timeout=1)

    assert result["dashboard_url"] == "https://dashboard.workerbee.localhost:19443/"
    assert urls == ["https://dashboard.workerbee.localhost:19443/healthz"]


def test_wait_ready_falls_back_to_loopback_dashboard_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", port=9876)
    requests: list[tuple[str, dict[str, Any]]] = []
    loopback_requests: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("workerbee.mcp_daemon._raise_if_dead", lambda _config: None)
    monkeypatch.setattr("workerbee.mcp_daemon._daemon_process_ready", lambda _config: True)
    monkeypatch.setattr("workerbee.mcp_daemon._tcp_ready", lambda _host, _port: True)
    monkeypatch.setattr(
        "workerbee.mcp_daemon.load_global_ingress_info",
        lambda _root: {"dashboard_url": "https://dashboard.workerbee.localhost:19443/"},
    )

    def fake_request(url: str, **kwargs):
        requests.append((url, kwargs))
        raise OSError("DNS lookup failed")

    def fake_loopback_request(url: str, **kwargs):
        loopback_requests.append((url, kwargs))
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr("workerbee.mcp_daemon.request", fake_request)
    monkeypatch.setattr(
        "workerbee.mcp_daemon.request_https_via_loopback",
        fake_loopback_request,
    )

    result = _wait_ready(config, timeout=1)

    assert result["dashboard_url"] == "https://dashboard.workerbee.localhost:19443/"
    assert requests[0][0] == "https://dashboard.workerbee.localhost:19443/healthz"
    assert loopback_requests[0][0] == "https://127.0.0.1:19443/healthz"
    assert loopback_requests[0][1]["server_hostname"] == "dashboard.workerbee.localhost"
    assert loopback_requests[0][1]["host_header"] == "dashboard.workerbee.localhost:19443"


def test_dashboard_health_url_normalizes_trailing_slash() -> None:
    assert (
        _dashboard_health_url("https://dashboard.workerbee.localhost:19443/")
        == "https://dashboard.workerbee.localhost:19443/healthz"
    )


def test_start_mcp_daemon_transfers_containerd_helper_env_to_child(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakePopen:
        pid = 4321

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            calls["argv"] = argv
            calls["kwargs"] = kwargs

    config = MCPDaemonConfig(
        state_root=tmp_path,
        runtime="containerd",
        project="demo",
        port=9876,
        containerd_privilege="sudo-helper",
    )
    monkeypatch.setattr("workerbee.mcp_daemon.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "workerbee.mcp_daemon.ensure_containerd_privilege",
        lambda **_kwargs: {
            "ok": True,
            "effective_mode": "sudo-helper",
            "helper": {"started": True},
            "env": {"WORKERBEE_NERDCTL_BIN": str(tmp_path / "workerbee-nerdctl")},
        },
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon.containerd_privilege_status",
        lambda **_kwargs: {"enabled": True},
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon._wait_ready",
        lambda _config, **_kwargs: _ready_payload(),
    )
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr("workerbee.mcp_daemon._pid_matches_metadata", lambda *_args: True)

    result = start_mcp_daemon(config, timeout=1)

    assert result["ok"] is True
    assert calls["argv"][calls["argv"].index("--containerd-privilege") + 1] == "unprivileged"
    assert calls["kwargs"]["env"]["WORKERBEE_NERDCTL_BIN"] == str(
        tmp_path / "workerbee-nerdctl"
    )
    metadata = json.loads(config.metadata_file.read_text(encoding="utf-8"))
    assert metadata["containerd_privilege"]["helper"]["started"] is True


def test_start_mcp_daemon_refreshes_helper_when_containerd_daemon_is_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(
        state_root=tmp_path,
        runtime="containerd",
        project="demo",
        port=9876,
        containerd_privilege="sudo-helper",
    )
    config.global_dir.mkdir(parents=True)
    config.metadata_file.write_text(
        json.dumps({"pid": 4321, "state_root": str(tmp_path), "runtime": "containerd"}),
        encoding="utf-8",
    )
    privilege_calls: list[Path] = []
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr("workerbee.mcp_daemon._pid_matches_metadata", lambda *_args: True)
    monkeypatch.setattr(
        "workerbee.mcp_daemon.ensure_containerd_privilege",
        lambda state_root, **_kwargs: privilege_calls.append(state_root)
        or {"effective_mode": "sudo-helper", "helper": {"responsive": True}},
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon.containerd_privilege_status",
        lambda **_kwargs: {"enabled": True, "helper": {"responsive": True}},
    )

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("running daemon must not spawn a second daemon")

    monkeypatch.setattr("workerbee.mcp_daemon.subprocess.Popen", fail_popen)

    result = start_mcp_daemon(config, timeout=1)

    assert result["ok"] is True
    assert result["started"] is False
    assert result["containerd_privilege_mode"] == "sudo-helper"
    assert privilege_calls == [tmp_path]


def test_stop_mcp_daemon_removes_metadata_after_process_stop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="docker")
    config.global_dir.mkdir(parents=True)
    config.metadata_file.write_text(
        json.dumps({"pid": 4321, "state_root": str(tmp_path)}),
        encoding="utf-8",
    )
    alive = {"value": True}
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda _pid: alive["value"])
    monkeypatch.setattr("workerbee.mcp_daemon._pid_matches_metadata", lambda *_args: True)
    monkeypatch.setattr(
        "workerbee.mcp_daemon._stop_global_ingress",
        lambda *_args: {"ok": True, "stopped": True, "runtime": "docker"},
    )

    def fake_terminate(_pid: int, *, timeout: float) -> None:
        _ = timeout
        alive["value"] = False

    monkeypatch.setattr("workerbee.mcp_daemon._terminate_process_group", fake_terminate)

    result = stop_mcp_daemon(config, timeout=1)

    assert result["stopped"] is True
    assert result["global_ingress_stop"]["stopped"] is True
    assert not config.metadata_file.exists()


def test_stop_mcp_daemon_without_metadata_still_stops_global_ingress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman")
    calls: list[str] = []
    monkeypatch.setattr(
        "workerbee.mcp_daemon._stop_global_ingress",
        lambda *_args: calls.append("ingress") or {"ok": True, "stopped": True},
    )

    result = stop_mcp_daemon(config, timeout=1)

    assert result["stopped"] is False
    assert result["global_ingress_stop"]["stopped"] is True
    assert calls == ["ingress"]


def test_stop_mcp_daemon_without_metadata_stops_matching_orphan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(state_root=tmp_path, runtime="podman", port=9876)
    alive = {"value": True}
    monkeypatch.setattr("workerbee.mcp_daemon._orphan_mcp_pids", lambda _config: [4321])
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda _pid: alive["value"])
    monkeypatch.setattr(
        "workerbee.mcp_daemon._terminate_process_group",
        lambda _pid, *, timeout: alive.update(value=False),  # noqa: ARG005
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon._stop_global_ingress",
        lambda *_args: {"ok": True, "stopped": True},
    )

    result = stop_mcp_daemon(config, timeout=1)

    assert result["stopped"] is True
    assert result["running"] is False
    assert result["orphan_pids"] == [4321]
    assert result["stopped_orphan_pids"] == [4321]


def test_stop_mcp_daemon_without_metadata_uses_containerd_privilege(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(
        state_root=tmp_path,
        runtime="containerd",
        containerd_privilege="sudo-helper",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "workerbee.mcp_daemon.ensure_containerd_privilege",
        lambda **_kwargs: {
            "ok": True,
            "runtime": "containerd",
            "requested_mode": "sudo-helper",
            "helper": {"started": True},
            "env": {"WORKERBEE_NERDCTL_BIN": str(tmp_path / "workerbee-nerdctl")},
        },
    )

    def fake_stop_global(_config, metadata):
        calls.append(metadata["containerd_privilege"]["env"]["WORKERBEE_NERDCTL_BIN"])
        return {"ok": True, "stopped": True, "runtime": "containerd"}

    monkeypatch.setattr("workerbee.mcp_daemon._stop_global_ingress", fake_stop_global)
    monkeypatch.setattr(
        "workerbee.mcp_daemon.stop_containerd_helper",
        lambda _root: calls.append("stop-helper") or {"ok": True, "stopped": True},
    )

    result = stop_mcp_daemon(config, timeout=1)

    assert result["global_ingress_stop"]["stopped"] is True
    assert result["containerd_helper_stop"]["stopped"] is True
    assert calls == [str(tmp_path / "workerbee-nerdctl"), "stop-helper"]


def test_stop_mcp_daemon_stops_containerd_helper_after_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(
        state_root=tmp_path,
        runtime="containerd",
        containerd_privilege="sudo-helper",
    )
    config.global_dir.mkdir(parents=True)
    config.metadata_file.write_text(
        json.dumps(
            {
                "pid": 4321,
                "state_root": str(tmp_path),
                "runtime": "containerd",
                "containerd_privilege_mode": "sudo-helper",
                "containerd_privilege": {
                    "helper": {"started": True},
                    "env": {"WORKERBEE_NERDCTL_BIN": str(tmp_path / "workerbee-nerdctl")},
                },
            }
        ),
        encoding="utf-8",
    )
    alive = {"value": True}
    stop_calls: list[Path] = []
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda _pid: alive["value"])
    monkeypatch.setattr("workerbee.mcp_daemon._pid_matches_metadata", lambda *_args: True)
    monkeypatch.setattr(
        "workerbee.mcp_daemon._terminate_process_group",
        lambda *_args, **_kwargs: alive.update(value=False),
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon._stop_containerd_state_before_helper_stop",
        lambda *_args: {"ok": True, "ingress": {"ok": True, "stopped": True}},
    )

    monkeypatch.setattr(
        "workerbee.mcp_daemon.stop_containerd_helper",
        lambda root: stop_calls.append(root) or {"ok": True, "stopped": True},
    )

    result = stop_mcp_daemon(config, timeout=1)

    assert result["stopped"] is True
    assert stop_calls == [tmp_path]


def test_stop_mcp_daemon_cleans_stale_containerd_before_stopping_helper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(
        state_root=tmp_path,
        runtime="containerd",
        containerd_privilege="sudo-helper",
    )
    config.global_dir.mkdir(parents=True)
    config.metadata_file.write_text(
        json.dumps(
            {
                "pid": 4321,
                "state_root": str(tmp_path),
                "runtime": "containerd",
                "containerd_privilege_mode": "sudo-helper",
                "containerd_privilege": {"helper": {"started": True}},
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda _pid: False)

    def fake_cleanup(*_args):
        calls.append("cleanup")
        return {"ok": True, "ingress": {"ok": True, "stopped": True}}

    def fake_stop_helper(_root):
        calls.append("stop-helper")
        return {"ok": True, "stopped": True}

    monkeypatch.setattr(
        "workerbee.mcp_daemon._stop_containerd_state_before_helper_stop",
        fake_cleanup,
    )
    monkeypatch.setattr("workerbee.mcp_daemon.stop_containerd_helper", fake_stop_helper)

    result = stop_mcp_daemon(config, timeout=1)

    assert result["stale"] is True
    assert calls == ["cleanup", "stop-helper"]
    assert result["global_ingress_stop"]["stopped"] is True
    assert not config.metadata_file.exists()


def test_stop_mcp_daemon_removes_stale_metadata_when_containerd_ingress_cleanup_warns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = MCPDaemonConfig(
        state_root=tmp_path,
        runtime="containerd",
        containerd_privilege="sudo-helper",
    )
    config.global_dir.mkdir(parents=True)
    config.metadata_file.write_text(
        json.dumps(
            {
                "pid": 4321,
                "state_root": str(tmp_path),
                "runtime": "containerd",
                "containerd_privilege": {"helper": {"started": True}},
            }
        ),
        encoding="utf-8",
    )
    stop_calls: list[Path] = []
    monkeypatch.setattr("workerbee.mcp_daemon._pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        "workerbee.mcp_daemon._stop_containerd_state_before_helper_stop",
        lambda *_args: {
            "ok": True,
            "ingress": {"ok": False, "stopped": False, "error": "helper socket missing"},
            "warnings": ["global ingress cleanup failed"],
        },
    )
    monkeypatch.setattr(
        "workerbee.mcp_daemon.stop_containerd_helper",
        lambda root: stop_calls.append(root),
    )

    result = stop_mcp_daemon(config, timeout=1)

    assert result["containerd_cleanup"]["warnings"] == ["global ingress cleanup failed"]
    assert result["global_ingress_stop"]["ok"] is False
    assert stop_calls == [tmp_path]
    assert not config.metadata_file.exists()


def _ready_payload() -> dict[str, str]:
    return {"dashboard_url": "https://dashboard.workerbee.localhost:19443/"}
