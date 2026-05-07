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

from workerbee.containerd_helper import (
    containerd_privilege_env,
    containerd_privilege_status,
    ensure_containerd_privilege,
    stop_containerd_helper,
    temporary_containerd_privilege_env,
)
from workerbee.http import request
from workerbee.ingress import load_global_ingress_info
from workerbee.paths import default_state_root
from workerbee.runtime_support import CONTAINERD_RUNTIME

MCP_DAEMON_FILE = "mcp-daemon.json"
MCP_DAEMON_LOG = "mcp-daemon.log"


@dataclass(frozen=True, slots=True)
class MCPDaemonConfig:
    state_root: Path
    runtime: str = "auto"
    project: str = "default"
    host: str = "127.0.0.1"
    port: int = 8765
    containerd_privilege: str = "auto"

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
    containerd_privilege: str = "auto",
) -> MCPDaemonConfig:
    return MCPDaemonConfig(
        state_root=(state_root or default_state_root()).resolve(),
        runtime=runtime,
        project=project or "default",
        host=host,
        port=port,
        containerd_privilege=containerd_privilege,
    )


def start_mcp_daemon(config: MCPDaemonConfig, *, timeout: float = 45.0) -> dict[str, Any]:
    status = mcp_daemon_status(config)
    if status["running"]:
        return {**status, "ok": True, "started": False}
    if status.get("stale"):
        stale_stop = stop_mcp_daemon(config, timeout=5.0)
        if stale_stop.get("containerd_cleanup") and not bool(
            stale_stop["containerd_cleanup"].get("ok")
        ):
            return {**stale_stop, "ok": False, "started": False}
    config.global_dir.mkdir(parents=True, exist_ok=True)
    privilege = ensure_containerd_privilege(
        state_root=config.state_root,
        runtime=config.runtime,
        mode=config.containerd_privilege,
    )
    child_privilege_mode = (
        "unprivileged"
        if privilege.get("effective_mode") == "sudo-helper"
        else config.containerd_privilege
    )
    child_env = os.environ.copy()
    child_env.update(containerd_privilege_env(privilege))
    log = open(config.log_file, "ab")  # noqa: SIM115 - passed to daemon child
    argv = [
        sys.executable,
        "-m",
        "workerbee",
        "--state-root",
        str(config.state_root),
        "--runtime",
        config.runtime,
        "--containerd-privilege",
        child_privilege_mode,
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
            env=child_env,
        )
    except Exception:
        if _helper_started(privilege):
            with suppress(Exception):
                stop_containerd_helper(config.state_root)
        raise
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
        "containerd_privilege_mode": config.containerd_privilege,
        "containerd_privilege": privilege,
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
        cleanup_result = _stop_containerd_state_before_helper_stop(config, metadata)
        if bool((cleanup_result or {}).get("ok", True)):
            helper_stop = _stop_metadata_helper(config, metadata)
            _cleanup_stale_metadata(config)
        else:
            helper_stop = {
                "ok": False,
                "stopped": False,
                "reason": "WorkerBee containerd cleanup failed; leaving helper running",
            }
        return {
            **_base_status(config),
            "running": False,
            "stale": True,
            "stopped": False,
            "containerd_cleanup": cleanup_result,
            "containerd_helper_stop": helper_stop,
        }
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
    cleanup_result = None
    helper_stop = None
    if stopped:
        cleanup_result = _stop_containerd_state_before_helper_stop(config, metadata)
        if bool((cleanup_result or {}).get("ok", True)):
            helper_stop = _stop_metadata_helper(config, metadata)
        else:
            helper_stop = {
                "ok": False,
                "stopped": False,
                "reason": "WorkerBee containerd cleanup failed; leaving helper running",
            }
    if stopped and bool((cleanup_result or {}).get("ok", True)):
        with suppress(OSError):
            config.metadata_file.unlink()
    return {
        **_base_status(config),
        "running": not stopped,
        "stopped": stopped,
        "pid": pid,
        "containerd_cleanup": cleanup_result,
        "containerd_helper_stop": helper_stop,
    }


def restart_mcp_daemon(config: MCPDaemonConfig, *, timeout: float = 45.0) -> dict[str, Any]:
    stop = stop_mcp_daemon(config)
    start = start_mcp_daemon(config, timeout=timeout)
    return {"ok": bool(start.get("ok")), "stop": stop, "start": start}


def mcp_daemon_status(config: MCPDaemonConfig) -> dict[str, Any]:
    metadata = _read_metadata(config.metadata_file)
    status = _base_status(config)
    if not metadata:
        return {
            **status,
            "running": False,
            "stale": False,
            "containerd_privilege": containerd_privilege_status(
                state_root=config.state_root,
                runtime=config.runtime,
                mode=config.containerd_privilege,
            ),
        }
    pid = _metadata_pid(metadata)
    running = bool(pid and _pid_alive(pid) and _pid_matches_metadata(pid, config, metadata))
    stale = bool(pid and not running)
    ingress = load_global_ingress_info(config.state_root) or {}
    runtime = str(metadata.get("runtime") or config.runtime)
    privilege_mode = str(metadata.get("containerd_privilege_mode") or config.containerd_privilege)
    return {
        **status,
        **metadata,
        "pid": pid,
        "running": running,
        "stale": stale,
        "dashboard_url": ingress.get("dashboard_url") or metadata.get("dashboard_url"),
        "global_dashboard": ingress or None,
        "containerd_privilege": containerd_privilege_status(
            state_root=config.state_root,
            runtime=runtime,
            mode=privilege_mode,
        ),
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
        "containerd_privilege_mode": config.containerd_privilege,
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


def _metadata_runtime(config: MCPDaemonConfig, metadata: dict[str, Any]) -> str:
    return str(metadata.get("runtime") or config.runtime)


def _metadata_privilege_mode(config: MCPDaemonConfig, metadata: dict[str, Any]) -> str:
    return str(metadata.get("containerd_privilege_mode") or config.containerd_privilege)


def _metadata_privilege_env(metadata: dict[str, Any]) -> dict[str, str]:
    privilege = metadata.get("containerd_privilege")
    if isinstance(privilege, dict):
        return containerd_privilege_env(privilege)
    return {}


def _stop_metadata_helper(
    config: MCPDaemonConfig,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    runtime = _metadata_runtime(config, metadata)
    if runtime != CONTAINERD_RUNTIME:
        return None
    _ = _metadata_privilege_mode(config, metadata)
    with temporary_containerd_privilege_env(_metadata_privilege_env(metadata)):
        return stop_containerd_helper(config.state_root)


def _stop_containerd_state_before_helper_stop(
    config: MCPDaemonConfig,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    runtime = _metadata_runtime(config, metadata)
    if runtime != CONTAINERD_RUNTIME:
        return None
    from workerbee.daemon import WorkerBeeDaemon

    with temporary_containerd_privilege_env(_metadata_privilege_env(metadata)):
        daemon = WorkerBeeDaemon(
            state_root=config.state_root,
            runtime=runtime,
            default_project=str(metadata.get("project") or config.project),
        )
        projects = daemon.stop_all_projects(purge=False)
        ingress = daemon.stop_global_ingress()
    return {
        "ok": bool(projects.get("ok")) and bool(ingress.get("ok")),
        "projects": projects,
        "ingress": ingress,
    }


def _helper_started(privilege: dict[str, Any]) -> bool:
    helper = privilege.get("helper") if isinstance(privilege, dict) else None
    return isinstance(helper, dict) and bool(helper.get("started"))
