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

from workerbee.ingress import GlobalIngress, GlobalIngressInfo, ProjectIngressConfig
from workerbee.locks import FileLock, project_lock_path, state_root_lock_path
from workerbee.paths import daemon_project_state_dir, default_state_root
from workerbee.ports import choose_port
from workerbee.runtime_support import cleanup_runtime, resolve_runtime, runtime_diagnostics
from workerbee.supervisor import WorkerBeeSupervisor, project_slug

T = TypeVar("T")


@dataclass(slots=True)
class ProjectRecord:
    project: str
    state_dir: str
    cwd_hint: str
    created_at: float
    last_seen_at: float


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
        self._register_project(name, cwd_hint=str(self.cwd))
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
            cwd=self.cwd,
            ingress=ingress,
        )

    def with_project(
        self,
        project: str | None,
        fn: Callable[[WorkerBeeSupervisor], T],
    ) -> T:
        name = project_slug(project or self.default_project)
        with self._project_lock(name):
            file_lock = FileLock(project_lock_path(self.state_root, name), label=f"project {name}")
            with file_lock:
                sup = self.supervisor(name)
                result = fn(sup)
                self._register_project(name, cwd_hint=str(sup.cwd))
                return result

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
                    "created_at": record.get("created_at"),
                    "last_seen_at": record.get("last_seen_at"),
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
        return self.with_project(project, lambda sup: sup.status())

    def capabilities(self) -> dict[str, Any]:
        from workerbee import __version__
        from workerbee.contract import API_VERSION, MCP_TOOL_NAMES

        return {
            "api_version": API_VERSION,
            "workerbee_version": __version__,
            "state_root": str(self.state_root),
            "default_project": self.default_project,
            "mcp_tools": MCP_TOOL_NAMES,
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

    def _project_lock(self, project: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(project)
            if lock is None:
                lock = threading.RLock()
                self._locks[project] = lock
            return lock

    def _register_project(self, project: str, *, cwd_hint: str) -> None:
        now = time.time()
        records = self._read_registry()
        existing = records.get(project) or {}
        record = ProjectRecord(
            project=project,
            state_dir=str(daemon_project_state_dir(project, state_root=self.state_root)),
            cwd_hint=cwd_hint,
            created_at=float(existing.get("created_at") or now),
            last_seen_at=now,
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
            f"<td>{'running' if item.get('running') else 'stopped'}</td>"
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
          <th>Project</th><th>Status</th><th>k1s Dashboard</th>
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
