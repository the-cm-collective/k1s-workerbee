"""Multi-project WorkerBee MCP daemon state."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar

from workerbee.agent import (
    DEFAULT_PROJECT_MODE,
    derive_session_project_info,
    next_actions_for_mode,
    normalize_project_mode,
    open_browser,
    runbook_payload,
    user_message_for_session,
)
from workerbee.contract import WorkerBeeError
from workerbee.ingress import GlobalIngress, GlobalIngressInfo, ProjectIngressConfig
from workerbee.locks import FileLock, project_lock_path, state_root_lock_path
from workerbee.paths import daemon_project_state_dir, default_state_root
from workerbee.ports import choose_port
from workerbee.probe import build_probe_url, probe_workerbee_url
from workerbee.runtime_support import cleanup_runtime, resolve_runtime, runtime_diagnostics
from workerbee.supervisor import WorkerBeeSupervisor, project_slug

T = TypeVar("T")


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
        self.ingress: GlobalIngress | None = None
        self._state_lock: FileLock | None = None

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
            "runtime": runtime_diagnostics(self.runtime_requested),
        }

    def cleanup(self, *, execute: bool = False, purge_images: bool = False) -> dict[str, Any]:
        return cleanup_runtime(
            state_root=self.state_root,
            runtime=self.runtime_requested,
            execute=execute,
            purge_images=purge_images,
        )

    def global_dashboard(self) -> dict[str, Any]:
        if self.ingress is not None:
            return self.ingress.info().public_dict()
        from workerbee.ingress import load_global_ingress_info

        return load_global_ingress_info(self.state_root) or {
            "enabled": False,
            "state_root": str(self.state_root),
        }

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

    def _resolve_runtime(self) -> str:
        return resolve_runtime(self.runtime_requested)

    def _start_dashboard_server(self) -> int:
        if self._dashboard is not None:
            return int(self._dashboard.server_address[1])
        port = choose_port(18090, start=18090, end=18190)
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/projects"):
                    _send_json(self, daemon.projects())
                    return
                _send_html(self, _render_dashboard(daemon.projects()))

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


def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_html(handler: BaseHTTPRequestHandler, html: str) -> None:
    body = html.encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


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


def _render_dashboard(payload: dict[str, Any]) -> str:
    projects = payload.get("projects") if isinstance(payload.get("projects"), list) else []
    rows = []
    for item in projects:
        if not isinstance(item, dict):
            continue
        ingress = item.get("ingress") if isinstance(item.get("ingress"), dict) else {}
        rows.append(
            "<tr>"
            f"<td>{_esc(item.get('project'))}</td>"
            f"<td>{_esc(item.get('mode'))} / {'running' if item.get('running') else 'stopped'}</td>"
            f"<td>{_esc(item.get('git_branch'))}</td>"
            f"<td>{_link(item.get('dashboard_url'))}</td>"
            f"<td>{_link(ingress.get('global_dashboard_url'))}</td>"
            f"<td>{_esc(item.get('error'))}</td>"
            f"<td>{_esc(item.get('state_dir'))}</td>"
            "</tr>"
        )
    global_dash = payload.get("global_dashboard")
    ingress_json = json.dumps(global_dash, indent=2, sort_keys=True)
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>WorkerBee Projects</title>
    <style>
      body {{ font-family: ui-sans-serif, system-ui; margin: 2rem; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border-bottom: 1px solid #ddd; padding: .5rem; text-align: left; }}
      code, pre {{ background: #f6f6f6; padding: .25rem; }}
    </style>
  </head>
  <body>
    <h1>WorkerBee Projects</h1>
    <table>
      <thead>
        <tr>
          <th>Project</th><th>Mode / Status</th><th>Git Branch</th><th>k1s Dashboard</th>
          <th>Global</th><th>Error</th><th>State</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <h2>Ingress</h2>
    <pre>{_esc(ingress_json)}</pre>
  </body>
</html>
"""


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
