from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workerbee.mcp_daemon import (
    MCPDaemonConfig,
    _wait_ready,
    mcp_daemon_status,
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
