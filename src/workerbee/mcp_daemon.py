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
from urllib.parse import SplitResult, urlsplit, urlunsplit

from workerbee.containerd_helper import (
    containerd_privilege_env,
    containerd_privilege_status,
    ensure_containerd_privilege,
    stop_containerd_helper,
    temporary_containerd_privilege_env,
)
from workerbee.http import request, request_https_via_loopback
from workerbee.ingress import global_ingress_status, load_global_ingress_info
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
        if config.runtime == CONTAINERD_RUNTIME:
            privilege = ensure_containerd_privilege(
                state_root=config.state_root,
                runtime=config.runtime,
                mode=config.containerd_privilege,
            )
            refreshed = mcp_daemon_status(config)
            return {
                **refreshed,
                "ok": bool(privilege.get("ok", True)),
                "started": False,
                "containerd_privilege": privilege,
                "containerd_privilege_mode": privilege.get("effective_mode"),
            }
        return {**status, "ok": True, "started": False}
    if status.get("stale"):
        stale_stop = stop_mcp_daemon(config, timeout=5.0)
        if stale_stop.get("containerd_cleanup") and not bool(
            stale_stop["containerd_cleanup"].get("ok")
        ):
            return {**stale_stop, "ok": False, "started": False}
    port_check = _mcp_port_available(config)
    if not port_check["ok"]:
        return {
            **_base_status(config),
            "ok": False,
            "started": False,
            "running": False,
            "error": port_check["error"],
        }
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
        privilege = _ensure_stop_privilege(config)
        metadata = _metadata_with_privilege(config, metadata, privilege)
        global_ingress_stop = _stop_global_ingress(config, metadata)
        helper_stop = _stop_temporary_helper(config, privilege)
        return {
            **_base_status(config),
            "running": False,
            "stopped": False,
            "global_ingress_stop": global_ingress_stop,
            "containerd_helper_stop": helper_stop,
            "containerd_privilege": privilege,
        }
    pid = _metadata_pid(metadata)
    if pid is None or not _pid_alive(pid):
        cleanup_result = _stop_containerd_state_before_helper_stop(config, metadata)
        global_ingress_stop = _global_ingress_stop_result(config, metadata, cleanup_result)
        helper_stop = _stop_metadata_helper(config, metadata)
        _cleanup_stale_metadata(config)
        return {
            **_base_status(config),
            "running": False,
            "stale": True,
            "stopped": False,
            "containerd_cleanup": cleanup_result,
            "containerd_helper_stop": helper_stop,
            "global_ingress_stop": global_ingress_stop,
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
    global_ingress_stop = None
    if stopped:
        cleanup_result = _stop_containerd_state_before_helper_stop(config, metadata)
        global_ingress_stop = _global_ingress_stop_result(config, metadata, cleanup_result)
        helper_stop = _stop_metadata_helper(config, metadata)
    if stopped:
        with suppress(OSError):
            config.metadata_file.unlink()
    return {
        **_base_status(config),
        "running": not stopped,
        "stopped": stopped,
        "pid": pid,
        "containerd_cleanup": cleanup_result,
        "containerd_helper_stop": helper_stop,
        "global_ingress_stop": global_ingress_stop,
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
    runtime = str(metadata.get("runtime") or config.runtime)
    privilege_mode = str(metadata.get("containerd_privilege_mode") or config.containerd_privilege)
    ingress = global_ingress_status(config.state_root, runtime=runtime)
    return {
        **status,
        **metadata,
        "pid": pid,
        "running": running,
        "stale": stale,
        "dashboard_url": ingress.get("dashboard_url") or metadata.get("dashboard_url"),
        "global_dashboard": ingress,
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
        if _daemon_process_ready(config) and _tcp_ready(config.host, config.port):
            ingress = load_global_ingress_info(config.state_root) or {}
            dashboard_url = str(ingress.get("dashboard_url") or "")
            if dashboard_url and _dashboard_healthy(dashboard_url):
                _raise_if_dead(config)
                return {
                    "dashboard_url": dashboard_url,
                    "global_dashboard": ingress,
                    "ready_at": time.time(),
                }
        time.sleep(0.25)
    raise TimeoutError(f"WorkerBee MCP did not become ready at {config.mcp_url}")


def _dashboard_health_url(dashboard_url: str) -> str:
    return f"{dashboard_url.rstrip('/')}/healthz"


def _dashboard_healthy(dashboard_url: str) -> bool:
    parsed = urlsplit(dashboard_url)
    try:
        result = request(_dashboard_health_url(dashboard_url), timeout=2.0, verify_tls=False)
        if int(getattr(result, "status", 200)) == 200:
            return True
    except OSError:
        pass
    loopback = _loopback_dashboard_health_url(parsed)
    if not loopback:
        return False
    try:
        result = request_https_via_loopback(
            loopback,
            timeout=2.0,
            server_hostname=parsed.hostname or "dashboard.workerbee.localhost",
            host_header=parsed.netloc,
            verify_tls=False,
        )
    except OSError:
        return False
    return int(getattr(result, "status", 200)) == 200


def _loopback_dashboard_health_url(parsed: SplitResult) -> str | None:
    if parsed.scheme != "https" or not parsed.port:
        return None
    return urlunsplit((parsed.scheme, f"127.0.0.1:{parsed.port}", "/healthz", "", ""))


def _raise_if_dead(config: MCPDaemonConfig) -> None:
    metadata = _read_metadata(config.metadata_file)
    pid = _metadata_pid(metadata)
    if pid is not None and not _pid_alive(pid):
        raise RuntimeError(f"WorkerBee MCP daemon exited early; inspect {config.log_file}")


def _daemon_process_ready(config: MCPDaemonConfig) -> bool:
    metadata = _read_metadata(config.metadata_file)
    pid = _metadata_pid(metadata)
    return bool(pid and _pid_alive(pid) and _pid_matches_metadata(pid, config, metadata))


def _tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _mcp_port_available(config: MCPDaemonConfig) -> dict[str, Any]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((config.host, int(config.port)))
    except OSError as exc:
        return {
            "ok": False,
            "error": {
                "code": "MCP_PORT_IN_USE",
                "message": f"WorkerBee MCP port {config.host}:{config.port} is already in use",
                "details": {
                    "host": config.host,
                    "port": config.port,
                    "mcp_url": config.mcp_url,
                    "owner": _port_owner_details(config.host, config.port),
                    "error": str(exc),
                },
                "retryable": True,
                "remediation": (
                    "Stop the process using this port or run "
                    "`workerbee mcp start --port <port>`."
                ),
            },
        }
    return {"ok": True}


def _port_owner_details(host: str, port: int) -> dict[str, Any]:
    details: dict[str, Any] = {"host": host, "port": int(port)}
    for label, argv in (
        ("ss", ["ss", "-ltnp", f"sport = :{int(port)}"]),
        ("lsof", ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN"]),
    ):
        proc = None
        with suppress(Exception):
            proc = subprocess.run(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=3,
                check=False,
            )
        if proc is None:
            continue
        output = (proc.stdout or "").strip()
        if output:
            details[label] = output
    return details


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


def _ensure_stop_privilege(config: MCPDaemonConfig) -> dict[str, Any] | None:
    runtime = config.runtime
    if runtime != CONTAINERD_RUNTIME:
        ingress = load_global_ingress_info(config.state_root) or {}
        runtime = str(ingress.get("runtime") or runtime)
    if runtime != CONTAINERD_RUNTIME:
        return None
    return ensure_containerd_privilege(
        state_root=config.state_root,
        runtime=runtime,
        mode=config.containerd_privilege,
    )


def _metadata_with_privilege(
    config: MCPDaemonConfig,
    metadata: dict[str, Any],
    privilege: dict[str, Any] | None,
) -> dict[str, Any]:
    if not privilege:
        return metadata
    merged = dict(metadata)
    merged.setdefault("runtime", privilege.get("runtime") or config.runtime)
    merged["containerd_privilege"] = privilege
    merged["containerd_privilege_mode"] = (
        privilege.get("requested_mode")
        or privilege.get("effective_mode")
        or config.containerd_privilege
    )
    return merged


def _stop_temporary_helper(
    config: MCPDaemonConfig,
    privilege: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not privilege or privilege.get("runtime") != CONTAINERD_RUNTIME:
        return None
    helper = privilege.get("helper")
    if not isinstance(helper, dict) or not bool(helper.get("started")):
        return None
    with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
        return stop_containerd_helper(config.state_root)


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


def _global_ingress_stop_result(
    config: MCPDaemonConfig,
    metadata: dict[str, Any],
    cleanup_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    runtime = _metadata_runtime(config, metadata)
    if runtime == CONTAINERD_RUNTIME:
        ingress = cleanup_result.get("ingress") if isinstance(cleanup_result, dict) else None
        return ingress if isinstance(ingress, dict) else None
    return _stop_global_ingress(config, metadata)


def _stop_global_ingress(
    config: MCPDaemonConfig,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    from workerbee.daemon import WorkerBeeDaemon

    runtime = _metadata_runtime(config, metadata)
    if not metadata:
        ingress = load_global_ingress_info(config.state_root) or {}
        runtime = str(ingress.get("runtime") or runtime)
    with temporary_containerd_privilege_env(_metadata_privilege_env(metadata)):
        try:
            daemon = WorkerBeeDaemon(
                state_root=config.state_root,
                runtime=runtime,
                default_project=str(metadata.get("project") or config.project),
            )
            return daemon.stop_global_ingress()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "stopped": False,
                "runtime": runtime,
                "error": str(exc),
            }


def _stop_containerd_state_before_helper_stop(
    config: MCPDaemonConfig,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    runtime = _metadata_runtime(config, metadata)
    if runtime != CONTAINERD_RUNTIME:
        return None
    from workerbee.daemon import WorkerBeeDaemon

    warnings: list[str] = []
    with temporary_containerd_privilege_env(_metadata_privilege_env(metadata)):
        daemon = WorkerBeeDaemon(
            state_root=config.state_root,
            runtime=runtime,
            default_project=str(metadata.get("project") or config.project),
        )
        try:
            projects = daemon.stop_all_projects(purge=False)
        except Exception as exc:  # noqa: BLE001
            projects = {
                "ok": False,
                "errors": [{"error": str(exc)}],
                "projects": [],
                "state_root": str(config.state_root),
            }
        try:
            ingress = daemon.stop_global_ingress()
        except Exception as exc:  # noqa: BLE001
            ingress = {"ok": False, "stopped": False, "runtime": runtime, "error": str(exc)}
    if not bool(ingress.get("ok")):
        warnings.append(
            "global ingress cleanup failed; stale containerd helper may already be gone"
        )
    return {
        "ok": bool(projects.get("ok")),
        "projects": projects,
        "ingress": ingress,
        "warnings": warnings,
    }


def _helper_started(privilege: dict[str, Any]) -> bool:
    helper = privilege.get("helper") if isinstance(privilege, dict) else None
    return isinstance(helper, dict) and bool(helper.get("started"))
