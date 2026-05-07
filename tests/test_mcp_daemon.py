from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workerbee.mcp_daemon import (
    MCPDaemonConfig,
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

    def fake_terminate(_pid: int, *, timeout: float) -> None:
        _ = timeout
        alive["value"] = False

    monkeypatch.setattr("workerbee.mcp_daemon._terminate_process_group", fake_terminate)

    result = stop_mcp_daemon(config, timeout=1)

    assert result["stopped"] is True
    assert not config.metadata_file.exists()


def _ready_payload() -> dict[str, str]:
    return {"dashboard_url": "https://dashboard.workerbee.localhost:19443/"}
