"""Global local HTTPS ingress for WorkerBee MCP mode."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from workerbee.http import wait_for_http
from workerbee.ports import choose_port
from workerbee.runtime_support import (
    CONTAINERD_RUNTIME,
    runtime_command_args,
    workerbee_runtime_labels,
)


@dataclass(frozen=True, slots=True)
class ProjectIngressConfig:
    project: str
    domain: str
    https_port: int
    sites_dir: Path
    caddy_container: str
    caddy_file: str
    host_alias: str
    ca_bundle: Path
    global_dashboard_url: str

    def url(self, host: str, path: str = "/") -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        return f"https://{host}:{self.https_port}{normalized}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "domain": self.domain,
            "https_port": self.https_port,
            "caddy_container": self.caddy_container,
            "caddy_sites": str(self.sites_dir),
            "ca_bundle": str(self.ca_bundle),
            "global_dashboard_url": self.global_dashboard_url,
        }


@dataclass(slots=True)
class GlobalIngressInfo:
    enabled: bool
    state_root: str
    https_port: int
    dashboard_port: int
    dashboard_url: str
    caddy_container: str
    caddy_data: str
    ca_bundle: str
    localhost_dns_ok: bool
    runtime: str

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ca_ready"] = _safe_is_file(Path(self.ca_bundle))
        return data


class GlobalIngress:
    def __init__(
        self,
        *,
        state_root: Path,
        runtime: str,
        https_port: int | None = None,
        dashboard_port: int | None = None,
    ) -> None:
        self.state_root = state_root.resolve()
        self.runtime = runtime
        self.global_dir = self.state_root / "global"
        self.projects_dir = self.state_root / "projects"
        self.caddy_data = self.global_dir / "caddy-data"
        self.caddy_ca_bundle = self.global_dir / "caddy-local-root.crt"
        self.caddy_file = self.global_dir / "Caddyfile"
        self.info_file = self.global_dir / "ingress.json"
        existing = self._load_existing()
        self.https_port = https_port or _env_int("WORKERBEE_INGRESS_PORT")
        if self.https_port is None:
            self.https_port = int(existing.get("https_port") or 0) or choose_port(
                19443,
                start=19443,
                end=19543,
            )
        self.dashboard_port = dashboard_port or int(existing.get("dashboard_port") or 0)
        self.dashboard_url = f"https://dashboard.workerbee.localhost:{self.https_port}/"
        self.container = existing.get("caddy_container") or _container_name(self.state_root)
        if self.runtime == CONTAINERD_RUNTIME:
            self.host_alias = "127.0.0.1"
        else:
            self.host_alias = (
                "host.containers.internal" if self.runtime == "podman" else "host.docker.internal"
            )

    def start(self, *, projects: list[str] | None = None) -> GlobalIngressInfo:
        self.global_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.caddy_data.mkdir(parents=True, exist_ok=True)
        self._write_caddyfile(projects or [])
        self._ensure_caddy_container()
        self._wait_ready()
        self._export_ca_bundle()
        info = self.info()
        self.info_file.write_text(json.dumps(info.public_dict(), indent=2), encoding="utf-8")
        return info

    def sync_projects(self, projects: list[str]) -> None:
        self._write_caddyfile(projects)
        self.reload()

    def reload(self) -> None:
        if not self._container_running():
            return
        subprocess.run(
            runtime_command_args(
                self.runtime,
                state_root=self.state_root,
                project=None,
                system=True,
                args=[
                    "exec",
                    self.container,
                    "caddy",
                    "reload",
                    "--config",
                    "/etc/caddy/Caddyfile",
                ],
            ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> dict[str, Any]:
        proc = subprocess.run(
            runtime_command_args(
                self.runtime,
                state_root=self.state_root,
                project=None,
                system=True,
                args=["rm", "-f", self.container],
            ),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        ok = proc.returncode == 0 or _missing_container(proc.stdout)
        if ok:
            with suppress(FileNotFoundError):
                self.info_file.unlink()
        return {
            "ok": ok,
            "container": self.container,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
        }

    def project_config(self, project: str) -> ProjectIngressConfig:
        domain = f"{project}.workerbee.localhost"
        sites_dir = self.projects_dir / project / "caddy"
        sites_dir.mkdir(parents=True, exist_ok=True)
        return ProjectIngressConfig(
            project=project,
            domain=domain,
            https_port=self.https_port,
            sites_dir=sites_dir,
            caddy_container=self.container,
            caddy_file="/etc/caddy/Caddyfile",
            host_alias=self.host_alias,
            ca_bundle=self.ca_bundle,
            global_dashboard_url=self.dashboard_url,
        )

    @property
    def ca_bundle(self) -> Path:
        return self.caddy_ca_bundle

    @property
    def _container_ca_bundle(self) -> str:
        return "/data/caddy/pki/authorities/local/root.crt"

    def info(self) -> GlobalIngressInfo:
        return GlobalIngressInfo(
            enabled=True,
            state_root=str(self.state_root),
            https_port=self.https_port,
            dashboard_port=self.dashboard_port,
            dashboard_url=self.dashboard_url,
            caddy_container=self.container,
            caddy_data=str(self.caddy_data),
            ca_bundle=str(self.ca_bundle),
            localhost_dns_ok=_localhost_dns_ok(),
            runtime=self.runtime,
        )

    def _load_existing(self) -> dict[str, Any]:
        if not self.info_file.is_file():
            return {}
        try:
            data = json.loads(self.info_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_caddyfile(self, projects: list[str]) -> None:
        imports = "\n".join(
            f"import /etc/caddy/projects/{project}/caddy/*.caddy"
            for project in sorted(set(projects))
        )
        self.caddy_file.write_text(
            f"""# Generated by WorkerBee. Do not edit while WorkerBee MCP is running.
{{
    auto_https disable_redirects
    {f"https_port {self.https_port}" if self.runtime == CONTAINERD_RUNTIME else ""}
    {"default_bind 127.0.0.1" if self.runtime == CONTAINERD_RUNTIME else ""}
}}

https://dashboard.workerbee.localhost {{
    log {{
        output stdout
        format console
    }}
    header -Strict-Transport-Security
    tls internal
    reverse_proxy {self.host_alias}:{self.dashboard_port}
}}

{imports}
""",
            encoding="utf-8",
        )

    def _ensure_caddy_container(self) -> None:
        if self._container_running():
            self.reload()
            return
        subprocess.run(
            runtime_command_args(
                self.runtime,
                state_root=self.state_root,
                project=None,
                system=True,
                args=["rm", "-f", self.container],
            ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        run_args = [
            "run",
            "-d",
            "--name",
            self.container,
            "--label",
            "workerbee.component=ingress",
        ]
        if self.runtime == CONTAINERD_RUNTIME:
            run_args.extend(["--net", "host"])
        else:
            run_args.extend(["-p", f"127.0.0.1:{self.https_port}:443"])
        run_args.extend(
            [
                "-v",
                f"{self.caddy_file}:/etc/caddy/Caddyfile:ro",
                "-v",
                f"{self.projects_dir}:/etc/caddy/projects:ro",
                "-v",
                f"{self.caddy_data}:/data",
            ]
        )
        for label in workerbee_runtime_labels(state_root=self.state_root):
            run_args.extend(["--label", label])
        if self.runtime == "docker":
            run_args.extend(["--add-host", "host.docker.internal:host-gateway"])
        run_args.extend(
            [
                os.getenv("WORKERBEE_CADDY_IMAGE", "docker.io/library/caddy:2.8"),
                "caddy",
                "run",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
            ]
        )
        cmd = runtime_command_args(
            self.runtime,
            state_root=self.state_root,
            project=None,
            system=True,
            args=run_args,
        )
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"failed to start WorkerBee Caddy:\n{proc.stdout}")

    def _wait_ready(self) -> None:
        wait_for_http(
            self.dashboard_url,
            timeout_seconds=20,
            interval_seconds=0.5,
            verify_tls=False,
            ok_statuses={200},
        )

    def _export_ca_bundle(self) -> None:
        proc = subprocess.run(
            runtime_command_args(
                self.runtime,
                state_root=self.state_root,
                project=None,
                system=True,
                args=["exec", self.container, "cat", self._container_ca_bundle],
            ),
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(f"failed to export WorkerBee Caddy CA:\n{proc.stderr}")
        tmp = self.ca_bundle.with_suffix(".tmp")
        tmp.write_text(proc.stdout, encoding="utf-8")
        tmp.chmod(0o644)
        tmp.replace(self.ca_bundle)

    def _container_running(self) -> bool:
        proc = subprocess.run(
            runtime_command_args(
                self.runtime,
                state_root=self.state_root,
                project=None,
                system=True,
                args=["ps", "-q", "--filter", f"name=^{self.container}$"],
            ),
            text=True,
            capture_output=True,
            timeout=10,
        )
        return bool(proc.stdout.strip())


def load_global_ingress_info(state_root: Path) -> dict[str, Any] | None:
    info_file = state_root.resolve() / "global" / "ingress.json"
    if not info_file.is_file():
        return None
    try:
        data = json.loads(info_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _container_name(state_root: Path) -> str:
    digest = hashlib.sha1(str(state_root).encode("utf-8")).hexdigest()[:10]  # noqa: S324
    return f"workerbee-caddy-{digest}"


def _localhost_dns_ok() -> bool:
    try:
        socket.getaddrinfo("dashboard.workerbee.localhost", 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def _missing_container(output: str) -> bool:
    lowered = output.lower()
    return "no such container" in lowered or "not found" in lowered


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False
