"""Background process management for the WorkerBee MCP daemon."""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from errno import EPERM
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from workerbee.containerd_helper import (
    containerd_available_for_auto,
    containerd_privilege_env,
    containerd_privilege_status,
    containerd_privilege_summary,
    ensure_containerd_privilege,
    stop_containerd_helper,
    temporary_containerd_privilege_env,
)
from workerbee.contract import WorkerBeeError
from workerbee.dns import DNSSettings, dns_port_available
from workerbee.http import request, request_https_via_loopback
from workerbee.ingress import (
    IngressSettings,
    global_ingress_status,
    load_global_ingress_info,
    resolve_ingress_settings,
)
from workerbee.paths import default_state_root
from workerbee.runtime_support import CONTAINERD_RUNTIME

MCP_DAEMON_FILE = "mcp-daemon.json"
MCP_DAEMON_LOG = "mcp-daemon.log"
WORKERBEE_ALLOW_REMOTE_MCP_ENV = "WORKERBEE_ALLOW_REMOTE_MCP"


@dataclass(frozen=True, slots=True)
class MCPDaemonConfig:
    state_root: Path
    runtime: str = "auto"
    project: str = "default"
    host: str = "127.0.0.1"
    port: int = 8765
    containerd_privilege: str = "auto"
    allow_remote_mcp: bool = False
    ingress_exposure: str | None = None
    ingress_domain: str | None = None
    ingress_bind: str | None = None
    ingress_ca_port: int | None = None
    ingress_dns: str | None = None
    ingress_dns_port: int | None = None
    ingress_dns_bind: str | None = None
    ingress_dns_answer: str | None = None
    ingress_dns_upstreams: tuple[str, ...] = ()

    @property
    def mcp_url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    @property
    def ingress_settings(self) -> IngressSettings:
        return resolve_ingress_settings(
            exposure=self.ingress_exposure,
            base_domain=self.ingress_domain,
            bind_host=self.ingress_bind,
            ca_http_port=self.ingress_ca_port,
            dns_mode=self.ingress_dns,
            dns_port=self.ingress_dns_port,
            dns_bind=self.ingress_dns_bind,
            dns_answer=self.ingress_dns_answer,
            dns_upstreams=self.ingress_dns_upstreams,
        )

    @property
    def dns_settings(self) -> DNSSettings:
        return self.ingress_settings.dns

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
    allow_remote_mcp: bool = False,
    ingress_exposure: str | None = None,
    ingress_domain: str | None = None,
    ingress_bind: str | None = None,
    ingress_ca_port: int | None = None,
    ingress_dns: str | None = None,
    ingress_dns_port: int | None = None,
    ingress_dns_bind: str | None = None,
    ingress_dns_answer: str | None = None,
    ingress_dns_upstreams: list[str] | tuple[str, ...] | str | None = None,
) -> MCPDaemonConfig:
    return MCPDaemonConfig(
        state_root=(state_root or default_state_root()).resolve(),
        runtime=runtime,
        project=project or "default",
        host=host,
        port=port,
        containerd_privilege=containerd_privilege,
        allow_remote_mcp=allow_remote_mcp,
        ingress_exposure=ingress_exposure,
        ingress_domain=ingress_domain,
        ingress_bind=ingress_bind,
        ingress_ca_port=ingress_ca_port,
        ingress_dns=ingress_dns,
        ingress_dns_port=ingress_dns_port,
        ingress_dns_bind=ingress_dns_bind,
        ingress_dns_answer=ingress_dns_answer,
        ingress_dns_upstreams=_normalize_dns_upstreams(ingress_dns_upstreams),
    )


def _normalize_dns_upstreams(
    upstreams: list[str] | tuple[str, ...] | str | None,
) -> tuple[str, ...]:
    if upstreams is None:
        return ()
    if isinstance(upstreams, str):
        values = upstreams.replace(",", " ").split()
    else:
        values = [
            item
            for value in upstreams
            for item in str(value).replace(",", " ").split()
        ]
    return tuple(value.strip() for value in values if value.strip())


def _preferred_start_config(
    config: MCPDaemonConfig,
) -> tuple[MCPDaemonConfig, dict[str, Any] | None]:
    if config.runtime.lower() != "auto":
        return config, None
    preference = containerd_available_for_auto(
        state_root=config.state_root,
        mode=config.containerd_privilege,
    )
    if preference.get("ok"):
        return replace(config, runtime=CONTAINERD_RUNTIME), preference
    return config, preference


def start_mcp_daemon(config: MCPDaemonConfig, *, timeout: float = 45.0) -> dict[str, Any]:
    try:
        require_mcp_loopback_or_opt_in(
            config.host,
            allow_remote_mcp=config.allow_remote_mcp,
        )
    except WorkerBeeError as exc:
        return _start_error(config, exc)
    try:
        ingress_settings = config.ingress_settings
    except Exception as exc:  # noqa: BLE001
        return _start_error(config, exc, code="INGRESS_CONFIG_INVALID")
    status = mcp_daemon_status(config)
    if status["running"]:
        runtime = str(status.get("runtime") or config.runtime)
        privilege_mode = str(
            status.get("containerd_privilege_mode") or config.containerd_privilege
        )
        if runtime == CONTAINERD_RUNTIME:
            try:
                privilege = ensure_containerd_privilege(
                    state_root=config.state_root,
                    runtime=runtime,
                    mode=privilege_mode,
                )
            except Exception as exc:  # noqa: BLE001
                return _start_error(config, exc, code="CONTAINERD_PRIVILEGE_FAILED")
            refreshed = mcp_daemon_status(config)
            return {
                **refreshed,
                "ok": bool(privilege.get("ok", True)),
                "started": False,
                "containerd_privilege": containerd_privilege_summary(privilege),
                "containerd_privilege_mode": privilege.get("effective_mode"),
            }
        return {**status, "ok": True, "started": False}
    if status.get("stale"):
        stale_stop = stop_mcp_daemon(config, timeout=5.0)
        if stale_stop.get("containerd_cleanup") and not bool(
            stale_stop["containerd_cleanup"].get("ok")
        ):
            return {**stale_stop, "ok": False, "started": False}
    orphan_cleanup = _stop_matching_orphan_mcp_daemons(config, timeout=5.0)
    port_check = _mcp_port_available(config)
    if not port_check["ok"]:
        return {
            **_base_status(config),
            "ok": False,
            "started": False,
            "running": False,
            "error": port_check["error"],
            "orphan_cleanup": orphan_cleanup,
        }
    dns_port_check = dns_port_available(ingress_settings.dns)
    if not dns_port_check["ok"]:
        return {
            **_base_status(config),
            "ok": False,
            "started": False,
            "running": False,
            "error": dns_port_check["error"],
            "orphan_cleanup": orphan_cleanup,
        }
    start_config, auto_runtime_preference = _preferred_start_config(config)
    config.global_dir.mkdir(parents=True, exist_ok=True)
    try:
        privilege = ensure_containerd_privilege(
            state_root=start_config.state_root,
            runtime=start_config.runtime,
            mode=start_config.containerd_privilege,
        )
    except Exception as exc:  # noqa: BLE001
        return _start_error(start_config, exc, code="CONTAINERD_PRIVILEGE_FAILED")
    if privilege.get("ok") is False:
        return _start_error(
            start_config,
            WorkerBeeError(
                code="CONTAINERD_PRIVILEGE_FAILED",
                message="WorkerBee containerd privilege setup did not complete",
                details={"containerd_privilege": privilege},
                retryable=True,
                remediation=(
                    "Check containerd socket access, sudo-helper status, and "
                    "WorkerBee containerd helper logs."
                ),
            ),
        )
    child_privilege_mode = (
        "unprivileged"
        if privilege.get("effective_mode") == "sudo-helper"
        else start_config.containerd_privilege
    )
    allow_remote_mcp = remote_mcp_allowed(config.allow_remote_mcp)
    child_env = os.environ.copy()
    child_env.update(containerd_privilege_env(privilege))
    log = open(start_config.log_file, "ab")  # noqa: SIM115 - passed to daemon child
    argv = [
        sys.executable,
        "-m",
        "workerbee",
        "--state-root",
        str(start_config.state_root),
        "--runtime",
        start_config.runtime,
        "--containerd-privilege",
        child_privilege_mode,
        "--project",
        start_config.project,
        "mcp",
        "serve",
        "--host",
        start_config.host,
        "--port",
        str(start_config.port),
        "--ingress-exposure",
        ingress_settings.exposure,
        "--ingress-domain",
        ingress_settings.base_domain,
        "--ingress-bind",
        ingress_settings.bind_host,
        "--ingress-ca-port",
        str(ingress_settings.ca_http_port),
    ]
    if ingress_settings.dns.enabled:
        argv.extend(
            [
                "--ingress-dns",
                ingress_settings.dns.mode,
                "--ingress-dns-port",
                str(ingress_settings.dns.port),
                "--ingress-dns-bind",
                str(ingress_settings.dns.bind_host or ""),
                "--ingress-dns-answer",
                str(ingress_settings.dns.answer or ""),
            ]
        )
        for upstream in ingress_settings.dns.upstreams:
            argv.extend(["--ingress-dns-upstream", upstream])
    if allow_remote_mcp:
        argv.append("--allow-remote-mcp")
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
    except Exception as exc:  # noqa: BLE001
        if _helper_started(privilege):
            with suppress(Exception):
                stop_containerd_helper(start_config.state_root)
        return _start_error(start_config, exc, code="MCP_SPAWN_FAILED")
    finally:
        log.close()
    metadata = {
        "pid": int(proc.pid),
        "argv": argv,
        "state_root": str(start_config.state_root),
        "runtime": start_config.runtime,
        "requested_runtime": config.runtime,
        "project": start_config.project,
        "host": start_config.host,
        "port": start_config.port,
        "containerd_privilege_mode": start_config.containerd_privilege,
        "allow_remote_mcp": allow_remote_mcp,
        "ingress_exposure": ingress_settings.exposure,
        "ingress_domain": ingress_settings.base_domain,
        "ingress_bind": ingress_settings.bind_host,
        "ingress_ca_port": ingress_settings.ca_http_port,
        "ingress_dns": ingress_settings.dns.public_dict(),
        "containerd_privilege": privilege,
        "auto_runtime_preference": auto_runtime_preference,
        "mcp_url": start_config.mcp_url,
        "log_file": str(start_config.log_file),
        "started_at": time.time(),
    }
    _write_metadata(start_config.metadata_file, metadata)
    try:
        ready = _wait_ready(start_config, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        _cleanup_failed_start(start_config, privilege)
        return _start_error(start_config, exc, code="MCP_NOT_READY")
    metadata.update(ready)
    _write_metadata(start_config.metadata_file, metadata)
    return {**mcp_daemon_status(start_config), "ok": True, "started": True}


def stop_mcp_daemon(config: MCPDaemonConfig, *, timeout: float = 10.0) -> dict[str, Any]:
    metadata = _read_metadata(config.metadata_file)
    if not metadata:
        orphan_pids = _orphan_mcp_pids(config)
        for pid in orphan_pids:
            _terminate_process_group(pid, timeout=timeout)
        remaining_orphans = [pid for pid in orphan_pids if _pid_alive(pid)]
        stopped_orphans = [pid for pid in orphan_pids if pid not in remaining_orphans]
        privilege = _ensure_stop_privilege(config)
        metadata = _metadata_with_privilege(config, metadata, privilege)
        global_ingress_stop = _stop_global_ingress(config, metadata)
        helper_stop = _stop_temporary_helper(config, privilege)
        return {
            **_base_status(config),
            "running": bool(remaining_orphans),
            "stopped": bool(orphan_pids) and not remaining_orphans,
            "orphan_pids": orphan_pids,
            "stopped_orphan_pids": stopped_orphans,
            "global_ingress_stop": global_ingress_stop,
            "containerd_helper_stop": helper_stop,
            "containerd_privilege": containerd_privilege_summary(privilege),
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
    try:
        require_mcp_loopback_or_opt_in(
            config.host,
            allow_remote_mcp=config.allow_remote_mcp,
        )
    except WorkerBeeError as exc:
        start = _start_error(config, exc)
        return {"ok": False, "stop": None, "start": start, "port_release": None}
    try:
        _ = config.ingress_settings
    except Exception as exc:  # noqa: BLE001
        start = _start_error(config, exc, code="INGRESS_CONFIG_INVALID")
        return {"ok": False, "stop": None, "start": start, "port_release": None}
    stop = stop_mcp_daemon(config)
    port_release = _wait_for_port_release(config, timeout=min(max(timeout, 1.0), 10.0))
    if not port_release["ok"]:
        start = {
            **_base_status(config),
            "ok": False,
            "started": False,
            "running": False,
            "error": port_release["error"],
        }
        return {"ok": False, "stop": stop, "start": start, "port_release": port_release}
    start = start_mcp_daemon(config, timeout=timeout)
    return {"ok": bool(start.get("ok")), "stop": stop, "start": start, "port_release": port_release}


def mcp_daemon_status(config: MCPDaemonConfig) -> dict[str, Any]:
    metadata = _read_metadata(config.metadata_file)
    status = _base_status(config)
    if not metadata:
        orphan_pids = _orphan_mcp_pids(config)
        orphan_pid = orphan_pids[0] if len(orphan_pids) == 1 else None
        runtime = config.runtime
        ingress = global_ingress_status(config.state_root, runtime=runtime)
        return {
            **status,
            "running": bool(orphan_pids),
            "stale": False,
            "pid": orphan_pid,
            "metadata_missing": bool(orphan_pids),
            "orphan_pids": orphan_pids,
            "dashboard_url": ingress.get("dashboard_url"),
            "ca_download_url": ingress.get("ca_download_url"),
            "dashboard_ca_download_url": ingress.get("dashboard_ca_download_url"),
            "dashboard_ca_sha256_url": ingress.get("dashboard_ca_sha256_url"),
            "ca_commands": ingress.get("ca_commands") or {},
            "dns": ingress.get("dns") or {},
            "global_dashboard": ingress,
            "containerd_privilege": containerd_privilege_summary(
                containerd_privilege_status(
                    state_root=config.state_root,
                    runtime=runtime,
                    mode=config.containerd_privilege,
                )
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
        "ca_download_url": ingress.get("ca_download_url") or metadata.get("ca_download_url"),
        "dashboard_ca_download_url": ingress.get("dashboard_ca_download_url")
        or metadata.get("dashboard_ca_download_url"),
        "dashboard_ca_sha256_url": ingress.get("dashboard_ca_sha256_url")
        or metadata.get("dashboard_ca_sha256_url"),
        "ca_commands": ingress.get("ca_commands") or metadata.get("ca_commands") or {},
        "dns": ingress.get("dns") or metadata.get("ingress_dns"),
        "global_dashboard": ingress,
        "containerd_privilege": containerd_privilege_summary(
            containerd_privilege_status(
                state_root=config.state_root,
                runtime=runtime,
                mode=privilege_mode,
            )
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
                    "ca_download_url": ingress.get("ca_download_url"),
                    "dashboard_ca_download_url": ingress.get("dashboard_ca_download_url"),
                    "dashboard_ca_sha256_url": ingress.get("dashboard_ca_sha256_url"),
                    "ca_commands": ingress.get("ca_commands") or {},
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
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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


def _wait_for_port_release(config: MCPDaemonConfig, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout, 0.0)
    last = _mcp_port_available(config)
    orphan_cleanup: dict[str, Any] | None = None
    while not last["ok"] and time.monotonic() < deadline:
        if orphan_cleanup is None:
            orphan_cleanup = _stop_matching_orphan_mcp_daemons(config, timeout=2.0)
        time.sleep(0.2)
        last = _mcp_port_available(config)
    if last["ok"]:
        return {
            "ok": True,
            "host": config.host,
            "port": config.port,
            "orphan_cleanup": orphan_cleanup,
        }
    error = dict(last.get("error") or {})
    error["message"] = (
        "WorkerBee MCP port did not become available after stopping the previous daemon"
    )
    error["remediation"] = (
        "Inspect the process still listening on the MCP port, stop it, then retry "
        "`workerbee mcp restart`."
    )
    return {
        "ok": False,
        "host": config.host,
        "port": config.port,
        "error": error,
        "orphan_cleanup": orphan_cleanup,
    }


def _start_error(
    config: MCPDaemonConfig,
    exc: Exception,
    *,
    code: str = "MCP_START_FAILED",
) -> dict[str, Any]:
    if isinstance(exc, WorkerBeeError):
        error = exc.public_dict()
    else:
        error = WorkerBeeError(
            code=code,
            message=str(exc),
            details={
                "mcp_url": config.mcp_url,
                "state_root": str(config.state_root),
                "log_file": str(config.log_file),
            },
            retryable=True,
            remediation="Inspect WorkerBee MCP logs, correct the runtime issue, then retry.",
        ).public_dict()
    return {
        **_base_status(config),
        "ok": False,
        "started": False,
        "running": False,
        "error": error,
    }


def _cleanup_failed_start(config: MCPDaemonConfig, privilege: dict[str, Any]) -> None:
    with suppress(Exception):
        stop_mcp_daemon(config, timeout=5.0)
    with suppress(OSError):
        config.metadata_file.unlink()
    if _helper_started(privilege):
        with suppress(Exception):
            stop_containerd_helper(config.state_root)


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
    ingress = _base_ingress_status(config)
    return {
        "state_root": str(config.state_root),
        "runtime": config.runtime,
        "project": config.project,
        "host": config.host,
        "port": config.port,
        "containerd_privilege_mode": config.containerd_privilege,
        "allow_remote_mcp": remote_mcp_allowed(config.allow_remote_mcp),
        "mcp_url": config.mcp_url,
        "codex_mcp_add": f"codex mcp add workerbee --url {config.mcp_url}",
        "agent_instructions": "workerbee agent instructions",
        "metadata_file": str(config.metadata_file),
        "log_file": str(config.log_file),
        **ingress,
    }


def _base_ingress_status(config: MCPDaemonConfig) -> dict[str, Any]:
    try:
        settings = config.ingress_settings
    except Exception as exc:  # noqa: BLE001 - status should report invalid config
        return {
            "ingress_exposure": config.ingress_exposure,
            "ingress_domain": config.ingress_domain,
            "ingress_bind": config.ingress_bind,
            "ingress_ca_port": config.ingress_ca_port,
            "ingress_dns": config.ingress_dns,
            "ingress_dns_port": config.ingress_dns_port,
            "ingress_dns_bind": config.ingress_dns_bind,
            "ingress_dns_answer": config.ingress_dns_answer,
            "ingress_dns_upstreams": list(config.ingress_dns_upstreams),
            "ingress_config_error": str(exc),
        }
    return {
        "ingress_exposure": settings.exposure,
        "ingress_domain": settings.base_domain,
        "ingress_bind": settings.bind_host,
        "ingress_ca_port": settings.ca_http_port,
        "ingress_dns": settings.dns.public_dict(),
    }


def require_mcp_loopback_or_opt_in(host: str, *, allow_remote_mcp: bool = False) -> None:
    if _mcp_host_is_loopback(host) or remote_mcp_allowed(allow_remote_mcp):
        return
    raise WorkerBeeError(
        code="MCP_REMOTE_BIND_REQUIRES_AUTH",
        message="Refusing to expose WorkerBee MCP on a non-loopback host without opt-in",
        details={
            "host": host,
            "allow_env": WORKERBEE_ALLOW_REMOTE_MCP_ENV,
        },
        remediation=(
            "Bind WorkerBee MCP to 127.0.0.1/localhost, or pass --allow-remote-mcp "
            f"or set {WORKERBEE_ALLOW_REMOTE_MCP_ENV}=1 for a controlled local network "
            "test. WorkerBee does not yet implement standards-compliant MCP OAuth "
            "authorization for remote exposure."
        ),
    )


def remote_mcp_allowed(explicit: bool = False) -> bool:
    return explicit or str(os.getenv(WORKERBEE_ALLOW_REMOTE_MCP_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _mcp_host_is_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


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
    except PermissionError as exc:
        return getattr(exc, "errno", None) == EPERM
    except OSError:
        return False


def _pid_matches_metadata(pid: int, config: MCPDaemonConfig, metadata: dict[str, Any]) -> bool:
    if str(metadata.get("state_root") or "") != str(config.state_root):
        return False
    parts = _proc_cmdline_parts(pid)
    if not parts:
        return True
    joined = " ".join(parts)
    return (
        "-m workerbee" in joined
        and "mcp" in parts
        and "serve" in parts
        and str(config.state_root) in joined
    )


def _orphan_mcp_pids(config: MCPDaemonConfig) -> list[int]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return _fallback_orphan_mcp_pids(config)
    pids: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        parts = _proc_cmdline_parts(pid)
        if parts and _argv_matches_mcp_config(parts, config):
            pids.append(pid)
    return pids


def _fallback_orphan_mcp_pids(config: MCPDaemonConfig) -> list[int]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        if not pid_text.isdigit() or not command:
            continue
        pid = int(pid_text)
        if pid == os.getpid():
            continue
        parts = _split_command_line(command)
        if parts and _argv_matches_mcp_config(parts, config):
            pids.append(pid)
    return pids


def _stop_matching_orphan_mcp_daemons(
    config: MCPDaemonConfig,
    *,
    timeout: float,
) -> dict[str, Any]:
    orphan_pids = _orphan_mcp_pids(config)
    for pid in orphan_pids:
        _terminate_process_group(pid, timeout=timeout)
    remaining = [pid for pid in orphan_pids if _pid_alive(pid)]
    return {
        "ok": not remaining,
        "orphan_pids": orphan_pids,
        "stopped_orphan_pids": [pid for pid in orphan_pids if pid not in remaining],
        "remaining_orphan_pids": remaining,
    }


def _proc_cmdline_parts(pid: int) -> list[str]:
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    if not proc_cmdline.is_file():
        return []
    try:
        raw = proc_cmdline.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [part for part in raw.split("\0") if part]


def _argv_matches_mcp_config(parts: list[str], config: MCPDaemonConfig) -> bool:
    if "mcp" not in parts or "serve" not in parts:
        return False
    executable = Path(parts[0]).name if parts else ""
    module_workerbee = "-m" in parts and "workerbee" in parts
    executable_workerbee = executable.startswith("workerbee")
    if not module_workerbee and not executable_workerbee:
        return False
    state_root = _argv_option(parts, "--state-root")
    if not state_root or Path(state_root).expanduser().resolve() != config.state_root.resolve():
        return False
    port = _argv_option(parts, "--port")
    if port != str(config.port):
        return False
    host = _argv_option(parts, "--host")
    return host in (None, config.host)


def _argv_option(parts: list[str], name: str) -> str | None:
    if name not in parts:
        return None
    index = parts.index(name) + 1
    if index >= len(parts):
        return None
    return parts[index]


def _split_command_line(command: str) -> list[str]:
    try:
        return [part for part in shlex.split(command, posix=True) if part]
    except Exception:
        return command.split()


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


def _existing_ingress_runtime(config: MCPDaemonConfig, fallback: str) -> str:
    ingress = load_global_ingress_info(config.state_root) or {}
    runtime = str(ingress.get("runtime") or "").strip()
    return runtime or fallback


def _metadata_privilege_mode(config: MCPDaemonConfig, metadata: dict[str, Any]) -> str:
    return str(metadata.get("containerd_privilege_mode") or config.containerd_privilege)


def _metadata_privilege_env(metadata: dict[str, Any]) -> dict[str, str]:
    privilege = metadata.get("containerd_privilege")
    if isinstance(privilege, dict):
        return containerd_privilege_env(privilege)
    return {}


def _ensure_stop_privilege(config: MCPDaemonConfig) -> dict[str, Any] | None:
    runtime = _existing_ingress_runtime(config, config.runtime)
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
    runtime = _existing_ingress_runtime(config, _metadata_runtime(config, metadata))
    ingress = cleanup_result.get("ingress") if isinstance(cleanup_result, dict) else None
    if isinstance(ingress, dict):
        return ingress
    if runtime == CONTAINERD_RUNTIME:
        return None
    return _stop_global_ingress(config, metadata)


def _stop_global_ingress(
    config: MCPDaemonConfig,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    from workerbee.daemon import WorkerBeeDaemon

    runtime = _existing_ingress_runtime(config, _metadata_runtime(config, metadata))
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
