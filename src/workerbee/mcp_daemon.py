"""Background process management for the WorkerBee MCP daemon."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workerbee.http import request
from workerbee.ingress import load_global_ingress_info
from workerbee.paths import default_state_root

MCP_DAEMON_FILE = "mcp-daemon.json"
MCP_DAEMON_LOG = "mcp-daemon.log"


@dataclass(frozen=True, slots=True)
class MCPDaemonConfig:
    state_root: Path
    runtime: str = "auto"
    project: str = "default"
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def mcp_url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    @property
    def global_dir(self) -> Path:
        return self.state_root.resolve() / "global"

    @property
    def metadata_file(self) -> Path:
        return self.global_dir / MCP_DAEMON_FILE

    @property
    def log_file(self) -> Path:
        return self.global_dir / MCP_DAEMON_LOG


def config_from_args(
    *,
    state_root: Path | None,
    runtime: str,
    project: str | None,
    host: str,
    port: int,
) -> MCPDaemonConfig:
    return MCPDaemonConfig(
        state_root=(state_root or default_state_root()).resolve(),
        runtime=runtime,
        project=project or "default",
        host=host,
        port=port,
    )


def start_mcp_daemon(config: MCPDaemonConfig, *, timeout: float = 45.0) -> dict[str, Any]:
    status = mcp_daemon_status(config)
    if status["running"]:
        return {**status, "ok": True, "started": False}
    if status.get("stale"):
        _cleanup_stale_metadata(config)
    config.global_dir.mkdir(parents=True, exist_ok=True)
    log = open(config.log_file, "ab")  # noqa: SIM115 - passed to daemon child
    argv = [
        sys.executable,
        "-m",
        "workerbee",
        "--state-root",
        str(config.state_root),
        "--runtime",
        config.runtime,
        "--project",
        config.project,
        "mcp",
        "serve",
        "--host",
        config.host,
        "--port",
        str(config.port),
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()
    metadata = {
        "pid": int(proc.pid),
        "argv": argv,
        "state_root": str(config.state_root),
        "runtime": config.runtime,
        "project": config.project,
        "host": config.host,
        "port": config.port,
        "mcp_url": config.mcp_url,
        "log_file": str(config.log_file),
        "started_at": time.time(),
    }
    _write_metadata(config.metadata_file, metadata)
    try:
        ready = _wait_ready(config, timeout=timeout)
    except Exception:
        with suppress(Exception):
            stop_mcp_daemon(config, timeout=5.0)
        raise
    metadata.update(ready)
    _write_metadata(config.metadata_file, metadata)
    return {**mcp_daemon_status(config), "ok": True, "started": True}


def stop_mcp_daemon(config: MCPDaemonConfig, *, timeout: float = 10.0) -> dict[str, Any]:
    metadata = _read_metadata(config.metadata_file)
    if not metadata:
        return {**_base_status(config), "running": False, "stopped": False}
    pid = _metadata_pid(metadata)
    if pid is None or not _pid_alive(pid):
        _cleanup_stale_metadata(config)
        return {**_base_status(config), "running": False, "stale": True, "stopped": False}
    if not _pid_matches_metadata(pid, config, metadata):
        return {
            **_base_status(config),
            "running": False,
            "stopped": False,
            "error": "PID does not look like the WorkerBee MCP daemon for this state root",
            "pid": pid,
        }
    _terminate_process_group(pid, timeout=timeout)
    stopped = not _pid_alive(pid)
    if stopped:
        with suppress(OSError):
            config.metadata_file.unlink()
    return {**_base_status(config), "running": not stopped, "stopped": stopped, "pid": pid}


def restart_mcp_daemon(config: MCPDaemonConfig, *, timeout: float = 45.0) -> dict[str, Any]:
    stop = stop_mcp_daemon(config)
    start = start_mcp_daemon(config, timeout=timeout)
    return {"ok": bool(start.get("ok")), "stop": stop, "start": start}


def mcp_daemon_status(config: MCPDaemonConfig) -> dict[str, Any]:
    metadata = _read_metadata(config.metadata_file)
    status = _base_status(config)
    if not metadata:
        return {**status, "running": False, "stale": False}
    pid = _metadata_pid(metadata)
    running = bool(pid and _pid_alive(pid) and _pid_matches_metadata(pid, config, metadata))
    stale = bool(pid and not running)
    ingress = load_global_ingress_info(config.state_root) or {}
    return {
        **status,
        **metadata,
        "pid": pid,
        "running": running,
        "stale": stale,
        "dashboard_url": ingress.get("dashboard_url") or metadata.get("dashboard_url"),
        "global_dashboard": ingress or None,
    }


def _wait_ready(config: MCPDaemonConfig, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _raise_if_dead(config)
        if _tcp_ready(config.host, config.port):
            ingress = load_global_ingress_info(config.state_root) or {}
            dashboard_url = str(ingress.get("dashboard_url") or "")
            if dashboard_url:
                try:
                    request(dashboard_url, timeout=2.0, verify_tls=False)
                    return {
                        "dashboard_url": dashboard_url,
                        "global_dashboard": ingress,
                        "ready_at": time.time(),
                    }
                except OSError:
                    pass
        time.sleep(0.25)
    raise TimeoutError(f"WorkerBee MCP did not become ready at {config.mcp_url}")


def _raise_if_dead(config: MCPDaemonConfig) -> None:
    metadata = _read_metadata(config.metadata_file)
    pid = _metadata_pid(metadata)
    if pid is not None and not _pid_alive(pid):
        raise RuntimeError(f"WorkerBee MCP daemon exited early; inspect {config.log_file}")


def _tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _base_status(config: MCPDaemonConfig) -> dict[str, Any]:
    return {
        "state_root": str(config.state_root),
        "runtime": config.runtime,
        "project": config.project,
        "host": config.host,
        "port": config.port,
        "mcp_url": config.mcp_url,
        "metadata_file": str(config.metadata_file),
        "log_file": str(config.log_file),
    }


def _read_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _metadata_pid(metadata: dict[str, Any]) -> int | None:
    try:
        pid = int(metadata.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_matches_metadata(pid: int, config: MCPDaemonConfig, metadata: dict[str, Any]) -> bool:
    if str(metadata.get("state_root") or "") != str(config.state_root):
        return False
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    if not proc_cmdline.is_file():
        return True
    try:
        raw = proc_cmdline.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return True
    parts = [part for part in raw.split("\0") if part]
    joined = " ".join(parts)
    return (
        "-m workerbee" in joined
        and "mcp" in parts
        and "serve" in parts
        and str(config.state_root) in joined
    )


def _terminate_process_group(pid: int, *, timeout: float) -> None:
    with suppress(OSError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.2)
    with suppress(OSError):
        os.killpg(pid, signal.SIGKILL)


def _cleanup_stale_metadata(config: MCPDaemonConfig) -> None:
    with suppress(OSError):
        config.metadata_file.unlink()
