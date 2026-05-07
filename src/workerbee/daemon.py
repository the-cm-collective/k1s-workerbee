"""Multi-project WorkerBee MCP daemon state."""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

from workerbee.agent import (
    DEFAULT_PROJECT_MODE,
    derive_session_project_info,
    next_actions_for_mode,
    normalize_project_mode,
    open_browser,
    runbook_payload,
    user_message_for_session,
)
from workerbee.containerd_helper import containerd_privilege_status
from workerbee.contract import WorkerBeeError
from workerbee.ingress import (
    GlobalIngress,
    GlobalIngressInfo,
    ProjectIngressConfig,
    global_ingress_status,
)
from workerbee.locks import FileLock, project_lock_path, state_root_lock_path
from workerbee.paths import daemon_project_state_dir, default_state_root
from workerbee.ports import choose_port
from workerbee.probe import build_probe_url, probe_workerbee_url
from workerbee.runtime_support import (
    cleanup_runtime,
    resolve_runtime,
    runtime_diagnostics,
)
from workerbee.supervisor import WorkerBeeSupervisor, project_slug

T = TypeVar("T")

DASHBOARD_BACKGROUND_PATH = "/static/dash-assets/page-background-1920x1080.png"
DASHBOARD_LOGO_PATH = "/static/dash-assets/k1s-logo-32.png"


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
        for name in sorted(project_names):
            record = records.get(name) or {}
            state_dir = record.get("state_dir") or str(
                daemon_project_state_dir(name, state_root=self.state_root)
            )
            try:
                sup = self._build_supervisor(name, ingress=self._project_ingress(name))
                state_dir = str(sup.state_dir)
                status = sup.status()
            except Exception as exc:  # noqa: BLE001 - dashboard must stay renderable
                status = {
                    "running": False,
                    "apishim_running": False,
                    "error": str(exc),
                }
            stack = status.get("stack") if isinstance(status, dict) else None
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
                    "running": bool(status.get("running")),
                    "apishim_running": bool(status.get("apishim_running")),
                    "dashboard_url": (stack or {}).get("dashboard_url") if stack else None,
                    "ingress": (stack or {}).get("ingress") if stack else None,
                    "error": status.get("error"),
                }
            )
        return {
            "state_root": str(self.state_root),
            "global_dashboard": self.global_dashboard(),
            "projects": items,
        }

    def project_status(self, project: str) -> dict[str, Any]:
        name = project_slug(project)
        status = self.with_project(name, lambda sup: sup.status())
        status["mode"] = self.project_mode(name)
        return status

    def capabilities(self) -> dict[str, Any]:
        from workerbee import __version__
        from workerbee.contract import API_VERSION, MCP_TOOL_NAMES

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
            "templates": ["frontend-api", "frontend-api-store", "stateless-web"],
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
            "runtime": runtime_diagnostics(self.runtime_requested, state_root=self.state_root),
            "containerd_privilege": containerd_privilege_status(
                state_root=self.state_root,
                runtime=self.runtime_requested,
            ),
        }

    def cleanup(self, *, execute: bool = False, purge_images: bool = False) -> dict[str, Any]:
        return cleanup_runtime(
            state_root=self.state_root,
            runtime=self.runtime_requested,
            execute=execute,
            purge_images=purge_images,
        )

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
            return _empty_project_action_result(state_root=self.state_root, purge=True)
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
            ingress_sync = (
                self._sync_ingress_projects_result()
                if sync_ingress
                else self._schedule_ingress_sync()
            )
        failed = [result for result in results if result.get("ok") is False]
        return {
            "ok": not errors and not failed,
            "state_root": str(self.state_root),
            "purge": purge,
            "unregistered": removed,
            "projects": results,
            "errors": errors,
            "ingress_sync": ingress_sync,
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
            timeout=timeout,
        )
        result["project"] = name
        return result

    def _project_ingress(self, project: str) -> ProjectIngressConfig | None:
        if self.ingress is None:
            return None
        return self.ingress.project_config(project)

    def _known_projects(self) -> list[str]:
        project_names = set(self._read_registry())
        if self.projects_dir.is_dir():
            project_names.update(path.name for path in self.projects_dir.iterdir() if path.is_dir())
        return sorted(project_names)

    def _sync_ingress_projects(self) -> None:
        if self.ingress is not None:
            self.ingress.sync_projects(self._known_projects())

    def _sync_ingress_projects_result(self) -> dict[str, Any]:
        self._sync_ingress_projects()
        return {"scheduled": False, "synced": self.ingress is not None}

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
                if path.startswith("/api/projects"):
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
    handler.end_headers()
    _write_response_body(handler, body)


def _send_html(handler: BaseHTTPRequestHandler, html: str) -> None:
    body = html.encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    _write_response_body(handler, body)


def _send_bytes(handler: BaseHTTPRequestHandler, body: bytes, content_type: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.end_headers()
    _write_response_body(handler, body)


def _send_not_found(handler: BaseHTTPRequestHandler) -> None:
    body = b"not found\n"
    handler.send_response(404)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    _write_response_body(handler, body)


def _write_response_body(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    try:
        handler.wfile.write(body)
    except BrokenPipeError:
        return


def _default_dashboard_scheduler(callback: Callable[[], None]) -> None:
    timer = threading.Timer(0.25, callback)
    timer.daemon = True
    timer.start()


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
    if action == "start_projects":
        result = daemon.start_projects(_dashboard_project_names(payload), sync_ingress=False)
    elif action == "stop_projects":
        result = daemon.stop_projects(_dashboard_project_names(payload), purge=False)
    elif action == "delete_projects":
        result = daemon.delete_projects(_dashboard_project_names(payload), sync_ingress=False)
    elif action == "start_all_projects":
        result = daemon.start_all_projects(sync_ingress=False)
    elif action == "stop_all_projects":
        result = daemon.stop_all_projects(purge=False)
    elif action == "delete_all_projects":
        result = daemon.delete_all_projects(sync_ingress=False)
    elif action == "mcp_shutdown":
        result = daemon.schedule_mcp_shutdown()
    elif action == "mcp_reboot":
        result = daemon.schedule_mcp_reboot()
    else:
        return 400, {"ok": False, "error": f"unknown dashboard action: {action}"}
    return 200 if result.get("ok") is not False else 500, {
        "ok": result.get("ok") is not False,
        "action": action,
        "result": result,
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


def _dashboard_url(status: dict[str, Any]) -> str | None:
    stack = status.get("stack") if isinstance(status.get("stack"), dict) else None
    if not stack:
        return None
    raw = stack.get("dashboard_url")
    return str(raw) if raw else None


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


def _render_dashboard(payload: dict[str, Any], *, action_token: str = "") -> str:
    projects = payload.get("projects") if isinstance(payload.get("projects"), list) else []
    rows = []
    for item in projects:
        if not isinstance(item, dict):
            continue
        ingress = item.get("ingress") if isinstance(item.get("ingress"), dict) else {}
        project = _esc(item.get("project"))
        running = bool(item.get("running"))
        status = "running" if running else "stopped"
        status_class = "ok" if running else "idle"
        rows.append(
            "<tr>"
            f'<td><input type="checkbox" class="project-select" value="{project}" '
            f'aria-label="Select {project}"></td>'
            f"<td>{project}</td>"
            f'<td><span class="pill {status_class}">{_esc(item.get("mode"))} / {status}</span></td>'
            f"<td>{_esc(item.get('git_branch'))}</td>"
            f"<td>{_link(item.get('dashboard_url'))}</td>"
            f"<td>{_link(ingress.get('global_dashboard_url'))}</td>"
            f"<td>{_esc(item.get('error'))}</td>"
            f"<td>{_esc(item.get('state_dir'))}</td>"
            '<td class="row-actions">'
            f'<button data-action="start_projects" data-project="{project}">Start</button>'
            f'<button data-action="stop_projects" data-project="{project}">Stop</button>'
            f'<button class="danger" data-action="delete_projects" '
            f'data-project="{project}">Delete</button>'
            "</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td class="muted" colspan="9">No WorkerBee projects registered.</td></tr>')
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
      input[type="checkbox"] {{ accent-color: var(--k1s-brand-gold); }}
      .actions {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
      }}
      .row-actions {{ display: flex; gap: 6px; white-space: nowrap; }}
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
        </div>
        <pre id="action-result" hidden></pre>
      </section>
      <section class="card">
        <h2>Projects</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Select</th><th>Project</th><th>Mode / Status</th><th>Git Branch</th>
                <th>k1s Dashboard</th><th>Global</th><th>Error</th><th>State</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <h2>Ingress</h2>
        <pre>{_esc(ingress_json)}</pre>
      </section>
    </main>
    <script>
      const token = document.querySelector('meta[name="workerbee-action-token"]').content;
      const resultBox = document.getElementById('action-result');

      function selectedProjects() {{
        return Array.from(document.querySelectorAll('.project-select:checked'))
          .map((item) => item.value)
          .filter(Boolean);
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

      async function postAction(action, projects = []) {{
        if (!confirmation(action, projects)) return;
        resultBox.hidden = false;
        resultBox.textContent = `Running ${{action}}...`;
        try {{
          const response = await fetch('/api/actions', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{action, projects, token}})
          }});
          const payload = await response.json();
          resultBox.textContent = JSON.stringify(payload, null, 2);
          if (payload.ok && !action.startsWith('mcp_')) {{
            setTimeout(() => window.location.reload(), 750);
          }}
        }} catch (err) {{
          resultBox.textContent = `${{action}} request was interrupted (${{err.message}}).`;
          if (!action.startsWith('mcp_')) {{
            resultBox.textContent += ' Refreshing to verify current state...';
            setTimeout(() => window.location.reload(), 1000);
          }}
        }}
      }}

      document.querySelectorAll('button[data-action]').forEach((button) => {{
        button.addEventListener('click', () => {{
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
      }});
    </script>
  </body>
</html>
"""


def _dashboard_static_asset(path: str) -> tuple[bytes, str] | None:
    if path == DASHBOARD_BACKGROUND_PATH:
        filename = "page-background-1920x1080.png"
    elif path == DASHBOARD_LOGO_PATH:
        filename = "k1s-logo-32.png"
    else:
        return None
    try:
        body = files("workerbee.assets").joinpath("dashboard", filename).read_bytes()
    except FileNotFoundError:
        return None
    return body, "image/png"


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
