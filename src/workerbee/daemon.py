"""Multi-project WorkerBee MCP daemon state."""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit, urlunsplit

from workerbee.agent import (
    DEFAULT_PROJECT_MODE,
    derive_session_project_info,
    next_actions_for_mode,
    normalize_project_mode,
    open_browser,
    runbook_payload,
    user_message_for_session,
)
from workerbee.containerd_helper import containerd_privilege_status, containerd_privilege_summary
from workerbee.contract import WorkerBeeError
from workerbee.http import request, request_https_via_loopback
from workerbee.ingress import (
    GlobalIngress,
    GlobalIngressInfo,
    ProjectIngressConfig,
    global_ingress_status,
)
from workerbee.locks import FileLock, project_lock_path, state_root_lock_path
from workerbee.manifests import deploy_profile_stage, export_bundle, prepare_stage
from workerbee.paths import daemon_project_state_dir, default_state_root
from workerbee.ports import choose_port
from workerbee.probe import build_probe_url, probe_workerbee_url
from workerbee.profiles import K1sProfileRunner, builtin_profiles
from workerbee.runtime_support import (
    cleanup_runtime,
    resolve_runtime,
    runtime_diagnostics,
)
from workerbee.secrets import secret_policy_status
from workerbee.security import DEFAULT_SECURITY_CHECKS, assess_stage_security
from workerbee.supervisor import WorkerBeeSupervisor, project_slug

T = TypeVar("T")

DASHBOARD_BACKGROUND_PATH = "/static/dash-assets/page-background-1920x1080.webp"
DASHBOARD_LOGO_PATH = "/static/dash-assets/k1s-logo-32.png"
_DASHBOARD_ACTIONS = {
    "start_projects",
    "stop_projects",
    "delete_projects",
    "start_all_projects",
    "stop_all_projects",
    "delete_all_projects",
    "mcp_shutdown",
    "mcp_reboot",
}


@dataclass(slots=True)
class ProjectRecord:
    project: str
    state_dir: str
    cwd_hint: str
    mode: str
    created_at: float
    last_seen_at: float
    git_root: str | None = None
    git_branch: str | None = None
    explicit_project: bool = False


@dataclass(slots=True)
class DashboardActionJob:
    job_id: str
    action: str
    projects: list[str]
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    ok: bool | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "projects": list(self.projects),
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
        }


class WorkerBeeDaemon:
    def __init__(
        self,
        *,
        state_root: Path | None = None,
        runtime: str = "auto",
        default_project: str = "default",
        cwd: Path | None = None,
    ) -> None:
        self.state_root = (state_root or default_state_root()).resolve()
        self.runtime_requested = runtime
        self.default_project = project_slug(default_project)
        self.cwd = (cwd or Path.cwd()).resolve()
        self.projects_dir = self.state_root / "projects"
        self.registry_file = self.state_root / "registry.json"
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._dashboard: ThreadingHTTPServer | None = None
        self._dashboard_thread: threading.Thread | None = None
        self.dashboard_action_token = secrets.token_urlsafe(32)
        self._dashboard_shutdown_callback: Callable[[], None] | None = None
        self._dashboard_reboot_callback: Callable[[], None] | None = None
        self._dashboard_scheduler: Callable[[Callable[[], None]], None] | None = None
        self._dashboard_jobs: dict[str, DashboardActionJob] = {}
        self._dashboard_jobs_order: list[str] = []
        self._dashboard_jobs_lock = threading.Lock()
        self.ingress: GlobalIngress | None = None
        self._state_lock: FileLock | None = None

    def configure_dashboard_lifecycle(
        self,
        *,
        shutdown: Callable[[], None] | None = None,
        reboot: Callable[[], None] | None = None,
        scheduler: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._dashboard_shutdown_callback = shutdown
        self._dashboard_reboot_callback = reboot
        self._dashboard_scheduler = scheduler

    def start(self, *, mcp_bind_url: str | None = None) -> GlobalIngressInfo:
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        if self._state_lock is None:
            self._state_lock = FileLock(state_root_lock_path(self.state_root), label="state root")
            self._state_lock.acquire(metadata={"mcp_bind_url": mcp_bind_url})
        dashboard_port = self._start_dashboard_server()
        self._register_project(self.default_project, cwd_hint=str(self.cwd))
        self.ingress = GlobalIngress(
            state_root=self.state_root,
            runtime=self._resolve_runtime(),
            dashboard_port=dashboard_port,
        )
        return self.ingress.start(projects=self._known_projects())

    def supervisor(self, project: str | None = None) -> WorkerBeeSupervisor:
        name = project_slug(project or self.default_project)
        ingress = self._project_ingress(name)
        self._register_project(name, cwd_hint=str(self._project_cwd(name)))
        self._sync_ingress_projects()
        return self._build_supervisor(name, ingress=ingress)

    def _build_supervisor(
        self,
        project: str,
        *,
        ingress: ProjectIngressConfig | None,
    ) -> WorkerBeeSupervisor:
        return WorkerBeeSupervisor(
            project=project,
            state_dir=daemon_project_state_dir(project, state_root=self.state_root),
            runtime=self.runtime_requested,
            cwd=self._project_cwd(project),
            ingress=ingress,
        )

    def with_project(
        self,
        project: str | None,
        fn: Callable[[WorkerBeeSupervisor], T],
        *,
        require_not_stopped: bool = False,
        require_active: bool = False,
        autostart: bool = False,
        start_reason: str = "operation",
    ) -> T:
        name = project_slug(project or self.default_project)
        with self._project_lock(name):
            file_lock = FileLock(project_lock_path(self.state_root, name), label=f"project {name}")
            with file_lock:
                sup = self.supervisor(name)
                events: list[dict[str, Any]] = []
                if require_not_stopped or require_active:
                    self._raise_if_project_stopped(name)
                if require_active:
                    running = bool(sup.status().get("running"))
                    if autostart:
                        info = sup.start()
                        if not running:
                            events.append(
                                _stack_started_event(
                                    project=name,
                                    info=info,
                                    reason=start_reason,
                                )
                            )
                    elif not running:
                        raise WorkerBeeError(
                            code="STACK_NOT_RUNNING",
                            message=f"WorkerBee project `{name}` is not running",
                            details={"project": name, "mode": self.project_mode(name)},
                            remediation=(
                                f"Start it with `workerbee project mode start --project {name}` "
                                "or call workerbee_v1_project_start."
                            ),
                            retryable=True,
                        )
                result = fn(sup)
                if events and isinstance(result, dict):
                    result = _with_events(result, events)
                self._register_project(name, cwd_hint=str(sup.cwd))
                return result

    def session_start(
        self,
        *,
        cwd: str | Path,
        goal: str | None = None,
        project: str | None = None,
        open_dashboard: bool = False,
    ) -> dict[str, Any]:
        cwd_path = Path(cwd).expanduser().resolve()
        project_info = derive_session_project_info(cwd_path, project=project)
        name = project_info.project
        with self._project_lock(name):
            file_lock = FileLock(project_lock_path(self.state_root, name), label=f"project {name}")
            with file_lock:
                self._register_project(
                    name,
                    cwd_hint=str(cwd_path),
                    git_root=str(project_info.git_root) if project_info.git_root else None,
                    git_branch=project_info.git_branch,
                    explicit_project=project_info.explicit_project,
                )
                sup = self.supervisor(name)
                mode = self.project_mode(name)
                events: list[dict[str, Any]] = []
                if mode == "start":
                    running = bool(sup.status().get("running"))
                    info = sup.start()
                    if not running:
                        events.append(
                            _stack_started_event(project=name, info=info, reason="session_start")
                        )
                    status = sup.status()
                else:
                    status = sup.status()
                dashboard_url = _dashboard_url(status)
                opened = open_browser(dashboard_url) if open_dashboard else False
                self._register_project(
                    name,
                    cwd_hint=str(sup.cwd),
                    git_root=str(project_info.git_root) if project_info.git_root else None,
                    git_branch=project_info.git_branch,
                    explicit_project=project_info.explicit_project,
                )
        running = bool(status.get("running"))
        return {
            "project": name,
            "cwd": str(cwd_path),
            "project_identity": project_info.public_dict(),
            "git_root": str(project_info.git_root) if project_info.git_root else None,
            "git_branch": project_info.git_branch,
            "explicit_project": project_info.explicit_project,
            "goal": goal,
            "mode": mode,
            "state_root": str(self.state_root),
            "state_dir": str(sup.state_dir),
            "global_dashboard": self.global_dashboard(),
            "project_status": status,
            "dashboard_url": dashboard_url,
            "browser_opened": opened,
            "runbook": runbook_payload(),
            "next_actions": next_actions_for_mode(mode, running=running),
            "events": events,
            "user_message": user_message_for_session(
                project=name,
                mode=mode,
                running=running,
                dashboard_url=dashboard_url,
            ),
        }

    def project_mode(self, project: str) -> str:
        name = project_slug(project)
        record = self._read_registry().get(name) or {}
        return _safe_project_mode(record.get("mode"))

    def project_mode_get(self, project: str | None = None) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        self._register_project(name, cwd_hint=str(self._project_cwd(name)))
        status = self.project_status(name)
        return {
            "project": name,
            "mode": self.project_mode(name),
            "state_root": str(self.state_root),
            "state_dir": str(daemon_project_state_dir(name, state_root=self.state_root)),
            "project_status": status,
            "dashboard_url": _dashboard_url(status),
        }

    def project_start(
        self,
        *,
        project: str | None = None,
        open_dashboard: bool = False,
    ) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        result = self.with_project(
            name,
            lambda supervisor: supervisor.start().public_dict(),
            require_active=True,
            autostart=True,
            start_reason="project_start",
        )
        if open_dashboard and isinstance(result, dict):
            result["browser_opened"] = open_browser(str(result.get("dashboard_url") or ""))
        return result

    def project_mode_set(
        self,
        *,
        mode: str,
        project: str | None = None,
        cwd: str | Path | None = None,
        open_dashboard: bool = False,
    ) -> dict[str, Any]:
        selected_mode = normalize_project_mode(mode)
        cwd_path = Path(cwd).expanduser().resolve() if cwd is not None else None
        name = project_slug(project or self.default_project)
        project_info = None
        if cwd_path is not None:
            implicit_info = derive_session_project_info(cwd_path)
            project_info = (
                implicit_info
                if name == implicit_info.project
                else derive_session_project_info(cwd_path, project=project)
            )
        with self._project_lock(name):
            file_lock = FileLock(project_lock_path(self.state_root, name), label=f"project {name}")
            with file_lock:
                self._register_project(
                    name,
                    cwd_hint=str(cwd_path or self._project_cwd(name)),
                    mode=selected_mode,
                    git_root=(
                        str(project_info.git_root)
                        if project_info is not None and project_info.git_root
                        else None
                    ),
                    git_branch=project_info.git_branch if project_info is not None else None,
                    explicit_project=(
                        project_info.explicit_project if project_info is not None else None
                    ),
                )
                sup = self.supervisor(name)
                events: list[dict[str, Any]] = []
                result: dict[str, Any] | None = None
                if selected_mode == "stop":
                    result = sup.stop(purge=False)
                    events.append(
                        {
                            "type": "project_stopped",
                            "project": name,
                            "dashboard_url": None,
                        }
                    )
                elif selected_mode == "start":
                    running = bool(sup.status().get("running"))
                    info = sup.start()
                    if not running:
                        events.append(
                            _stack_started_event(project=name, info=info, reason="mode_set")
                        )
                status = sup.status()
                dashboard_url = _dashboard_url(status)
                opened = open_browser(dashboard_url) if open_dashboard else False
                self._register_project(name, cwd_hint=str(sup.cwd), mode=selected_mode)
        running = bool(status.get("running"))
        return {
            "project": name,
            "mode": selected_mode,
            "state_root": str(self.state_root),
            "state_dir": str(sup.state_dir),
            "project_status": status,
            "dashboard_url": dashboard_url,
            "browser_opened": opened,
            "events": events,
            "result": result or {},
            "user_message": user_message_for_session(
                project=name,
                mode=selected_mode,
                running=running,
                dashboard_url=dashboard_url,
            ),
        }

    def projects(self) -> dict[str, Any]:
        records = self._read_registry()
        project_names = set(records)
        if self.projects_dir.is_dir():
            project_names.update(path.name for path in self.projects_dir.iterdir() if path.is_dir())
        items: list[dict[str, Any]] = []
        profile_ingress_sync_needed = False
        global_dashboard = self.global_dashboard()
        https_port = int(global_dashboard.get("https_port") or 19443)
        for name in sorted(project_names):
            record = records.get(name) or {}
            state_dir = record.get("state_dir") or str(
                daemon_project_state_dir(name, state_root=self.state_root)
            )
            project_ingress = self._project_ingress(name)
            try:
                sup = self._build_supervisor(name, ingress=project_ingress)
                state_dir = str(sup.state_dir)
                status = sup.status()
            except Exception as exc:  # noqa: BLE001 - dashboard must stay renderable
                status = {
                    "running": False,
                    "apishim_running": False,
                    "error": str(exc),
                }
            profile_status = self._project_profile_status(name, ingress=project_ingress)
            ingress_refresh = (
                profile_status.get("ingress_refresh")
                if isinstance(profile_status.get("ingress_refresh"), dict)
                else {}
            )
            if ingress_refresh.get("sync_needed"):
                profile_ingress_sync_needed = True
            stack = status.get("stack") if isinstance(status, dict) else None
            profile = (
                profile_status.get("profile")
                if isinstance(profile_status.get("profile"), dict)
                else None
            )
            profile_urls = (
                profile.get("ingress_urls")
                if isinstance(profile, dict) and isinstance(profile.get("ingress_urls"), dict)
                else {}
            )
            profile_dashboard_url = None
            if isinstance(profile, dict) and profile_urls.get("dashboard"):
                profile_dashboard_url = str(profile_urls["dashboard"])
            stack_dashboard_url = (stack or {}).get("dashboard_url") if stack else None
            stack_running = bool(status.get("running"))
            profile_running = bool(profile_status.get("running"))
            running = stack_running or profile_running
            dashboard_url = profile_dashboard_url or stack_dashboard_url
            exposed_routes = _project_exposed_routes(Path(state_dir), https_port=https_port)
            exposed_hosts = sorted(
                {
                    host
                    for route in exposed_routes
                    for host in route.get("hosts", [])
                    if isinstance(host, str)
                }
            )
            ingress_ready = bool(dashboard_url or exposed_routes)
            ingress_status = "ready" if ingress_ready else ("missing" if running else "idle")
            status_kind = _project_status_kind(
                stack_running=stack_running,
                profile_running=profile_running,
            )
            error = status.get("error") or profile_status.get("error")
            items.append(
                {
                    "project": name,
                    "state_dir": state_dir,
                    "cwd_hint": record.get("cwd_hint"),
                    "git_root": record.get("git_root"),
                    "git_branch": record.get("git_branch"),
                    "explicit_project": bool(record.get("explicit_project")),
                    "created_at": record.get("created_at"),
                    "last_seen_at": record.get("last_seen_at"),
                    "mode": _safe_project_mode(record.get("mode")),
                    "running": running,
                    "stack_running": stack_running,
                    "profile_running": profile_running,
                    "status_kind": status_kind,
                    "apishim_running": bool(status.get("apishim_running")),
                    "dashboard_url": dashboard_url,
                    "stack_dashboard_url": stack_dashboard_url,
                    "profile_dashboard_url": profile_dashboard_url,
                    "ingress_ready": ingress_ready,
                    "ingress_status": ingress_status,
                    "exposed_routes": exposed_routes,
                    "exposed_route_count": len(exposed_routes),
                    "exposed_hosts": exposed_hosts,
                    "exposed_route_summary": _exposed_route_summary(exposed_routes),
                    "profile_name": profile.get("profile") if isinstance(profile, dict) else None,
                    "profile_urls": profile_urls,
                    "profile": profile,
                    "profile_status": profile_status,
                    "ingress": (stack or {}).get("ingress") if stack else None,
                    "error": error,
                }
            )
        ingress_sync = {"needed": False, "synced": False}
        if profile_ingress_sync_needed:
            ingress_sync = {**self._sync_ingress_projects_result(), "needed": True}
        return {
            "state_root": str(self.state_root),
            "global_dashboard": global_dashboard,
            "projects": items,
            "summary": _dashboard_summary(items, global_dashboard=global_dashboard),
            "action_jobs": self.dashboard_action_jobs(),
            "ingress_sync": ingress_sync,
            "updated_at": time.time(),
        }

    def project_status(self, project: str) -> dict[str, Any]:
        name = project_slug(project)
        status = self.with_project(name, lambda sup: sup.status())
        status["mode"] = self.project_mode(name)
        status["latest_deployment"] = self._latest_deployment(name)
        return status

    def profile_list(self) -> dict[str, Any]:
        return builtin_profiles()

    def profile_start(
        self,
        *,
        profile: str,
        project: str | None = None,
        k1s_root: str | Path | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        with self._project_lock(name):
            file_lock = FileLock(project_lock_path(self.state_root, name), label=f"project {name}")
            with file_lock:
                self._register_project(name, cwd_hint=str(self._project_cwd(name)))
                result = self._profile_runner(name, k1s_root=k1s_root).start(
                    profile=profile,
                    timeout=timeout,
                )
                ingress_sync = self._sync_ingress_projects_result()
                self._register_project(name, cwd_hint=str(self._project_cwd(name)))
                return {
                    **result,
                    "project": name,
                    "ingress_sync": ingress_sync,
                }

    def profile_status(
        self,
        *,
        project: str | None = None,
        k1s_root: str | Path | None = None,
    ) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        self._register_project(name, cwd_hint=str(self._project_cwd(name)))
        result = self._profile_runner(name, k1s_root=k1s_root).status()
        return {**result, "project": name, "ingress_sync": self._sync_ingress_projects_result()}

    def profile_stop(
        self,
        *,
        project: str | None = None,
        purge: bool = False,
        k1s_root: str | Path | None = None,
    ) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        with self._project_lock(name):
            file_lock = FileLock(project_lock_path(self.state_root, name), label=f"project {name}")
            with file_lock:
                result = self._profile_runner(name, k1s_root=k1s_root).stop(purge=purge)
                ingress_sync = self._sync_ingress_projects_result()
                return {
                    **result,
                    "project": name,
                    "ingress_sync": ingress_sync,
                }

    def profile_validate(
        self,
        *,
        profile: str,
        project: str | None = None,
        k1s_root: str | Path | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        with self._project_lock(name):
            file_lock = FileLock(project_lock_path(self.state_root, name), label=f"project {name}")
            with file_lock:
                self._register_project(name, cwd_hint=str(self._project_cwd(name)))
                result = self._profile_runner(name, k1s_root=k1s_root).validate(
                    profile=profile,
                    timeout=timeout,
                )
                ingress_sync = self._sync_ingress_projects_result()
                return {
                    **result,
                    "project": name,
                    "ingress_sync": ingress_sync,
                }

    def profile_workload_validate(
        self,
        *,
        profile: str,
        project: str | None = None,
        k1s_root: str | Path | None = None,
        timeout: float = 240.0,
    ) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        with self._project_lock(name):
            file_lock = FileLock(project_lock_path(self.state_root, name), label=f"project {name}")
            with file_lock:
                self._raise_if_project_stopped(name)
                self._register_project(name, cwd_hint=str(self._project_cwd(name)))
                if self._active_ingress() is None:
                    raise WorkerBeeError(
                        code="PROFILE_INGRESS_REQUIRED",
                        message=(
                            "profile workload validation requires running WorkerBee MCP ingress"
                        ),
                        remediation=(
                            "Start WorkerBee MCP before running profile workload validation."
                        ),
                    )
                supervisor = self._build_supervisor(name, ingress=self._project_ingress(name))
                runner = self._profile_runner(name, k1s_root=k1s_root)
                profile_start = runner.start(profile=profile, timeout=timeout)
                contexts = _copy_realtime_contexts(self.state_root, name)
                builds = [
                    supervisor.build_image(
                        path,
                        tag=f"workerbee-{name}-realtime-{app}:dev",
                    )
                    for app, path in contexts.items()
                ]
                prepared = prepare_stage(
                    supervisor=supervisor,
                    name="realtime-web-db",
                    template="realtime-web-db",
                )
                deploy = deploy_profile_stage(
                    supervisor=supervisor,
                    profile_runner=runner,
                    stage_dir=Path(str(prepared["stage_dir"])),
                    profile=profile,
                    namespace=name,
                    timeout=int(timeout),
                    sync_ingress=self._sync_ingress_projects_result,
                    reset_existing=True,
                )
                status = runner.workload_status(profile=profile, namespace=name)
                connection = runner.connection(profile=profile)
                url_checks = _profile_control_plane_checks(connection)
                probes = [
                    _probe_with_retry(
                        self,
                        project=name,
                        host=f"api.{name}.workerbee.localhost",
                        path="/healthz",
                        expected_status=200,
                        timeout=timeout,
                    ),
                    _probe_with_retry(
                        self,
                        project=name,
                        host=f"api.{name}.workerbee.localhost",
                        path="/api/seed",
                        expected_status=200,
                        body_contains="workerbee",
                        timeout=timeout,
                    ),
                    _probe_with_retry(
                        self,
                        project=name,
                        host=f"app.{name}.workerbee.localhost",
                        path="/healthz",
                        expected_status=200,
                        timeout=timeout,
                    ),
                ]
                websocket = _websocket_probe(
                    f"wss://api.{name}.workerbee.localhost:"
                    f"{self.global_dashboard().get('https_port', 19443)}/ws",
                    ca_bundle=str(connection["ca_bundle"]),
                    expected="echo:workerbee",
                )
                exports: dict[str, Any] = {}
                for fmt in ("k1s", "k8s", "helm"):
                    try:
                        exports[fmt] = export_bundle(
                            supervisor=supervisor,
                            stage_dir=Path(str(prepared["stage_dir"])),
                            fmt=fmt,
                            namespace=name,
                        )
                    except Exception as exc:  # noqa: BLE001
                        exports[fmt] = {"ok": False, "error": str(exc)}
                checks = [
                    {"name": "profile-start", "ok": bool(profile_start.get("ok"))},
                    {"name": "images-built", "ok": all(bool(item.get("ok")) for item in builds)},
                    {"name": "manifest-deploy", "ok": bool(deploy.get("ok"))},
                    {"name": "workload-status", "ok": bool(status.get("ok"))},
                    {
                        "name": "control-plane-urls",
                        "ok": all(item.get("ok") for item in url_checks),
                    },
                    {"name": "https-probes", "ok": all(item.get("ok") for item in probes)},
                    {"name": "websocket-probe", "ok": bool(websocket.get("ok"))},
                    {"name": "exports", "ok": all(item.get("ok") for item in exports.values())},
                ]
                return {
                    "ok": all(bool(item.get("ok")) for item in checks),
                    "project": name,
                    "profile": profile,
                    "checks": checks,
                    "profile_start": profile_start,
                    "builds": builds,
                    "prepared": prepared,
                    "deploy": deploy,
                    "status": status,
                    "url_checks": url_checks,
                    "probes": probes,
                    "websocket": websocket,
                    "exports": exports,
                }

    def manifest_deploy_local(
        self,
        *,
        stage: Path,
        target: str = "workerbee",
        profile: str | None = None,
        project: str | None = None,
        namespace: str | None = None,
        timeout: int = 180,
        k1s_root: str | Path | None = None,
    ) -> dict[str, Any]:
        from workerbee.manifests import deploy_local_stage, resolve_stage_dir

        name = project_slug(project or self.default_project)
        target = _normalize_deploy_target(target)
        if target == "workerbee":
            def deploy_and_record(supervisor: WorkerBeeSupervisor) -> dict[str, Any]:
                stage_dir = resolve_stage_dir(supervisor, stage)
                result = deploy_local_stage(
                    supervisor=supervisor,
                    stage_dir=stage_dir,
                    namespace=namespace,
                    timeout=timeout,
                )
                deployment = self._record_deployment(
                    project=name,
                    stage=stage,
                    stage_dir=stage_dir,
                    target=target,
                    profile=profile,
                    namespace=namespace,
                    result=result,
                )
                return {**result, "deployment": deployment}

            return self.with_project(
                name,
                deploy_and_record,
                require_active=True,
                autostart=True,
                start_reason="manifest_deploy_local",
            )
        self._raise_if_project_stopped(name)
        with self._project_lock(name):
            file_lock = FileLock(project_lock_path(self.state_root, name), label=f"project {name}")
            with file_lock:
                self._register_project(name, cwd_hint=str(self._project_cwd(name)))
                self._active_ingress()
                supervisor = self._build_supervisor(name, ingress=self._project_ingress(name))
                stage_dir = resolve_stage_dir(supervisor, stage)
                result = deploy_profile_stage(
                    supervisor=supervisor,
                    profile_runner=self._profile_runner(name, k1s_root=k1s_root),
                    stage_dir=stage_dir,
                    profile=profile,
                    namespace=namespace,
                    timeout=timeout,
                    sync_ingress=self._sync_ingress_projects_result,
                )
                deployment = self._record_deployment(
                    project=name,
                    stage=stage,
                    stage_dir=stage_dir,
                    target=target,
                    profile=str(result.get("profile") or profile or ""),
                    namespace=namespace,
                    result=result,
                )
                return {**result, "project": name, "deployment": deployment}

    def security_assess(
        self,
        *,
        stage: Path,
        target: str = "workerbee",
        project: str | None = None,
        namespace: str | None = None,
        checks: list[str] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        from workerbee.manifests import resolve_stage_dir

        name = project_slug(project or self.default_project)
        target = _normalize_deploy_target(target)

        def runtime_probe(**kwargs: Any) -> dict[str, Any]:
            return self.ingress_probe(
                project=name,
                url=str(kwargs["url"]),
                method=str(kwargs.get("method") or "GET"),
                timeout=float(kwargs.get("timeout") or timeout),
            )

        probe = runtime_probe if self._active_ingress() is not None else None
        return self.with_project(
            name,
            lambda supervisor: assess_stage_security(
                supervisor=supervisor,
                stage_dir=resolve_stage_dir(supervisor, stage),
                namespace=namespace,
                target=target,
                checks=checks,
                runtime_probe=probe,
                timeout=timeout,
            ),
        )

    def security_review_project(
        self,
        *,
        project: str | None = None,
        stage: Path | None = None,
        target: str = "workerbee",
        namespace: str | None = None,
        checks: list[str] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        from workerbee.manifests import resolve_stage_dir

        name = project_slug(project or self.default_project)
        deployment = self._latest_deployment(name)
        stage_ref = stage
        resolved_target = _normalize_deploy_target(target)
        if stage_ref is None:
            if not deployment:
                raise self._security_review_deployment_required(name)
            raw_stage = deployment.get("stage_dir") or deployment.get("stage")
            if not raw_stage:
                raise WorkerBeeError(
                    code="SECURITY_REVIEW_STAGE_REQUIRED",
                    message="latest deployment metadata does not include a stage path",
                    details={"project": name, "deployment": deployment},
                    remediation=(
                        "Pass an explicit stage path or redeploy the project so WorkerBee can "
                        "record fresh deployment metadata."
                    ),
                    retryable=True,
                )
            stage_ref = Path(str(raw_stage))
            resolved_target = _normalize_deploy_target(str(deployment.get("target") or target))
            if namespace is None and deployment.get("namespace"):
                namespace = str(deployment["namespace"])

        def runtime_probe(**kwargs: Any) -> dict[str, Any]:
            return self.ingress_probe(
                project=name,
                url=str(kwargs["url"]),
                method=str(kwargs.get("method") or "GET"),
                timeout=float(kwargs.get("timeout") or timeout),
            )

        probe = runtime_probe if self._active_ingress() is not None else None
        assessment = self.with_project(
            name,
            lambda supervisor: assess_stage_security(
                supervisor=supervisor,
                stage_dir=resolve_stage_dir(supervisor, stage_ref),
                namespace=namespace,
                target=resolved_target,
                checks=checks,
                runtime_probe=probe,
                timeout=timeout,
            ),
        )
        review = {
            "ok": True,
            "mode": "advisory",
            "project": name,
            "target": resolved_target,
            "stage": str(stage_ref),
            "stage_dir": assessment.get("stage_dir"),
            "deployment": deployment,
            "assessment": assessment,
        }
        report = self._write_security_review_report(project=name, review=review)
        return {**review, "report": report}

    def _record_deployment(
        self,
        *,
        project: str,
        stage: Path | str,
        stage_dir: Path,
        target: str,
        profile: str | None,
        namespace: str | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = _utc_timestamp_iso()
        deployment_id = f"deploy-{_utc_timestamp_slug()}-{secrets.token_hex(4)}"
        deployment = {
            "api_version": "workerbee.deployment/v1",
            "id": deployment_id,
            "project": project,
            "target": target,
            "profile": profile or None,
            "namespace": namespace,
            "stage": str(stage),
            "stage_dir": str(stage_dir.expanduser().resolve()),
            "created_at": timestamp,
            "updated_at": timestamp,
            "ok": bool(result.get("ok", True)),
            "validation": _deployment_validation_summary(result.get("validation")),
            "apply": _deployment_apply_summary(result.get("apply")),
            "ingress_urls": _deployment_ingress_urls(result),
            "exports": _deployment_exports(stage_dir),
        }
        alias_refresh = result.get("alias_refresh")
        if isinstance(alias_refresh, dict):
            deployment["alias_refresh"] = {
                "ok": alias_refresh.get("ok"),
                "enabled": alias_refresh.get("enabled"),
                "reason": alias_refresh.get("reason"),
                "runtime": alias_refresh.get("runtime"),
            }
        paths = _deployment_paths(self.state_root, project)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        _write_json(paths["latest"], deployment)
        _write_json(paths["dir"] / f"{deployment_id}.json", deployment)
        return deployment

    def _latest_deployment(self, project: str) -> dict[str, Any] | None:
        path = _deployment_paths(self.state_root, project)["latest"]
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _security_review_deployment_required(self, project: str) -> WorkerBeeError:
        state_dir = daemon_project_state_dir(project, state_root=self.state_root)
        global_dashboard = self.global_dashboard()
        https_port = int(global_dashboard.get("https_port") or 19443)
        routes = _project_exposed_routes(state_dir, https_port=https_port)
        return WorkerBeeError(
            code="SECURITY_REVIEW_DEPLOYMENT_REQUIRED",
            message=f"WorkerBee project `{project}` has no recorded deployment to review",
            details={
                "project": project,
                "state_dir": str(state_dir),
                "available_stages": _available_stage_names(state_dir),
                "exposed_routes": routes,
                "latest_deployment": str(_deployment_paths(self.state_root, project)["latest"]),
            },
            remediation=(
                "Stage and deploy the app first with WorkerBee, or pass an explicit stage path "
                "to the security review."
            ),
            retryable=True,
        )

    def _write_security_review_report(
        self,
        *,
        project: str,
        review: dict[str, Any],
    ) -> dict[str, Any]:
        generated_at = _utc_timestamp_iso()
        report_id = f"security-{_utc_timestamp_slug()}-{secrets.token_hex(4)}"
        report = {
            "api_version": "workerbee.security_report/v1",
            "kind": "SecurityReviewProject",
            "id": report_id,
            "generated_at": generated_at,
            "project": project,
            "review": review,
        }
        reports_dir = (
            daemon_project_state_dir(project, state_root=self.state_root)
            / "reports"
            / "security"
        )
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / f"{report_id}.json"
        _write_json(path, report)
        return {
            "api_version": "workerbee.security_report/v1",
            "id": report_id,
            "path": str(path),
            "generated_at": generated_at,
        }

    def profile_workload_status(
        self,
        *,
        project: str | None = None,
        profile: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        self._raise_if_project_stopped(name)
        self._register_project(name, cwd_hint=str(self._project_cwd(name)))
        self._active_ingress()
        runner = self._profile_runner(name)
        runner.connection(profile=profile)
        self._sync_ingress_projects_result()
        return runner.workload_status(profile=profile, namespace=namespace)

    def profile_logs(
        self,
        *,
        app: str,
        project: str | None = None,
        profile: str | None = None,
        namespace: str | None = None,
        tail: int = 80,
    ) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        self._raise_if_project_stopped(name)
        self._register_project(name, cwd_hint=str(self._project_cwd(name)))
        self._active_ingress()
        runner = self._profile_runner(name)
        runner.connection(profile=profile)
        self._sync_ingress_projects_result()
        return runner.workload_logs(
            app=app,
            profile=profile,
            namespace=namespace,
            tail=tail,
        )

    def capabilities(self) -> dict[str, Any]:
        from workerbee import __version__
        from workerbee.contract import AGENT_FEEDBACK_SCHEMA, API_VERSION, MCP_TOOL_NAMES
        from workerbee.k1s_runtime import resolve_k1s_runtime

        try:
            k1s_runtime = resolve_k1s_runtime(cwd=self.cwd)
            k1s = {
                "source": k1s_runtime.source,
                "python": k1s_runtime.python_executable,
                "root": str(k1s_runtime.k1s_root) if k1s_runtime.k1s_root else None,
                "ae_origin": k1s_runtime.ae_origin,
                "ae_version": k1s_runtime.ae_version,
            }
        except Exception as exc:  # noqa: BLE001
            k1s = {"error": str(exc)}

        return {
            "api_version": API_VERSION,
            "workerbee_version": __version__,
            "state_root": str(self.state_root),
            "default_project": self.default_project,
            "mcp_tools": MCP_TOOL_NAMES,
            "agent_workflow": {
                "session_bootstrap": True,
                "project_modes": sorted({"start", "lazy", "stop"}),
                "default_project_mode": DEFAULT_PROJECT_MODE,
                "project_identity": (
                    "explicit project override, otherwise git repo basename + branch + cwd hash"
                ),
                "ingress_probe": "WorkerBee-managed localhost HTTPS hosts only",
            },
            "agent_feedback": {
                "schema": AGENT_FEEDBACK_SCHEMA,
                "embedded_in_existing_results": True,
            },
            "tool_hints": {
                "workerbee_v1_ingress_probe": {
                    "methods": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                    "body_fields": ["json_body", "body"],
                    "headers": True,
                    "notes": (
                        "Use headers for signed requests such as S3 presigned PUTs; "
                        "Host and Content-Length are intentionally blocked."
                    ),
                },
                "workerbee_v1_image_build": {
                    "dockerfile": (
                        "Set dockerfile for repo-root builds with nested Dockerfiles, "
                        "for example dockerfile='backend/Dockerfile'."
                    )
                },
                "workerbee_v1_manifest_deploy_remote_k1s": {
                    "allow_remote_secretrefs": (
                        "Defaults to false; WorkerBee refuses remote secretRefs unless "
                        "the caller explicitly accepts remote controller path handling."
                    )
                },
            },
            "secret_policy": secret_policy_status(
                daemon_project_state_dir(self.default_project, state_root=self.state_root)
            ),
            "templates": [
                "frontend-api",
                "frontend-api-store",
                "realtime-web-db",
                "stateless-web",
            ],
            "manifest_inputs": {
                "native-k1s": {
                    "deploy_local": True,
                    "deploy_remote_k1s": True,
                    "export_formats": ["k1s", "k8s", "helm"],
                },
                "kubernetes": {
                    "deploy_local": True,
                    "deploy_remote_k1s": True,
                    "export_formats": ["k8s", "helm"],
                    "v0_1_constraint": (
                        "one Deployment/StatefulSet/DaemonSet/Job plus optional "
                        "Service/Ingress per file"
                    ),
                },
            },
            "bundle_formats": ["k1s", "k8s", "helm"],
            "security_assessment": {
                "advisory": True,
                "stage_assess_tool": "workerbee_v1_security_assess",
                "project_review_tool": "workerbee_v1_security_review_project",
                "checks": list(DEFAULT_SECURITY_CHECKS),
                "standards": [
                    "OWASP Web Top 10 2021",
                    "OWASP API Security Top 10 2023",
                    "OWASP Kubernetes Top 10",
                    "OWASP CI/CD Security Risks",
                ],
                "latest_deployment_metadata": True,
                "report_output": "WorkerBee-managed project reports/security directory",
                "first_run_behavior": (
                    "If no deployment metadata exists, stage/deploy the app first or pass "
                    "an explicit stage path."
                ),
                "blocks_deploy": False,
            },
            "k1s_runtime": k1s,
            "k1s_profiles": {
                "runtime_requirement": "containerd",
                "host_k1s_processes": False,
                "profiles": [item["name"] for item in builtin_profiles()["profiles"]],
                "workload_targets": ["profile"],
            },
            "runtime": runtime_diagnostics(self.runtime_requested, state_root=self.state_root),
            "containerd_privilege": containerd_privilege_summary(
                containerd_privilege_status(
                    state_root=self.state_root,
                    runtime=self.runtime_requested,
                )
            ),
        }

    def secret_policy_status(self, project: str | None = None) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        project_state = daemon_project_state_dir(name, state_root=self.state_root)
        return {
            "ok": True,
            "project": name,
            "state_root": str(self.state_root),
            "project_state": str(project_state),
            "secret_policy": secret_policy_status(project_state),
        }

    def cleanup(self, *, execute: bool = False, purge_images: bool = False) -> dict[str, Any]:
        return cleanup_runtime(
            state_root=self.state_root,
            runtime=self.runtime_requested,
            execute=execute,
            purge_images=purge_images,
        )

    def enqueue_dashboard_action(self, action: str, projects: list[str]) -> dict[str, Any]:
        job = DashboardActionJob(
            job_id=secrets.token_urlsafe(12),
            action=action,
            projects=list(projects),
            status="queued",
            created_at=time.time(),
        )
        with self._dashboard_jobs_lock:
            self._dashboard_jobs[job.job_id] = job
            self._dashboard_jobs_order.append(job.job_id)
            self._prune_dashboard_jobs_locked()
        scheduler = self._dashboard_scheduler or _default_dashboard_job_scheduler
        scheduler(lambda: self._run_dashboard_action_job(job.job_id))
        return job.public_dict()

    def dashboard_action_job(self, job_id: str) -> dict[str, Any] | None:
        with self._dashboard_jobs_lock:
            job = self._dashboard_jobs.get(job_id)
            return job.public_dict() if job is not None else None

    def dashboard_action_jobs(self) -> list[dict[str, Any]]:
        with self._dashboard_jobs_lock:
            jobs = [
                self._dashboard_jobs[job_id].public_dict()
                for job_id in self._dashboard_jobs_order
                if job_id in self._dashboard_jobs
            ]
        return list(reversed(jobs))

    def _run_dashboard_action_job(self, job_id: str) -> None:
        with self._dashboard_jobs_lock:
            job = self._dashboard_jobs.get(job_id)
            if job is None:
                return
            job.status = "running"
            job.started_at = time.time()
        try:
            result = self._execute_dashboard_action(job.action, job.projects)
            ok = result.get("ok") is not False
            error = _dashboard_action_result_error(result)
        except Exception as exc:  # noqa: BLE001 - dashboard jobs must report failures
            result = {"ok": False, "error": str(exc)}
            ok = False
            error = str(exc)
        with self._dashboard_jobs_lock:
            job = self._dashboard_jobs.get(job_id)
            if job is None:
                return
            job.status = "succeeded" if ok else "failed"
            job.ok = ok
            job.result = result
            job.error = error
            job.finished_at = time.time()
            self._prune_dashboard_jobs_locked()

    def _execute_dashboard_action(self, action: str, projects: list[str]) -> dict[str, Any]:
        if action == "start_projects":
            return self.start_projects(projects, sync_ingress=False)
        if action == "stop_projects":
            return self.stop_projects(projects, purge=False)
        if action == "delete_projects":
            return self.delete_projects(projects, sync_ingress=False)
        if action == "start_all_projects":
            return self.start_all_projects(sync_ingress=False)
        if action == "stop_all_projects":
            return self.stop_all_projects(purge=False)
        if action == "delete_all_projects":
            return self.delete_all_projects(sync_ingress=False)
        if action == "mcp_shutdown":
            return self.schedule_mcp_shutdown()
        if action == "mcp_reboot":
            return self.schedule_mcp_reboot()
        return {"ok": False, "error": f"unknown dashboard action: {action}"}

    def _prune_dashboard_jobs_locked(self) -> None:
        cutoff = time.time() - 30 * 60
        keep: list[str] = []
        for job_id in self._dashboard_jobs_order:
            job = self._dashboard_jobs.get(job_id)
            if job is None:
                continue
            if job.status in {"queued", "running"} or job.created_at >= cutoff:
                keep.append(job_id)
            else:
                self._dashboard_jobs.pop(job_id, None)
        finished = [
            job_id
            for job_id in keep
            if self._dashboard_jobs[job_id].status not in {"queued", "running"}
        ]
        while len(keep) > 50 and finished:
            candidate = finished.pop(0)
            if candidate in keep:
                keep.remove(candidate)
            self._dashboard_jobs.pop(candidate, None)
        self._dashboard_jobs_order = keep

    def stop_all_projects(self, *, purge: bool = False) -> dict[str, Any]:
        projects = self._known_projects()
        if not projects:
            return _empty_project_action_result(state_root=self.state_root, purge=purge)
        return self.stop_projects(projects, purge=purge, unregister=False)

    def start_all_projects(self, *, sync_ingress: bool = True) -> dict[str, Any]:
        projects = self._known_projects()
        if not projects:
            return _empty_project_action_result(state_root=self.state_root, purge=False)
        return self.start_projects(projects, sync_ingress=sync_ingress)

    def start_projects(
        self,
        projects: list[str],
        *,
        sync_ingress: bool = True,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        known = set(self._known_projects())
        names = _dashboard_project_names({"projects": projects})
        started: list[str] = []
        if not names:
            return {
                "ok": False,
                "state_root": str(self.state_root),
                "purge": False,
                "unregistered": [],
                "projects": [],
                "errors": [{"error": "no WorkerBee projects selected"}],
            }
        for name in names:
            if name not in known:
                errors.append({"project": name, "error": "unknown WorkerBee project"})
                continue
            try:
                with self._project_lock(name):
                    file_lock = FileLock(
                        project_lock_path(self.state_root, name),
                        label=f"project {name}",
                    )
                    with file_lock:
                        previous_mode = self.project_mode(name)
                        selected_mode = "start" if previous_mode == "stop" else None
                        self._register_project(
                            name,
                            cwd_hint=str(self._project_cwd(name)),
                        )
                        sup = self._build_supervisor(name, ingress=self._project_ingress(name))
                        was_running = bool(sup.status().get("running"))
                        info = sup.start()
                        self._register_project(
                            name,
                            cwd_hint=str(sup.cwd),
                            mode=selected_mode,
                        )
                        started.append(name)
                        results.append(
                            {
                                "project": name,
                                "ok": True,
                                "started": not was_running,
                                "mode": self.project_mode(name),
                                "dashboard_url": info.dashboard_url,
                                "stack": info.public_dict(),
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                errors.append({"project": name, "error": str(exc)})
        ingress_sync: dict[str, Any] | None = None
        if started:
            ingress_sync = (
                self._sync_ingress_projects_result()
                if sync_ingress
                else self._schedule_ingress_sync()
            )
        failed = [result for result in results if result.get("ok") is False]
        return {
            "ok": not errors and not failed,
            "state_root": str(self.state_root),
            "purge": False,
            "unregistered": [],
            "projects": results,
            "errors": errors,
            "ingress_sync": ingress_sync,
        }

    def delete_all_projects(self, *, sync_ingress: bool = True) -> dict[str, Any]:
        projects = self._known_projects()
        if not projects:
            result = _empty_project_action_result(state_root=self.state_root, purge=True)
            result["default_restored"] = self._restore_default_project()
            return result
        return self.delete_projects(projects, sync_ingress=sync_ingress)

    def delete_projects(
        self,
        projects: list[str],
        *,
        sync_ingress: bool = True,
    ) -> dict[str, Any]:
        return self.stop_projects(
            projects,
            purge=True,
            unregister=True,
            sync_ingress=sync_ingress,
        )

    def stop_projects(
        self,
        projects: list[str],
        *,
        purge: bool = False,
        unregister: bool = False,
        sync_ingress: bool = True,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        known = set(self._known_projects())
        names = _dashboard_project_names({"projects": projects})
        removed: list[str] = []
        if not names:
            return {
                "ok": False,
                "state_root": str(self.state_root),
                "purge": purge,
                "unregistered": [],
                "projects": [],
                "errors": [{"error": "no WorkerBee projects selected"}],
            }
        for name in names:
            if name not in known:
                errors.append({"project": name, "error": "unknown WorkerBee project"})
                continue
            try:
                with self._project_lock(name):
                    file_lock = FileLock(
                        project_lock_path(self.state_root, name),
                        label=f"project {name}",
                    )
                    with file_lock:
                        sup = self._build_supervisor(name, ingress=self._project_ingress(name))
                        result = {"project": name, **sup.stop(purge=purge)}
                        results.append(result)
                        if unregister and result.get("ok") is not False:
                            removed.append(name)
            except Exception as exc:  # noqa: BLE001
                errors.append({"project": name, "error": str(exc)})
        ingress_sync: dict[str, Any] | None = None
        if unregister and removed:
            self._unregister_projects(removed)
            default_restored = (
                self._restore_default_project() if self.default_project in names else None
            )
            ingress_sync = (
                self._sync_ingress_projects_result()
                if sync_ingress
                else self._schedule_ingress_sync()
            )
        else:
            default_restored = self._restore_default_project() if (
                unregister and self.default_project in names
            ) else None
        failed = [result for result in results if result.get("ok") is False]
        payload = {
            "ok": not errors and not failed,
            "state_root": str(self.state_root),
            "purge": purge,
            "unregistered": removed,
            "projects": results,
            "errors": errors,
            "ingress_sync": ingress_sync,
        }
        if default_restored is not None:
            payload["default_restored"] = default_restored
        return payload

    def _restore_default_project(self) -> dict[str, Any]:
        self._register_project(
            self.default_project,
            cwd_hint=str(self.cwd),
            mode=DEFAULT_PROJECT_MODE,
        )
        return {
            "project": self.default_project,
            "mode": DEFAULT_PROJECT_MODE,
            "state_dir": str(
                daemon_project_state_dir(self.default_project, state_root=self.state_root)
            ),
        }

    def schedule_mcp_shutdown(self) -> dict[str, Any]:
        return self._schedule_dashboard_lifecycle("mcp_shutdown", self._dashboard_shutdown_callback)

    def schedule_mcp_reboot(self) -> dict[str, Any]:
        return self._schedule_dashboard_lifecycle("mcp_reboot", self._dashboard_reboot_callback)

    def _schedule_dashboard_lifecycle(
        self,
        action: str,
        callback: Callable[[], None] | None,
    ) -> dict[str, Any]:
        if callback is None:
            return {
                "ok": False,
                "action": action,
                "scheduled": False,
                "error": "MCP lifecycle callback is not configured for this daemon",
            }
        scheduler = self._dashboard_scheduler or _default_dashboard_scheduler
        scheduler(callback)
        return {
            "ok": True,
            "action": action,
            "scheduled": True,
            "message": f"{action} scheduled",
        }

    def stop_global_ingress(self) -> dict[str, Any]:
        runtime = self._resolve_runtime()
        ingress = GlobalIngress(state_root=self.state_root, runtime=runtime)
        return {"stopped": True, "runtime": runtime, **ingress.stop()}

    def global_dashboard(self) -> dict[str, Any]:
        if self.ingress is not None:
            return global_ingress_status(self.state_root, runtime=self.ingress.runtime)
        return global_ingress_status(self.state_root, runtime=self.runtime_requested)

    def ingress_probe(
        self,
        *,
        project: str | None = None,
        url: str | None = None,
        host: str | None = None,
        path: str = "/",
        method: str = "GET",
        expected_status: int | None = None,
        body_contains: str | None = None,
        json_body: dict[str, Any] | None = None,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        name = project_slug(project or self.default_project)
        self._raise_if_project_stopped(name)
        info = self.global_dashboard()
        probe_url = build_probe_url(ingress_info=info, url=url, host=host, path=path)
        result = probe_workerbee_url(
            project=name,
            ingress_info=info,
            url=probe_url,
            method=method,
            expected_status=expected_status,
            body_contains=body_contains,
            json_body=json_body,
            body=body,
            headers=headers,
            timeout=timeout,
        )
        result["project"] = name
        return result

    def _project_ingress(self, project: str) -> ProjectIngressConfig | None:
        ingress = self._active_ingress()
        if ingress is None:
            return None
        return ingress.project_config(project)

    def _profile_runner(
        self,
        project: str,
        *,
        k1s_root: str | Path | None = None,
    ) -> K1sProfileRunner:
        root = Path(k1s_root).expanduser().resolve() if k1s_root is not None else None
        return K1sProfileRunner(
            project=project,
            state_root=self.state_root,
            runtime=self.runtime_requested,
            cwd=self._project_cwd(project),
            k1s_root=root,
            ingress=self._project_ingress(project),
        )

    def _project_profile_status(
        self,
        project: str,
        *,
        ingress: ProjectIngressConfig | None = None,
    ) -> dict[str, Any]:
        runner = K1sProfileRunner(
            project=project,
            state_root=self.state_root,
            runtime=self.runtime_requested,
            cwd=self._project_cwd(project),
            k1s_root=None,
            ingress=ingress,
        )
        refresh_ingress = ingress is not None
        site_path = ingress.sites_dir / "k1s-profile.caddy" if ingress is not None else None
        before_site_text = _read_optional_text(site_path)
        before_dashboard_url = None
        load = getattr(runner, "load", None)
        if callable(load):
            try:
                info = load()
                urls = getattr(info, "ingress_urls", None)
                if isinstance(urls, dict) and urls.get("dashboard"):
                    before_dashboard_url = str(urls["dashboard"])
            except Exception:
                before_dashboard_url = None
        try:
            result = dict(runner.status(refresh_ingress=refresh_ingress))
            result["ingress_refresh"] = _profile_ingress_refresh_metadata(
                refresh_ingress=refresh_ingress,
                running=bool(result.get("running")),
                before_dashboard_url=before_dashboard_url,
                before_site_text=before_site_text,
                after_site_text=_read_optional_text(site_path),
                site_path=site_path,
                profile=result.get("profile"),
            )
            return result
        except WorkerBeeError as exc:
            return {
                "ok": False,
                "running": False,
                "project": project,
                "state_root": str(self.state_root),
                "state_dir": str(runner.project_state),
                "error": exc.message,
                "code": exc.code,
                "ingress_refresh": _profile_ingress_refresh_metadata(
                    refresh_ingress=refresh_ingress,
                    running=False,
                    before_dashboard_url=before_dashboard_url,
                    before_site_text=before_site_text,
                    after_site_text=_read_optional_text(site_path),
                    site_path=site_path,
                    profile=None,
                    error=exc.message,
                ),
            }
        except Exception as exc:  # noqa: BLE001 - dashboard status must be best-effort
            return {
                "ok": False,
                "running": False,
                "project": project,
                "state_root": str(self.state_root),
                "state_dir": str(runner.project_state),
                "error": str(exc),
                "ingress_refresh": _profile_ingress_refresh_metadata(
                    refresh_ingress=refresh_ingress,
                    running=False,
                    before_dashboard_url=before_dashboard_url,
                    before_site_text=before_site_text,
                    after_site_text=_read_optional_text(site_path),
                    site_path=site_path,
                    profile=None,
                    error=str(exc),
                ),
            }

    def _known_projects(self) -> list[str]:
        project_names = set(self._read_registry())
        if self.projects_dir.is_dir():
            project_names.update(path.name for path in self.projects_dir.iterdir() if path.is_dir())
        return sorted(project_names)

    def _sync_ingress_projects(self) -> None:
        ingress = self._active_ingress()
        if ingress is not None:
            ingress.sync_projects(self._known_projects())

    def _sync_ingress_projects_result(self) -> dict[str, Any]:
        self._sync_ingress_projects()
        return {"scheduled": False, "synced": self.ingress is not None}

    def _active_ingress(self) -> GlobalIngress | None:
        if self.ingress is not None:
            return self.ingress
        status = global_ingress_status(self.state_root, runtime=self.runtime_requested)
        if not status.get("running"):
            return None
        runtime = str(status.get("runtime") or self._resolve_runtime())
        self.ingress = GlobalIngress(state_root=self.state_root, runtime=runtime)
        return self.ingress

    def _schedule_ingress_sync(self) -> dict[str, Any]:
        if self.ingress is None:
            return {"scheduled": False, "synced": False, "reason": "global ingress is not running"}
        scheduler = self._dashboard_scheduler or _default_ingress_sync_scheduler
        scheduler(self._sync_ingress_projects)
        return {"scheduled": True, "synced": False}

    def _raise_if_project_stopped(self, project: str) -> None:
        mode = self.project_mode(project)
        if mode != "stop":
            return
        name = project_slug(project)
        raise WorkerBeeError(
            code="PROJECT_STOPPED",
            message=f"WorkerBee is disabled for project `{name}`",
            details={"project": name, "mode": mode},
            remediation=f"Run `workerbee project mode start --project {name}` to re-enable it.",
        )

    def _project_cwd(self, project: str) -> Path:
        record = self._read_registry().get(project_slug(project)) or {}
        raw = record.get("cwd_hint")
        if raw:
            try:
                return Path(str(raw)).expanduser().resolve()
            except OSError:
                return Path(str(raw)).expanduser()
        return self.cwd

    def _project_lock(self, project: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(project)
            if lock is None:
                lock = threading.RLock()
                self._locks[project] = lock
            return lock

    def _register_project(
        self,
        project: str,
        *,
        cwd_hint: str,
        mode: str | None = None,
        git_root: str | None = None,
        git_branch: str | None = None,
        explicit_project: bool | None = None,
    ) -> None:
        project = project_slug(project)
        now = time.time()
        records = self._read_registry()
        existing = records.get(project) or {}
        selected_mode = normalize_project_mode(mode) if mode is not None else _safe_project_mode(
            existing.get("mode")
        )
        record = ProjectRecord(
            project=project,
            state_dir=str(daemon_project_state_dir(project, state_root=self.state_root)),
            cwd_hint=cwd_hint,
            mode=selected_mode,
            created_at=float(existing.get("created_at") or now),
            last_seen_at=now,
            git_root=git_root if git_root is not None else existing.get("git_root"),
            git_branch=git_branch if git_branch is not None else existing.get("git_branch"),
            explicit_project=(
                bool(explicit_project)
                if explicit_project is not None
                else bool(existing.get("explicit_project"))
            ),
        )
        records[project] = asdict(record)
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"projects": records}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.registry_file)

    def _unregister_projects(self, projects: list[str]) -> None:
        remove = {project_slug(project) for project in projects}
        if not remove:
            return
        records = self._read_registry()
        for project in remove:
            records.pop(project, None)
        self._write_registry(records)

    def _read_registry(self) -> dict[str, dict[str, Any]]:
        if not self.registry_file.is_file():
            return {}
        try:
            data = json.loads(self.registry_file.read_text(encoding="utf-8"))
            projects = data.get("projects") if isinstance(data, dict) else None
            if isinstance(projects, dict):
                return {
                    str(name): value
                    for name, value in projects.items()
                    if isinstance(value, dict)
                }
        except Exception:
            return {}
        return {}

    def _write_registry(self, records: dict[str, dict[str, Any]]) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"projects": records}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.registry_file)

    def _resolve_runtime(self) -> str:
        return resolve_runtime(self.runtime_requested)

    def _start_dashboard_server(self) -> int:
        if self._dashboard is not None:
            return int(self._dashboard.server_address[1])
        port = choose_port(18090, start=18090, end=18190)
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/healthz":
                    _send_json(
                        self,
                        {
                            "ok": True,
                            "state_root": str(daemon.state_root),
                        },
                    )
                    return
                if path.startswith("/api/action-jobs/"):
                    job_id = path.rsplit("/", 1)[-1]
                    job = daemon.dashboard_action_job(job_id)
                    if job is None:
                        _send_json(
                            self,
                            {
                                "ok": False,
                                "error": "unknown dashboard action job",
                                "job_id": job_id,
                            },
                            status=404,
                        )
                    else:
                        _send_json(self, {"ok": True, "job": job})
                    return
                if path.startswith("/api/projects") or path.startswith("/api/status"):
                    _send_json(self, daemon.projects())
                    return
                if path.startswith("/static/"):
                    asset = _dashboard_static_asset(path)
                    if asset is None:
                        _send_not_found(self)
                        return
                    body, content_type = asset
                    _send_bytes(self, body, content_type)
                    return
                _send_html(
                    self,
                    _render_dashboard(
                        daemon.projects(),
                        action_token=daemon.dashboard_action_token,
                    ),
                )

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path != "/api/actions":
                    _send_json(
                        self,
                        {
                            "ok": False,
                            "error": "unknown dashboard action endpoint",
                        },
                        status=404,
                    )
                    return
                if not _dashboard_host_allowed(self):
                    _send_json(
                        self,
                        {
                            "ok": False,
                            "error": "dashboard actions require a local WorkerBee host",
                        },
                        status=403,
                    )
                    return
                status, payload = _handle_dashboard_action(
                    daemon,
                    _read_dashboard_action_payload(self),
                )
                _send_json(self, payload, status=status)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._dashboard = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
        self._dashboard_thread = threading.Thread(
            target=self._dashboard.serve_forever,
            name="workerbee-dashboard",
            daemon=True,
        )
        self._dashboard_thread.start()
        return port


def _send_json(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
    *,
    status: int = 200,
) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    _send_dynamic_security_headers(handler)
    handler.end_headers()
    _write_response_body(handler, body)


def _send_html(handler: BaseHTTPRequestHandler, html: str) -> None:
    body = html.encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _send_dynamic_security_headers(handler)
    handler.send_header("Content-Security-Policy", _dashboard_csp())
    handler.end_headers()
    _write_response_body(handler, body)


def _send_bytes(handler: BaseHTTPRequestHandler, body: bytes, content_type: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    _write_response_body(handler, body)


def _send_not_found(handler: BaseHTTPRequestHandler) -> None:
    body = b"not found\n"
    handler.send_response(404)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _send_dynamic_security_headers(handler)
    handler.end_headers()
    _write_response_body(handler, body)


def _send_dynamic_security_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")


def _dashboard_csp() -> str:
    return (
        "default-src 'self'; "
        "base-uri 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "form-action 'none'"
    )


def _write_response_body(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    try:
        handler.wfile.write(body)
    except BrokenPipeError:
        return


def _default_dashboard_scheduler(callback: Callable[[], None]) -> None:
    timer = threading.Timer(0.25, callback)
    timer.daemon = True
    timer.start()


def _default_dashboard_job_scheduler(callback: Callable[[], None]) -> None:
    thread = threading.Thread(target=callback, name="workerbee-dashboard-action", daemon=True)
    thread.start()


def _default_ingress_sync_scheduler(callback: Callable[[], None]) -> None:
    timer = threading.Timer(1.0, callback)
    timer.daemon = True
    timer.start()


def _dashboard_host_allowed(handler: BaseHTTPRequestHandler) -> bool:
    host = _host_name(str(handler.headers.get("Host") or ""))
    return host in {
        "dashboard.workerbee.localhost",
        "localhost",
        "127.0.0.1",
        "::1",
    }


def _host_name(raw: str) -> str:
    host = raw.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


def _read_dashboard_action_payload(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError:
        length = 0
    if length <= 0:
        return {}
    if length > 64 * 1024:
        return {"_payload_error": "dashboard action payload is too large"}
    raw = handler.rfile.read(length)
    content_type = str(handler.headers.get("Content-Type") or "").split(";", 1)[0].strip()
    if content_type == "application/json":
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {"_payload_error": "invalid JSON payload"}
        if isinstance(data, dict):
            return data
        return {"_payload_error": "JSON payload must be an object"}
    if content_type == "application/x-www-form-urlencoded":
        form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        payload: dict[str, Any] = {}
        for key, values in form.items():
            payload[key] = values if key == "projects" else (values[-1] if values else "")
        return payload
    return {"_payload_error": "unsupported dashboard action content type"}


def _handle_dashboard_action(
    daemon: WorkerBeeDaemon,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    payload_error = payload.get("_payload_error")
    if payload_error:
        return 400, {"ok": False, "error": str(payload_error)}
    token = str(payload.get("token") or "")
    if not secrets.compare_digest(token, daemon.dashboard_action_token):
        return 403, {"ok": False, "error": "invalid dashboard action token"}
    action = str(payload.get("action") or "")
    if action not in _DASHBOARD_ACTIONS:
        return 400, {"ok": False, "error": f"unknown dashboard action: {action}"}
    projects = _dashboard_project_names(payload)
    job = daemon.enqueue_dashboard_action(action, projects)
    return 202, {
        "ok": True,
        "action": action,
        "job_id": job["job_id"],
        "job": job,
    }


def _empty_project_action_result(*, state_root: Path, purge: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "state_root": str(state_root),
        "purge": purge,
        "unregistered": [],
        "projects": [],
        "errors": [],
    }


def _dashboard_project_names(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("projects")
    if raw is None:
        raw = payload.get("project")
    values: list[object]
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, tuple):
        values = list(raw)
    elif raw is None:
        values = []
    else:
        values = str(raw).split(",")
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        name = project_slug(text)
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _dashboard_action_result_error(result: dict[str, Any]) -> str | None:
    if result.get("ok") is not False:
        return None
    raw_error = result.get("error")
    if raw_error:
        return str(raw_error)
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict) and first.get("error"):
            project = first.get("project")
            prefix = f"{project}: " if project else ""
            return f"{prefix}{first['error']}"
        return str(first)
    return "dashboard action failed"


def _dashboard_summary(
    projects: list[dict[str, Any]],
    *,
    global_dashboard: dict[str, Any],
) -> dict[str, Any]:
    errors = [item for item in projects if item.get("error")]
    running = [item for item in projects if item.get("running")]
    stopped = [item for item in projects if not item.get("running") and not item.get("error")]
    ingress = [item for item in projects if item.get("ingress_ready")]
    health_probe = global_dashboard.get("health_probe")
    health_ok = bool(health_probe.get("ok")) if isinstance(health_probe, dict) else False
    return {
        "mcp_running": True,
        "global_ingress_running": bool(global_dashboard.get("running")),
        "https_health": health_ok,
        "projects_total": len(projects),
        "projects_running": len(running),
        "projects_stopped": len(stopped),
        "projects_error": len(errors),
        "projects_ingress_ready": len(ingress),
    }


def _dashboard_url(status: dict[str, Any]) -> str | None:
    stack = status.get("stack") if isinstance(status.get("stack"), dict) else None
    if not stack:
        return None
    raw = stack.get("dashboard_url")
    return str(raw) if raw else None


def _project_status_kind(*, stack_running: bool, profile_running: bool) -> str:
    if stack_running and profile_running:
        return "stack+profile"
    if profile_running:
        return "profile"
    if stack_running:
        return "stack"
    return "stopped"


def _stack_started_event(*, project: str, info: Any, reason: str) -> dict[str, Any]:
    return {
        "type": "project_stack_started",
        "project": project,
        "reason": reason,
        "dashboard_url": getattr(info, "dashboard_url", None),
        "controller_url": getattr(info, "controller_url", None),
        "apishim_url": getattr(info, "apishim_url", None),
    }


def _with_events(payload: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(payload)
    existing = result.get("events")
    result["events"] = events + (existing if isinstance(existing, list) else [])
    first = events[0] if events else {}
    if first.get("dashboard_url") and "user_message" not in result:
        result["user_message"] = (
            f"WorkerBee project `{first.get('project')}` started for {first.get('reason')}. "
            f"Dashboard: {first.get('dashboard_url')}"
        )
    return result


def _safe_project_mode(raw: object) -> str:
    try:
        return normalize_project_mode(str(raw or DEFAULT_PROJECT_MODE))
    except WorkerBeeError:
        return DEFAULT_PROJECT_MODE


def _utc_timestamp_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _utc_timestamp_slug() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _deployment_paths(state_root: Path, project: str) -> dict[str, Path]:
    directory = daemon_project_state_dir(project, state_root=state_root) / "deployments"
    return {"dir": directory, "latest": directory / "latest.json"}


def _available_stage_names(state_dir: Path) -> list[str]:
    staged_root = state_dir / "artifacts" / "staged"
    if not staged_root.is_dir():
        return []
    return sorted(path.name for path in staged_root.iterdir() if path.is_dir())


def _deployment_validation_summary(raw: object) -> dict[str, Any]:
    validation = raw if isinstance(raw, dict) else {}
    return {
        "ok": validation.get("ok"),
        "stage_dir": validation.get("stage_dir"),
        "input_kinds": validation.get("input_kinds", []),
        "images": validation.get("images", []),
        "manifests": validation.get("manifests", []),
        "findings": validation.get("findings", []),
    }


def _deployment_apply_summary(raw: object) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else []
    summary: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        summary.append(
            {
                "ok": item.get("ok"),
                "manifest": item.get("manifest"),
                "input_kind": item.get("input_kind"),
                "namespace": item.get("namespace"),
                "ingress_urls": item.get("ingress_urls", []),
            }
        )
    return summary


def _deployment_ingress_urls(raw: object) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if (
            isinstance(value, str)
            and value.startswith(("http://", "https://"))
            and value not in seen
        ):
            seen.add(value)
            urls.append(value)

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"admin_token", "token", "read_token", "apishim_token"}:
                    continue
                if key in {"ingress_urls", "urls"}:
                    visit(item)
                elif key.startswith("public_") or key.endswith("_url") or key == "server":
                    add(item)
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        else:
            add(value)

    visit(raw)
    return urls


def _deployment_exports(stage_dir: Path) -> dict[str, str]:
    exports: dict[str, str] = {}
    root = stage_dir.expanduser().resolve() / "exports"
    if not root.is_dir():
        return exports
    for fmt in ("k1s", "k8s", "helm"):
        candidate = root / fmt
        if candidate.is_dir():
            exports[fmt] = str(candidate)
    return exports


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _read_optional_text(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _profile_ingress_refresh_metadata(
    *,
    refresh_ingress: bool,
    running: bool,
    before_dashboard_url: str | None,
    before_site_text: str | None,
    after_site_text: str | None,
    site_path: Path | None,
    profile: object,
    error: str | None = None,
) -> dict[str, Any]:
    profile_data = profile if isinstance(profile, dict) else {}
    profile_urls = (
        profile_data.get("ingress_urls")
        if isinstance(profile_data.get("ingress_urls"), dict)
        else {}
    )
    dashboard_url = str(profile_urls.get("dashboard")) if profile_urls.get("dashboard") else None
    missing_before = running and (not before_dashboard_url or before_site_text is None)
    changed = running and before_site_text is not None and after_site_text is not None and (
        before_site_text != after_site_text
    )
    repaired = bool(
        refresh_ingress
        and dashboard_url
        and after_site_text
        and (missing_before or changed)
    )
    if error:
        reason = error
    elif not refresh_ingress:
        reason = "global ingress unavailable"
    elif not running:
        reason = "profile not running"
    elif repaired:
        reason = "profile ingress route repaired"
    else:
        reason = "profile ingress current"
    return {
        "attempted": refresh_ingress,
        "active": refresh_ingress,
        "running": running,
        "site": str(site_path) if site_path is not None else None,
        "site_existed_before": before_site_text is not None,
        "site_exists": after_site_text is not None,
        "before_dashboard_url": before_dashboard_url,
        "dashboard_url": dashboard_url,
        "repaired": repaired,
        "sync_needed": repaired,
        "reason": reason,
    }


def _project_exposed_routes(state_dir: Path, *, https_port: int) -> list[dict[str, Any]]:
    sites_dir = state_dir / "caddy"
    if not sites_dir.is_dir():
        return []
    routes: list[dict[str, Any]] = []
    for path in sorted(sites_dir.glob("*.caddy")):
        routes.extend(_caddy_exposed_routes(path, https_port=https_port))
    return routes


def _caddy_exposed_routes(path: Path, *, https_port: int) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    routes: list[dict[str, Any]] = []
    hosts: list[str] = []
    path_stack: list[tuple[int, str]] = []
    depth = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        depth_before = depth
        path_stack = [
            (item_depth, item_path)
            for item_depth, item_path in path_stack
            if item_depth <= depth_before
        ]
        if depth_before == 0 and "{" in line:
            hosts = _caddy_site_hosts(line.split("{", 1)[0])
        if hosts and line.startswith(("handle ", "handle_path ")):
            matcher = _caddy_handle_path(line)
            if matcher:
                path_stack.append((depth_before + 1, matcher))
        if hosts and line.startswith("reverse_proxy "):
            route_path = path_stack[-1][1] if path_stack else None
            upstreams, proxy_path = _caddy_reverse_proxy_targets(line)
            if proxy_path and route_path is None:
                route_path = proxy_path
            path_matchers = [route_path] if route_path else []
            routes.append(
                {
                    "type": _classify_caddy_route(
                        source_file=path.name,
                        hosts=hosts,
                        path_matchers=path_matchers,
                    ),
                    "source_file": path.name,
                    "hosts": list(hosts),
                    "path_matchers": path_matchers,
                    "upstreams": upstreams,
                    "public_urls": _caddy_public_urls(
                        hosts=hosts,
                        path_matchers=path_matchers,
                        https_port=https_port,
                    ),
                }
            )
        depth = max(0, depth + line.count("{") - line.count("}"))
        if depth == 0:
            hosts = []
            path_stack = []
    return routes


def _caddy_site_hosts(raw: str) -> list[str]:
    hosts: list[str] = []
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        if "://" in text:
            parsed = urlsplit(text)
            host = parsed.hostname or ""
        else:
            host = text.split()[0].split(":", 1)[0]
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _caddy_handle_path(line: str) -> str | None:
    expression = line.split("{", 1)[0].strip()
    parts = expression.split()
    if len(parts) < 2:
        return None
    candidate = parts[1].strip()
    return candidate if candidate.startswith("/") else None


def _caddy_reverse_proxy_targets(line: str) -> tuple[list[str], str | None]:
    expression = line.split("{", 1)[0].strip()
    raw_targets = expression.removeprefix("reverse_proxy").strip().split()
    upstreams: list[str] = []
    proxy_path: str | None = None
    for target in raw_targets:
        if target.startswith("/"):
            proxy_path = target
            continue
        if _caddy_proxy_target_option(target):
            continue
        upstreams.append(target)
    return upstreams, proxy_path


def _caddy_proxy_target_option(target: str) -> bool:
    return target.startswith("{") or target in {"*", "/*"}


def _classify_caddy_route(
    *,
    source_file: str,
    hosts: list[str],
    path_matchers: list[str],
) -> str:
    if any(path.startswith("/static/dash-assets") for path in path_matchers):
        return "static-assets"
    if source_file == "k1s-profile.caddy" or source_file == "k1s-stack.caddy":
        if any(host.startswith("k1s-api.") for host in hosts):
            return "k1s-api"
        if any(host.startswith("k1s.") for host in hosts):
            return "k1s-dashboard"
        if any(host.startswith("k1s-dash.") for host in hosts):
            return "legacy-dashboard"
    if any(host.startswith("api.") for host in hosts):
        return "api"
    if any(host.startswith("app.") for host in hosts):
        return "app"
    if any(host.startswith("s3.") for host in hosts):
        return "app"
    return "unknown"


def _caddy_public_urls(
    *,
    hosts: list[str],
    path_matchers: list[str],
    https_port: int,
) -> list[str]:
    suffixes = path_matchers or ["/"]
    urls: list[str] = []
    for host in hosts:
        for suffix in suffixes:
            path = suffix if suffix.startswith("/") else f"/{suffix}"
            urls.append(f"https://{host}:{https_port}{path}")
    return urls


def _exposed_route_summary(routes: list[dict[str, Any]]) -> str:
    if not routes:
        return "none"
    hosts = {
        host
        for route in routes
        for host in route.get("hosts", [])
        if isinstance(host, str)
    }
    return f"{len(routes)} route(s), {len(hosts)} host(s)"


def _render_route_details(item: dict[str, Any]) -> str:
    routes = item.get("exposed_routes")
    if not isinstance(routes, list) or not routes:
        return '<span class="muted">No Caddy routes are currently exposed for this project.</span>'
    rows: list[str] = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        urls = route.get("public_urls") if isinstance(route.get("public_urls"), list) else []
        upstreams = route.get("upstreams") if isinstance(route.get("upstreams"), list) else []
        url_html = "<br>".join(_link(url) for url in urls) or '<span class="muted">-</span>'
        upstream_html = "<br>".join(_esc(item) for item in upstreams) or "-"
        rows.append(
            "<tr>"
            f"<td>{_esc(route.get('type'))}</td>"
            f"<td>{url_html}</td>"
            f"<td>{upstream_html}</td>"
            f"<td>{_esc(route.get('source_file'))}</td>"
            "</tr>"
        )
    return (
        '<div class="route-panel">'
        '<table class="route-table">'
        "<thead><tr><th>Type</th><th>Public URL</th><th>Upstream</th><th>Source</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
        "</div>"
    )


def _render_dashboard(payload: dict[str, Any], *, action_token: str = "") -> str:
    projects = payload.get("projects") if isinstance(payload.get("projects"), list) else []
    rows = []
    for item in projects:
        if not isinstance(item, dict):
            continue
        project = _esc(item.get("project"))
        mode = _esc(item.get("mode") or DEFAULT_PROJECT_MODE)
        stack_class = "ok" if item.get("stack_running") else "idle"
        stack_label = "running" if item.get("stack_running") else "stopped"
        profile_class = "ok" if item.get("profile_running") else "idle"
        profile_label = item.get("profile_name") or "none"
        if item.get("profile_running"):
            profile_label = f"{profile_label} running"
        ingress_status = str(item.get("ingress_status") or "idle")
        ingress_class = (
            "ok"
            if ingress_status == "ready"
            else ("warn" if ingress_status == "missing" else "idle")
        )
        error = item.get("error")
        route_count = int(item.get("exposed_route_count") or 0)
        rows.append(
            "<tr>"
            f'<td><input type="checkbox" class="project-select" value="{project}" '
            f'aria-label="Select {project}"></td>'
            f"<td>{project}</td>"
            f'<td><span class="pill idle">{mode}</span></td>'
            f'<td><span class="pill {stack_class}">{stack_label}</span></td>'
            f'<td><span class="pill {profile_class}">{_esc(profile_label)}</span></td>'
            f'<td><span class="pill {ingress_class}">{_esc(ingress_status)}</span></td>'
            "<td>"
            f'<button class="route-toggle" data-project="{project}" '
            f'aria-expanded="false">Routes {route_count}</button>'
            "</td>"
            f"<td>{_link(item.get('dashboard_url'))}</td>"
            f"<td>{_esc(item.get('git_branch'))}</td>"
            f"<td>{_esc(error)}</td>"
            f"<td>{_esc(item.get('state_dir'))}</td>"
            '<td class="row-actions"><div class="row-actions-inner">'
            f'<button data-action="start_projects" data-project="{project}">Start</button>'
            f'<button data-action="stop_projects" data-project="{project}">Stop</button>'
            f'<button class="danger" data-action="delete_projects" '
            f'data-project="{project}">Delete</button>'
            "</div></td>"
            "</tr>"
            f'<tr class="route-details" data-route-project="{project}" hidden>'
            f'<td colspan="12">{_render_route_details(item)}</td>'
            "</tr>"
        )
    if not rows:
        rows.append(
            '<tr><td class="muted" colspan="12">No WorkerBee projects registered.</td></tr>'
        )
    global_dash = payload.get("global_dashboard")
    ingress_json = json.dumps(global_dash, indent=2, sort_keys=True)
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="workerbee-action-token" content="{_esc(action_token)}">
    <title>WorkerBee Projects</title>
    <style>
      :root {{
        color-scheme: dark;
        --header-h: 60px;
        --k1s-brand-gold: #fbc02d;
        --k1s-brand-graphite: #404040;
        --panel-edge: #8884;
        --text: #f2f5f8;
        --muted: #c4ccd5;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
        color: var(--text);
        overflow-x: hidden;
        background-color: #0b0f14;
        background-image:
          linear-gradient(rgba(7, 10, 14, 0.72), rgba(7, 10, 14, 0.72)),
          url('{DASHBOARD_BACKGROUND_PATH}');
        background-size: 100% 100%, cover;
        background-position: center, center top;
        background-repeat: no-repeat, no-repeat;
      }}
      header {{
        min-height: var(--header-h);
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 10px 14px;
        background: #0a0a0a10;
        position: sticky;
        top: 0;
        backdrop-filter: blur(4px);
        -webkit-backdrop-filter: blur(4px);
        z-index: 40;
        border-bottom: 1px solid var(--panel-edge);
      }}
      header::after {{
        content: "";
        position: absolute;
        left: 14px;
        right: 14px;
        bottom: 0;
        height: 2px;
        border-radius: 999px;
        background: linear-gradient(90deg, transparent, var(--k1s-brand-gold), transparent);
        opacity: .5;
        pointer-events: none;
      }}
      .brand-title {{
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 18px;
        letter-spacing: .01em;
      }}
      .brand-logo {{
        width: 28px;
        height: 28px;
        border-radius: 999px;
        box-shadow: 0 6px 14px rgba(0, 0, 0, .2);
      }}
      h1 {{ margin: 0; font-size: 18px; }}
      h2 {{ font-size: 14px; margin: 14px 4px 6px; opacity: .9; }}
      .brand-accent {{ color: var(--k1s-brand-gold); }}
      .caption {{ color: var(--muted); font-size: 13px; }}
      main {{
        display: grid;
        grid-template-columns: 1fr;
        gap: 12px;
        width: min(1440px, 100%);
        margin: 0 auto;
        padding: 12px 12px 48px;
      }}
      .card {{
        border: 1px solid var(--panel-edge);
        border-radius: 8px;
        padding: 8px 10px;
        min-width: 0;
        max-width: 100%;
        overflow: hidden;
        position: relative;
        background-color: rgba(7, 10, 14, 0.18);
        background-image: linear-gradient(135deg, rgba(25, 30, 36, 0.35), rgba(25, 30, 36, 0.45));
        background-size: 100% 100%;
        background-position: center;
        background-repeat: no-repeat;
        backdrop-filter: blur(6px);
        -webkit-backdrop-filter: blur(6px);
      }}
      .card::before {{
        content: "";
        position: absolute;
        inset: 0;
        background-image: url('{DASHBOARD_BACKGROUND_PATH}');
        background-size: cover;
        background-position: center;
        background-repeat: no-repeat;
        opacity: 0.12;
        filter: saturate(0.28) brightness(0.5);
        pointer-events: none;
      }}
      .card > * {{ position: relative; z-index: 1; }}
      .table-wrap {{ overflow-x: auto; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{
        border-bottom: 1px solid var(--panel-edge);
        padding: 6px;
        text-align: left;
        font-size: 13px;
      }}
      th {{ color: var(--k1s-brand-gold); font-weight: 650; }}
      a {{ color: #ffe082; text-decoration: none; }}
      a:hover {{ text-decoration: underline; }}
      code, pre {{
        background: rgba(0, 0, 0, .24);
        border: 1px solid var(--panel-edge);
        border-radius: 4px;
      }}
      pre {{ margin: 0; padding: 10px; overflow-x: auto; color: #e7edf4; }}
      button {{
        border: 1px solid var(--panel-edge);
        border-radius: 6px;
        padding: 4px 8px;
        font: inherit;
        font-size: 12px;
        color: var(--text);
        background: rgba(7, 10, 14, .42);
        cursor: pointer;
      }}
      button:hover {{ border-color: var(--k1s-brand-gold); color: #ffe082; }}
      button.danger:hover {{ border-color: rgba(244, 67, 54, .7); color: #ffcdd2; }}
      select {{
        border: 1px solid var(--panel-edge);
        border-radius: 6px;
        padding: 3px 6px;
        font: inherit;
        font-size: 12px;
        color: var(--text);
        background: rgba(7, 10, 14, .72);
      }}
      input[type="checkbox"] {{ accent-color: var(--k1s-brand-gold); }}
      .actions {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
      }}
      .refresh-controls {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        color: var(--muted);
        font-size: 12px;
      }}
      .summary-grid, .jobs-grid {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
      }}
      .summary-grid {{ margin: 8px 0 2px; }}
      .jobs-grid {{ margin-top: 8px; }}
      .route-details td {{ background: rgba(0, 0, 0, .14); }}
      .route-panel {{ padding: 8px 0; }}
      .route-table th, .route-table td {{ font-size: 12px; vertical-align: top; }}
      .route-toggle {{ white-space: nowrap; }}
      .row-actions {{ white-space: nowrap; }}
      .row-actions-inner {{ display: inline-flex; gap: 6px; align-items: center; }}
      #action-result {{ margin-top: 10px; white-space: pre-wrap; }}
      .pill {{
        display: inline-flex;
        align-items: center;
        border: 1px solid var(--panel-edge);
        border-radius: 999px;
        padding: 1px 6px;
        font-size: 12px;
        background: rgba(0, 0, 0, .2);
        white-space: nowrap;
      }}
      .pill.ok {{ border-color: rgba(76, 175, 80, .55); color: #b9f6ca; }}
      .pill.idle {{ border-color: rgba(251, 192, 45, .45); color: #ffe082; }}
      .pill.warn {{ border-color: rgba(255, 152, 0, .6); color: #ffd180; }}
      .pill.bad {{ border-color: rgba(244, 67, 54, .7); color: #ffcdd2; }}
      .muted {{ color: var(--muted); }}
      @media (max-width: 720px) {{
        header {{ align-items: flex-start; flex-direction: column; }}
        .caption {{ font-size: 12px; }}
      }}
    </style>
  </head>
  <body>
    <header>
      <div class="brand-title">
        <img class="brand-logo" alt="k1s logo" src="{DASHBOARD_LOGO_PATH}">
        <h1><span class="brand-accent">WorkerBee</span> Projects</h1>
      </div>
      <div class="caption">Global dashboard</div>
    </header>
    <main>
      <section class="card">
        <h2>Controls</h2>
        <div class="actions">
          <button id="start-selected" data-action="start_projects">Start Selected</button>
          <button id="stop-selected" data-action="stop_projects">Stop Selected</button>
          <button id="delete-selected" class="danger" data-action="delete_projects">
            Delete Selected
          </button>
          <button id="start-all" data-action="start_all_projects">Start All</button>
          <button id="stop-all" data-action="stop_all_projects">Stop All</button>
          <button id="delete-all" class="danger" data-action="delete_all_projects">
            Delete All
          </button>
          <button id="mcp-reboot" data-action="mcp_reboot">Reboot MCP</button>
          <button id="mcp-shutdown" class="danger" data-action="mcp_shutdown">
            Shutdown MCP
          </button>
          <label class="refresh-controls">
            Refresh
            <select id="refresh-interval" aria-label="Refresh interval">
              <option value="0">Off</option>
              <option value="2000">2s</option>
              <option value="5000" selected>5s</option>
              <option value="10000">10s</option>
              <option value="30000">30s</option>
            </select>
          </label>
          <span id="refresh-status" class="muted">not refreshed</span>
        </div>
        <div id="summary-grid" class="summary-grid"></div>
        <div id="jobs-grid" class="jobs-grid"></div>
        <pre id="action-result" hidden></pre>
      </section>
      <section class="card">
        <h2>Projects</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Select</th><th>Project</th><th>Mode</th><th>App Stack</th>
                <th>k1s Profile</th><th>Ingress</th><th>Routes</th><th>Dashboard</th>
                <th>Git Branch</th><th>Error</th><th>State</th><th>Actions</th>
              </tr>
            </thead>
            <tbody id="projects-body">{''.join(rows)}</tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <h2>Ingress</h2>
        <pre id="ingress-json">{_esc(ingress_json)}</pre>
      </section>
    </main>
    <script>
      const token = document.querySelector('meta[name="workerbee-action-token"]').content;
      const resultBox = document.getElementById('action-result');
      const summaryGrid = document.getElementById('summary-grid');
      const jobsGrid = document.getElementById('jobs-grid');
      const projectsBody = document.getElementById('projects-body');
      const ingressBox = document.getElementById('ingress-json');
      const refreshSelect = document.getElementById('refresh-interval');
      const refreshStatus = document.getElementById('refresh-status');
      const refreshKey = 'workerbee.dashboard.refreshIntervalMs';
      let refreshTimer = null;
      const activeJobIds = new Set();
      const expandedRouteProjects = new Set();

      function escapeHtml(value) {{
        return String(value ?? '')
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;')
          .replace(/'/g, '&#39;');
      }}

      function link(url) {{
        if (!url) return '<span class="muted">-</span>';
        const safe = escapeHtml(url);
        return `<a href="${{safe}}" target="_blank" rel="noreferrer">${{safe}}</a>`;
      }}

      function pill(label, state = 'idle') {{
        return `<span class="pill ${{state}}">${{escapeHtml(label)}}</span>`;
      }}

      function stackPill(item) {{
        return pill(item.stack_running ? 'running' : 'stopped', item.stack_running ? 'ok' : 'idle');
      }}

      function profilePill(item) {{
        const profile = item.profile_name || 'none';
        if (item.profile_running) return pill(`${{profile}} running`, 'ok');
        return pill(profile === 'none' ? 'none' : `${{profile}} stopped`, 'idle');
      }}

      function ingressPill(item) {{
        const status = item.ingress_status || 'idle';
        const state = status === 'ready' ? 'ok' : (status === 'missing' ? 'warn' : 'idle');
        return pill(status, state);
      }}

      function routeButton(item, safeProject, expanded = false) {{
        const count = item.exposed_route_count || 0;
        return `<button class="route-toggle" data-project="${{safeProject}}" `
          + `aria-expanded="${{expanded ? 'true' : 'false'}}">Routes ${{count}}</button>`;
      }}

      function renderRouteDetails(item, safeProject, expanded = false) {{
        const hidden = expanded ? '' : ' hidden';
        const routes = Array.isArray(item.exposed_routes) ? item.exposed_routes : [];
        if (!routes.length) {{
          return `<tr class="route-details" data-route-project="${{safeProject}}"${{hidden}}>`
            + '<td colspan="12"><span class="muted">'
            + 'No Caddy routes are currently exposed for this project.'
            + '</span></td></tr>';
        }}
        const rows = routes.map((route) => {{
          const urls = Array.isArray(route.public_urls) ? route.public_urls : [];
          const upstreams = Array.isArray(route.upstreams) ? route.upstreams : [];
          const urlHtml = urls.length
            ? urls.map((url) => link(url)).join('<br>')
            : '<span class="muted">-</span>';
          const upstreamHtml = upstreams.length
            ? upstreams.map((item) => escapeHtml(item)).join('<br>')
            : '-';
          return '<tr>'
            + `<td>${{escapeHtml(route.type || 'unknown')}}</td>`
            + `<td>${{urlHtml}}</td>`
            + `<td>${{upstreamHtml}}</td>`
            + `<td>${{escapeHtml(route.source_file || '')}}</td>`
            + '</tr>';
        }}).join('');
        return `<tr class="route-details" data-route-project="${{safeProject}}"${{hidden}}>`
          + '<td colspan="12"><div class="route-panel"><table class="route-table">'
          + '<thead><tr><th>Type</th><th>Public URL</th><th>Upstream</th>'
          + '<th>Source</th></tr></thead>'
          + `<tbody>${{rows}}</tbody></table></div></td></tr>`;
      }}

      function selectedSet() {{
        return new Set(selectedProjects());
      }}

      function selectedProjects() {{
        return Array.from(document.querySelectorAll('.project-select:checked'))
          .map((item) => item.value)
          .filter(Boolean);
      }}

      function renderProjects(projects) {{
        const selected = selectedSet();
        if (!Array.isArray(projects) || !projects.length) {{
          projectsBody.innerHTML =
            '<tr><td class="muted" colspan="12">No WorkerBee projects registered.</td></tr>';
          expandedRouteProjects.clear();
          configureRefreshTimer({{persist: false}});
          return;
        }}
        const presentProjects = new Set(projects.map((item) => String(item.project || '')));
        for (const project of Array.from(expandedRouteProjects)) {{
          if (!presentProjects.has(project)) expandedRouteProjects.delete(project);
        }}
        projectsBody.innerHTML = projects.map((item) => {{
          const project = String(item.project || '');
          const safeProject = escapeHtml(project);
          const checked = selected.has(project) ? ' checked' : '';
          const error = item.error || '';
          const expanded = expandedRouteProjects.has(project);
          return '<tr>'
            + `<td><input type="checkbox" class="project-select" value="${{safeProject}}" `
            + `aria-label="Select ${{safeProject}}"${{checked}}></td>`
            + `<td>${{safeProject}}</td>`
            + `<td>${{pill(item.mode || 'lazy', 'idle')}}</td>`
            + `<td>${{stackPill(item)}}</td>`
            + `<td>${{profilePill(item)}}</td>`
            + `<td>${{ingressPill(item)}}</td>`
            + `<td>${{routeButton(item, safeProject, expanded)}}</td>`
            + `<td>${{link(item.dashboard_url)}}</td>`
            + `<td>${{escapeHtml(item.git_branch || '')}}</td>`
            + `<td>${{escapeHtml(error)}}</td>`
            + `<td>${{escapeHtml(item.state_dir || '')}}</td>`
            + '<td class="row-actions"><div class="row-actions-inner">'
            + `<button data-action="start_projects" data-project="${{safeProject}}">Start</button>`
            + `<button data-action="stop_projects" data-project="${{safeProject}}">Stop</button>`
            + `<button class="danger" data-action="delete_projects" `
            + `data-project="${{safeProject}}">Delete</button>`
            + '</div></td>'
            + '</tr>'
            + renderRouteDetails(item, safeProject, expanded);
        }}).join('');
        configureRefreshTimer({{persist: false}});
      }}

      function renderSummary(summary = {{}}, globalDashboard = {{}}) {{
        const health = summary.https_health ? 'healthy' : 'unknown';
        const ingress = summary.global_ingress_running ? 'ingress running' : 'ingress stopped';
        summaryGrid.innerHTML = [
          pill('MCP running', 'ok'),
          pill(ingress, summary.global_ingress_running ? 'ok' : 'warn'),
          pill(`HTTPS ${{health}}`, summary.https_health ? 'ok' : 'warn'),
          pill(`projects ${{summary.projects_total ?? 0}}`, 'idle'),
          pill(`running ${{summary.projects_running ?? 0}}`, 'ok'),
          pill(`stopped ${{summary.projects_stopped ?? 0}}`, 'idle'),
          pill(`errors ${{summary.projects_error ?? 0}}`, summary.projects_error ? 'bad' : 'idle'),
          pill(`routes ${{summary.projects_ingress_ready ?? 0}}`, 'idle')
        ].join('');
      }}

      function jobState(job) {{
        if (job.status === 'failed') return 'bad';
        if (job.status === 'succeeded') return job.ok === false ? 'bad' : 'ok';
        if (job.status === 'running') return 'warn';
        return 'idle';
      }}

      function renderJobs(jobs = []) {{
        const recent = Array.isArray(jobs) ? jobs.slice(0, 6) : [];
        recent.forEach((job) => {{
          if (job.status === 'queued' || job.status === 'running') {{
            activeJobIds.add(job.job_id);
          }} else {{
            activeJobIds.delete(job.job_id);
          }}
        }});
        jobsGrid.innerHTML = recent.map((job) => {{
          const label = `${{job.action}}: ${{job.status}}`;
          return pill(label, jobState(job));
        }}).join('');
        updateBusyControls();
      }}

      function renderDashboard(payload) {{
        renderProjects(payload.projects || []);
        renderSummary(payload.summary || {{}}, payload.global_dashboard || {{}});
        renderJobs(payload.action_jobs || []);
        ingressBox.textContent = JSON.stringify(payload.global_dashboard || {{}}, null, 2);
        const stamp = payload.updated_at ? new Date(payload.updated_at * 1000) : new Date();
        const updated = `updated ${{stamp.toLocaleTimeString()}}`;
        refreshStatus.textContent = expandedRouteProjects.size
          ? `${{updated}}; refresh paused: route details open`
          : updated;
      }}

      async function refreshProjects(options = {{}}) {{
        if (!options.silent) refreshStatus.textContent = 'refreshing...';
        const response = await fetch('/api/projects', {{
          headers: {{'Accept': 'application/json'}},
          cache: 'no-store'
        }});
        if (!response.ok) throw new Error(`status ${{response.status}}`);
        const payload = await response.json();
        renderDashboard(payload);
        return payload;
      }}

      function scheduleRefreshAttempt() {{
        setTimeout(() => refreshProjects({{silent: true}}).catch(() => {{}}), 1000);
        setTimeout(() => refreshProjects({{silent: true}}).catch(() => {{}}), 3000);
      }}

      function configureRefreshTimer(options = {{}}) {{
        if (refreshTimer) {{
          clearInterval(refreshTimer);
          refreshTimer = null;
        }}
        const ms = parseInt(refreshSelect.value, 10) || 0;
        if (options.persist !== false) {{
          localStorage.setItem(refreshKey, String(ms));
        }}
        if (expandedRouteProjects.size > 0) {{
          refreshStatus.textContent = 'refresh paused: route details open';
          return;
        }}
        if (ms > 0) {{
          refreshTimer = setInterval(() => {{
            refreshProjects({{silent: true}}).catch((err) => {{
              refreshStatus.textContent = `refresh failed: ${{err.message}}`;
            }});
          }}, ms);
        }}
      }}

      function confirmation(action, projects) {{
        const count = projects.length || 'all';
        const messages = {{
          start_projects: `Start ${{count}} WorkerBee project(s)?`,
          stop_projects: `Stop ${{count}} WorkerBee project(s)?`,
          delete_projects: `Delete ${{count}} WorkerBee project(s), including state?`,
          start_all_projects: 'Start all WorkerBee projects?',
          stop_all_projects: 'Stop all WorkerBee projects?',
          delete_all_projects: 'Delete all WorkerBee projects, including state?',
          mcp_reboot: 'Reboot the WorkerBee MCP server?',
          mcp_shutdown: 'Shutdown the WorkerBee MCP server?'
        }};
        return window.confirm(messages[action] || `Run ${{action}}?`);
      }}

      function updateBusyControls() {{
        const busy = activeJobIds.size > 0;
        document.querySelectorAll('button[data-action]').forEach((button) => {{
          button.disabled = busy;
        }});
      }}

      function finishJob(job) {{
        activeJobIds.delete(job.job_id);
        updateBusyControls();
        resultBox.hidden = false;
        resultBox.textContent = JSON.stringify(job, null, 2);
        refreshProjects({{silent: true}}).catch((err) => {{
          refreshStatus.textContent = `refresh failed: ${{err.message}}`;
        }});
      }}

      function pollJob(jobId, attempt = 0) {{
        fetch(`/api/action-jobs/${{encodeURIComponent(jobId)}}`, {{
          headers: {{'Accept': 'application/json'}},
          cache: 'no-store'
        }})
          .then((response) => {{
            if (!response.ok) throw new Error(`status ${{response.status}}`);
            return response.json();
          }})
          .then((payload) => {{
            const job = payload.job || {{}};
            if (job.status === 'succeeded' || job.status === 'failed') {{
              finishJob(job);
              return;
            }}
            resultBox.textContent =
              `Running ${{job.action || 'action'}}... ${{job.status || 'queued'}}`;
            setTimeout(() => pollJob(jobId, attempt + 1), 750);
          }})
          .catch((err) => {{
            resultBox.hidden = false;
            resultBox.textContent =
              `Verifying current state after interrupted poll (${{err.message}})...`;
            scheduleRefreshAttempt();
            if (attempt < 60) {{
              setTimeout(() => pollJob(jobId, attempt + 1), 1000);
            }} else {{
              activeJobIds.delete(jobId);
              updateBusyControls();
            }}
          }});
      }}

      function toggleRouteDetails(button) {{
        const project = button.dataset.project || '';
        const row = document.querySelector(
          `.route-details[data-route-project="${{CSS.escape(project)}}"]`
        );
        if (!row) return;
        const expanded = button.getAttribute('aria-expanded') === 'true';
        row.hidden = expanded;
        button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
        if (expanded) {{
          expandedRouteProjects.delete(project);
        }} else {{
          expandedRouteProjects.add(project);
        }}
        configureRefreshTimer({{persist: false}});
      }}

      async function postAction(action, projects = []) {{
        if (!confirmation(action, projects)) return;
        resultBox.hidden = false;
        resultBox.textContent = `Scheduling ${{action}}...`;
        try {{
          const response = await fetch('/api/actions', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{action, projects, token}})
          }});
          const payload = await response.json();
          if (response.status === 202 && payload.job_id) {{
            activeJobIds.add(payload.job_id);
            updateBusyControls();
            resultBox.textContent = `Queued ${{action}} as ${{payload.job_id}}`;
            pollJob(payload.job_id);
            scheduleRefreshAttempt();
            return;
          }}
          resultBox.textContent = JSON.stringify(payload, null, 2);
        }} catch (err) {{
          resultBox.textContent = `${{action}} request was interrupted (${{err.message}}).`;
          if (!action.startsWith('mcp_')) {{
            resultBox.textContent += ' Refreshing to verify current state...';
            scheduleRefreshAttempt();
          }}
        }}
      }}

      document.addEventListener('click', (event) => {{
        const routeButton = event.target.closest('button.route-toggle');
        if (routeButton) {{
          toggleRouteDetails(routeButton);
          return;
        }}
        const button = event.target.closest('button[data-action]');
        if (!button) return;
          const action = button.dataset.action;
          const project = button.dataset.project;
          let projects = project ? [project] : [];
          if (
            action === 'start_projects' ||
            action === 'stop_projects' ||
            action === 'delete_projects'
          ) {{
            projects = projects.length ? projects : selectedProjects();
            if (!projects.length) {{
              window.alert('Select at least one WorkerBee project.');
              return;
            }}
          }}
          postAction(action, projects);
      }});

      refreshSelect.value = localStorage.getItem(refreshKey) || refreshSelect.value || '5000';
      refreshSelect.addEventListener('change', () => {{
        configureRefreshTimer();
        refreshProjects({{silent: false}}).catch((err) => {{
          refreshStatus.textContent = `refresh failed: ${{err.message}}`;
        }});
      }});
      configureRefreshTimer();
      refreshProjects({{silent: true}}).catch((err) => {{
        refreshStatus.textContent = `refresh failed: ${{err.message}}`;
      }});
    </script>
  </body>
</html>
"""


def _dashboard_static_asset(path: str) -> tuple[bytes, str] | None:
    background_aliases = {
        DASHBOARD_BACKGROUND_PATH,
        "/static/dash-assets/page-background-1920x1080.png",
        "/static/dash-assets/page-background-3840x2160.png",
        "/static/dash-assets/page-background-tile-1024.png",
        "/static/dash-assets/system-graph-background-1920x1080.png",
    }
    if path in background_aliases:
        filename = "page-background-1920x1080.webp"
        content_type = "image/webp"
    elif path == DASHBOARD_LOGO_PATH:
        filename = "k1s-logo-32.png"
        content_type = "image/png"
    else:
        return None
    try:
        body = files("workerbee.assets").joinpath("dashboard", filename).read_bytes()
    except FileNotFoundError:
        return None
    return body, content_type


def _copy_realtime_contexts(state_root: Path, project: str) -> dict[str, Path]:
    root = state_root / "projects" / project / "artifacts" / "image-contexts" / "realtime"
    out: dict[str, Path] = {}
    for app in ("db", "backend", "frontend"):
        source = files("workerbee.assets").joinpath("realtime", app)
        dest = root / app
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.is_file():
                (dest / item.name).write_bytes(item.read_bytes())
        out[app] = dest
    return out


def _profile_control_plane_checks(connection: dict[str, Any]) -> list[dict[str, Any]]:
    ca_bundle = str(connection["ca_bundle"])
    urls = connection.get("urls") if isinstance(connection.get("urls"), dict) else {}
    checks = [
        ("dashboard", urls.get("dashboard"), None),
        ("docs", urls.get("docs"), None),
        ("controller-health", urls.get("controller_health"), connection.get("read_token")),
        ("api-healthz", urls.get("api_healthz"), connection.get("apishim_token")),
        ("api-openapi-v3", urls.get("api_openapi_v3"), connection.get("apishim_token")),
    ]
    results: list[dict[str, Any]] = []
    for name, url, token in checks:
        if not url:
            results.append({"name": name, "ok": False, "error": "url missing"})
            continue
        try:
            resp = _request_profile_public_url(
                str(url),
                token=str(token) if token else None,
                ca_bundle=ca_bundle,
            )
            results.append(
                {"name": name, "ok": resp.status == 200, "status": resp.status, "url": url}
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "ok": False, "url": url, "error": str(exc)})
    return results


def _request_profile_public_url(
    url: str,
    *,
    token: str | None,
    ca_bundle: str,
) -> Any:
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        port = int(parsed.port or 443)
        loopback_url = urlunsplit(
            (
                parsed.scheme,
                f"127.0.0.1:{port}",
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        return request_https_via_loopback(
            loopback_url,
            server_hostname=parsed.hostname,
            host_header=parsed.netloc,
            token=token,
            ca_bundle=ca_bundle,
        )
    return request(url, token=token, ca_bundle=ca_bundle)


def _probe_with_retry(
    daemon: WorkerBeeDaemon,
    *,
    project: str,
    host: str,
    path: str,
    expected_status: int,
    timeout: float,
    body_contains: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + min(float(timeout), 60.0)
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            last = daemon.ingress_probe(
                project=project,
                host=host,
                path=path,
                expected_status=expected_status,
                body_contains=body_contains,
                timeout=5.0,
            )
            if last.get("ok"):
                return last
        except Exception as exc:  # noqa: BLE001
            last = {"ok": False, "host": host, "path": path, "error": str(exc)}
        time.sleep(2.0)
    return last or {"ok": False, "host": host, "path": path, "error": "probe timed out"}


def _websocket_probe(
    url: str,
    *,
    ca_bundle: str,
    expected: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + min(float(timeout), 60.0)
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            return _websocket_probe_once(url, ca_bundle=ca_bundle, expected=expected)
        except Exception as exc:  # noqa: BLE001
            last = {"ok": False, "url": url, "error": str(exc)}
            time.sleep(2.0)
    return last or {"ok": False, "url": url, "error": "websocket probe timed out"}


def _websocket_probe_once(url: str, *, ca_bundle: str, expected: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme != "wss":
        raise ValueError("websocket probe URL must use wss")
    host = parsed.hostname or ""
    port = int(parsed.port or 443)
    host_header = parsed.netloc or f"{host}:{port}"
    path = parsed.path or "/"
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    context = ssl.create_default_context(cafile=ca_bundle)
    with (
        socket.create_connection(("127.0.0.1", port), timeout=8) as raw,
        context.wrap_socket(raw, server_hostname=host) as sock,
    ):
        request_bytes = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request_bytes)
        headers = _read_until(sock, b"\r\n\r\n", limit=8192)
        if b" 101 " not in headers.split(b"\r\n", 1)[0]:
            raise RuntimeError(headers.decode("utf-8", errors="replace")[:500])
        _send_ws_text(sock, "workerbee")
        text = _read_ws_text(sock)
    return {"ok": text == expected, "url": url, "message": text, "expected": expected}


def _read_until(sock: socket.socket, marker: bytes, *, limit: int) -> bytes:
    data = b""
    while marker not in data and len(data) < limit:
        chunk = sock.recv(1024)
        if not chunk:
            break
        data += chunk
    return data


def _send_ws_text(sock: socket.socket, text: str) -> None:
    data = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    if len(data) < 126:
        header.append(0x80 | len(data))
    else:
        header.extend([0x80 | 126, *struct.pack("!H", len(data))])
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    sock.sendall(bytes(header) + mask + masked)


def _read_ws_text(sock: socket.socket) -> str:
    header = _recv_exact(sock, 2)
    if len(header) != 2:
        return ""
    _flags, second = header
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    payload = _recv_exact(sock, length)
    return payload.decode("utf-8", errors="replace")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _link(raw: object) -> str:
    if not raw:
        return ""
    text = _esc(raw)
    return f'<a href="{text}">{text}</a>'


def _esc(raw: object) -> str:
    text = "" if raw is None else str(raw)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _normalize_deploy_target(target: str | None) -> str:
    value = (target or "workerbee").strip().lower()
    if value not in {"workerbee", "profile"}:
        raise WorkerBeeError(
            code="INVALID_DEPLOY_TARGET",
            message="deploy target must be workerbee or profile",
            details={"target": target},
        )
    return value
