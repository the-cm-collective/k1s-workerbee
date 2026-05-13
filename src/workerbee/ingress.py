"""Global local HTTPS ingress for WorkerBee MCP mode."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from workerbee.dns import DEFAULT_DNS_DOMAIN, DNSSettings, resolve_dns_settings
from workerbee.http import request, request_https_via_loopback, wait_for_http
from workerbee.ports import choose_port
from workerbee.runtime_support import (
    CONTAINERD_RUNTIME,
    resolve_runtime,
    runtime_command_args,
    workerbee_runtime_labels,
)

INGRESS_EXPOSURE_LOOPBACK = "loopback"
INGRESS_EXPOSURE_LAN = "lan"
VALID_INGRESS_EXPOSURES = {INGRESS_EXPOSURE_LOOPBACK, INGRESS_EXPOSURE_LAN}
DEFAULT_INGRESS_DOMAIN = "workerbee.localhost"
DEFAULT_INGRESS_CA_HTTP_PORT = 19080
INGRESS_BIND_LOOPBACK = "127.0.0.1"
INGRESS_BIND_LAN = "0.0.0.0"  # noqa: S104 - explicit LAN ingress bind address


@dataclass(frozen=True, slots=True)
class IngressSettings:
    exposure: str = INGRESS_EXPOSURE_LOOPBACK
    base_domain: str = DEFAULT_INGRESS_DOMAIN
    bind_host: str = INGRESS_BIND_LOOPBACK
    ca_http_port: int = DEFAULT_INGRESS_CA_HTTP_PORT
    dns: DNSSettings = field(default_factory=DNSSettings)


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
    dashboard_port: int = 0
    exposure: str = INGRESS_EXPOSURE_LOOPBACK
    base_domain: str = DEFAULT_INGRESS_DOMAIN
    bind_host: str = INGRESS_BIND_LOOPBACK
    ca_download_url: str | None = None
    ca_http_port: int = DEFAULT_INGRESS_CA_HTTP_PORT
    dns: dict[str, Any] = field(default_factory=dict)

    def url(self, host: str, path: str = "/") -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        return f"https://{host}:{self.https_port}{normalized}"

    def host(self, prefix: str | None = None) -> str:
        return f"{prefix}.{self.domain}" if prefix else self.domain

    def public_dict(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "exposure": self.exposure,
            "base_domain": self.base_domain,
            "domain": self.domain,
            "bind_host": self.bind_host,
            "https_port": self.https_port,
            "caddy_container": self.caddy_container,
            "caddy_sites": str(self.sites_dir),
            "ca_bundle": str(self.ca_bundle),
            "ca_download_url": self.ca_download_url,
            "ca_http_port": self.ca_http_port,
            "dns": dict(self.dns),
            "global_dashboard_url": self.global_dashboard_url,
            "dashboard_port": self.dashboard_port,
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
    exposure: str = INGRESS_EXPOSURE_LOOPBACK
    base_domain: str = DEFAULT_INGRESS_DOMAIN
    bind_host: str = INGRESS_BIND_LOOPBACK
    dashboard_dns_ok: bool = True
    ca_download_url: str | None = None
    dashboard_ca_download_url: str | None = None
    dashboard_ca_sha256_url: str | None = None
    ca_sha256: str | None = None
    ca_http_port: int = DEFAULT_INGRESS_CA_HTTP_PORT
    dns: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        ca = Path(self.ca_bundle)
        ca_ready = _safe_is_file(ca)
        data["ca_ready"] = ca_ready
        data["ca_sha256"] = _safe_sha256(ca) if ca_ready else None
        data["ca_commands"] = ca_command_guidance(data) if ca_ready else {}
        return data


def resolve_ingress_settings(
    *,
    exposure: str | None = None,
    base_domain: str | None = None,
    bind_host: str | None = None,
    ca_http_port: int | str | None = None,
    dns_mode: str | None = None,
    dns_port: int | str | None = None,
    dns_bind: str | None = None,
    dns_answer: str | None = None,
    dns_upstreams: list[str] | tuple[str, ...] | str | None = None,
) -> IngressSettings:
    selected_exposure = (
        exposure or os.getenv("WORKERBEE_INGRESS_EXPOSURE") or INGRESS_EXPOSURE_LOOPBACK
    )
    selected_exposure = selected_exposure.strip().lower()
    if selected_exposure not in VALID_INGRESS_EXPOSURES:
        expected = ", ".join(sorted(VALID_INGRESS_EXPOSURES))
        raise ValueError(
            f"invalid WorkerBee ingress exposure: {selected_exposure}; expected {expected}"
        )
    domain_raw = base_domain
    if domain_raw is None:
        domain_raw = os.getenv("WORKERBEE_INGRESS_DOMAIN")
    if domain_raw in (None, ""):
        domain_raw = (
            DEFAULT_DNS_DOMAIN
            if selected_exposure == INGRESS_EXPOSURE_LAN and _dns_enabled(dns_mode)
            else _default_lan_domain()
            if selected_exposure == INGRESS_EXPOSURE_LAN
            else DEFAULT_INGRESS_DOMAIN
        )
    selected_domain = _normalize_domain(str(domain_raw))
    if selected_exposure == INGRESS_EXPOSURE_LAN and (
        selected_domain == "localhost" or selected_domain.endswith(".localhost")
    ):
        raise ValueError("LAN ingress requires a non-.localhost domain")
    selected_bind = bind_host
    if selected_bind is None:
        selected_bind = os.getenv("WORKERBEE_INGRESS_BIND")
    if selected_bind in (None, ""):
        selected_bind = (
            INGRESS_BIND_LAN
            if selected_exposure == INGRESS_EXPOSURE_LAN
            else INGRESS_BIND_LOOPBACK
        )
    selected_bind = str(selected_bind).strip()
    if not selected_bind:
        raise ValueError("WorkerBee ingress bind address cannot be empty")
    selected_port = ca_http_port
    if selected_port is None:
        selected_port = _env_int("WORKERBEE_INGRESS_CA_PORT")
    port = int(selected_port or DEFAULT_INGRESS_CA_HTTP_PORT)
    if port < 1 or port > 65535:
        raise ValueError(f"invalid WorkerBee ingress CA HTTP port: {port}")
    dns = resolve_dns_settings(
        exposure=selected_exposure,
        bind_host=selected_bind,
        mode=dns_mode,
        port=dns_port,
        dns_bind=dns_bind,
        answer=dns_answer,
        upstreams=dns_upstreams,
    )
    return IngressSettings(
        exposure=selected_exposure,
        base_domain=selected_domain,
        bind_host=selected_bind,
        ca_http_port=port,
        dns=dns,
    )


class GlobalIngress:
    def __init__(
        self,
        *,
        state_root: Path,
        runtime: str,
        https_port: int | None = None,
        dashboard_port: int | None = None,
        ingress_settings: IngressSettings | None = None,
        dns_status: dict[str, Any] | None = None,
    ) -> None:
        self.state_root = state_root.resolve()
        self.runtime = runtime
        self.ingress_settings = ingress_settings or resolve_ingress_settings()
        self.exposure = self.ingress_settings.exposure
        self.base_domain = self.ingress_settings.base_domain
        self.bind_host = self.ingress_settings.bind_host
        self.ca_http_port = self.ingress_settings.ca_http_port
        self.dns_status = dns_status
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
        self.dashboard_host = f"dashboard.{self.base_domain}"
        self.ca_host = f"ca.{self.base_domain}"
        self.dashboard_url = f"https://{self.dashboard_host}:{self.https_port}/"
        self.dashboard_ca_download_url = _dashboard_ca_download_url(self.dashboard_url)
        self.dashboard_ca_sha256_url = _dashboard_ca_sha256_url(self.dashboard_url)
        self.ca_download_url = (
            f"http://{self.ca_host}:{self.ca_http_port}/workerbee-ca.crt"
            if self.exposure == INGRESS_EXPOSURE_LAN
            else None
        )
        self.container = existing.get("caddy_container") or _container_name(self.state_root)
        if self.runtime == CONTAINERD_RUNTIME:
            self.host_alias = INGRESS_BIND_LOOPBACK
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
        verification = self._verified_dashboard_health_probe()
        if not verification.get("ok"):
            recovery = self._recover_caddy_ca_mismatch(verification)
            if not recovery.get("ok"):
                raise RuntimeError(_caddy_ca_verification_error(recovery))
        info = self.info()
        self.info_file.write_text(json.dumps(info.public_dict(), indent=2), encoding="utf-8")
        return info

    def sync_projects(self, projects: list[str]) -> None:
        self._write_caddyfile(projects)
        self.reload()

    def reload(self) -> dict[str, Any]:
        if not self._container_running():
            return {"ok": False, "container": self.container, "reason": "container is not running"}
        proc = subprocess.run(
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
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return {
            "ok": proc.returncode == 0,
            "container": self.container,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
        }

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
        domain = f"{project}.{self.base_domain}"
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
            dashboard_port=self.dashboard_port,
            exposure=self.exposure,
            base_domain=self.base_domain,
            bind_host=self.bind_host,
            ca_download_url=self.ca_download_url,
            ca_http_port=self.ca_http_port,
            dns=self._dns_public_dict(),
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
            localhost_dns_ok=_dns_ok("dashboard.workerbee.localhost"),
            runtime=self.runtime,
            exposure=self.exposure,
            base_domain=self.base_domain,
            bind_host=self.bind_host,
            dashboard_dns_ok=_dns_ok(self.dashboard_host),
            ca_download_url=self.ca_download_url,
            dashboard_ca_download_url=self.dashboard_ca_download_url,
            dashboard_ca_sha256_url=self.dashboard_ca_sha256_url,
            ca_sha256=_sha256(self.ca_bundle) if self.ca_bundle.is_file() else None,
            ca_http_port=self.ca_http_port,
            dns=self._dns_public_dict(),
        )

    def _dns_public_dict(self) -> dict[str, Any]:
        if self.dns_status is not None:
            return dict(self.dns_status)
        data = self.ingress_settings.dns.public_dict()
        if self.ingress_settings.dns.enabled:
            data["base_domain"] = self.base_domain
        return data

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
        containerd_bind = (
            f"default_bind {self.bind_host}" if self.runtime == CONTAINERD_RUNTIME else ""
        )
        ca_site = ""
        if self.exposure == INGRESS_EXPOSURE_LAN:
            ca_site = f"""
http://{self.ca_host}:{self.ca_http_port} {{
    @workerbee_ca path /workerbee-ca.crt /workerbee-ca.sha256
    handle @workerbee_ca {{
        reverse_proxy {self.host_alias}:{self.dashboard_port}
    }}
    respond "not found\\n" 404
}}
"""
        self.caddy_file.write_text(
            f"""# Generated by WorkerBee. Do not edit while WorkerBee MCP is running.
{{
    auto_https disable_redirects
    {f"https_port {self.https_port}" if self.runtime == CONTAINERD_RUNTIME else ""}
    {containerd_bind}
}}

https://{self.dashboard_host} {{
    log {{
        output stdout
        format console
    }}
    header -Strict-Transport-Security
    tls internal
    reverse_proxy {self.host_alias}:{self.dashboard_port}
}}

{ca_site}
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
            if self.runtime == "podman" and _podman_is_rootless():
                run_args.extend(["--network", "slirp4netns:allow_host_loopback=true"])
            run_args.extend(["-p", f"{self.bind_host}:{self.https_port}:443"])
            if self.exposure == INGRESS_EXPOSURE_LAN:
                run_args.extend(["-p", f"{self.bind_host}:{self.ca_http_port}:{self.ca_http_port}"])
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
        if self.dashboard_port:
            wait_for_http(
                f"http://127.0.0.1:{self.dashboard_port}/healthz",
                timeout_seconds=10,
                interval_seconds=0.2,
                verify_tls=False,
                ok_statuses={200},
            )
        probe_host = _bind_probe_host(self.bind_host)
        _wait_for_tcp(probe_host, self.https_port, timeout_seconds=20)
        if self.exposure == INGRESS_EXPOSURE_LAN:
            _wait_for_tcp(probe_host, self.ca_http_port, timeout_seconds=20)

    def _export_ca_bundle(self) -> None:
        deadline = time.monotonic() + 10.0
        last_output = ""
        while True:
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
            if proc.returncode == 0 and proc.stdout.strip():
                break
            last_output = (proc.stderr or proc.stdout or "").strip()
            if time.monotonic() >= deadline:
                raise RuntimeError(f"failed to export WorkerBee Caddy CA:\n{last_output}")
            time.sleep(0.2)
        tmp = self.ca_bundle.with_suffix(".tmp")
        tmp.write_text(proc.stdout, encoding="utf-8")
        tmp.chmod(0o644)
        tmp.replace(self.ca_bundle)

    def _verified_dashboard_health_probe(self) -> dict[str, Any]:
        return _global_dashboard_health_probe(self.info().public_dict())

    def recover_caddy_tls_state(self, initial_probe: dict[str, Any]) -> dict[str, Any]:
        recovery = self._recover_caddy_ca_mismatch(initial_probe)
        if recovery.get("ok"):
            self.info_file.write_text(
                json.dumps(self.info().public_dict(), indent=2),
                encoding="utf-8",
            )
        return recovery

    def _recover_caddy_ca_mismatch(self, initial_probe: dict[str, Any]) -> dict[str, Any]:
        recovery: dict[str, Any] = {
            "ok": False,
            "reason": "caddy CA verification failed",
            "container": self.container,
            "https_port": self.https_port,
            "ca_bundle": str(self.ca_bundle),
            "initial_probe": initial_probe,
        }
        try:
            stop_result = self.stop()
            recovery["stop"] = stop_result
            if not stop_result.get("ok"):
                raise RuntimeError(
                    "failed to stop WorkerBee Caddy before CA recovery: "
                    f"{stop_result}"
                )
            recovery["purge"] = self._purge_caddy_state()
            self.caddy_data.mkdir(parents=True, exist_ok=True)
            self._ensure_caddy_container()
            self._wait_ready()
            self._export_ca_bundle()
            verification = self._verified_dashboard_health_probe()
            recovery["verification"] = verification
            recovery["ok"] = bool(verification.get("ok"))
            return recovery
        except Exception as exc:  # noqa: BLE001 - include recovery details in startup error
            recovery["error"] = str(exc)
            return recovery

    def _purge_caddy_state(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "caddy_data": str(self.caddy_data),
            "ca_bundle": str(self.ca_bundle),
            "removed_caddy_data": False,
            "removed_ca_bundle": False,
        }
        if self.ca_bundle.exists() or self.ca_bundle.is_symlink():
            self.ca_bundle.unlink()
            result["removed_ca_bundle"] = True
        if not self.caddy_data.exists() and not self.caddy_data.is_symlink():
            return result
        try:
            if self.caddy_data.is_dir() and not self.caddy_data.is_symlink():
                shutil.rmtree(self.caddy_data)
            else:
                self.caddy_data.unlink()
            result["removed_caddy_data"] = True
            return result
        except Exception as exc:  # noqa: BLE001 - try privileged helper for root-owned files
            result["direct_error"] = str(exc)
            if self.runtime != CONTAINERD_RUNTIME:
                raise
        from workerbee.containerd_helper import remove_containerd_helper_tree

        helper = remove_containerd_helper_tree(self.state_root, self.caddy_data)
        result["helper"] = helper
        if not helper.get("ok"):
            raise RuntimeError(
                "failed to purge WorkerBee Caddy data after CA verification failure: "
                f"{helper}"
            )
        result["removed_caddy_data"] = bool(helper.get("removed"))
        return result

    def _container_running(self) -> bool:
        return _caddy_container_running(self.state_root, self.runtime, self.container)


def _wait_for_tcp(host: str, port: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    raise TimeoutError(f"{host}:{port} did not accept TCP connections: {last_error}")


def load_global_ingress_info(state_root: Path) -> dict[str, Any] | None:
    info_file = state_root.resolve() / "global" / "ingress.json"
    if not info_file.is_file():
        return None
    try:
        data = json.loads(info_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def ca_command_guidance(info: dict[str, Any]) -> dict[str, str]:
    commands = {
        "export": "workerbee ingress ca --output workerbee-ca.crt",
        "trust_system": "workerbee trust install --target system",
        "trust_nss": "workerbee trust install --target nss",
    }
    ca_download_url = str(info.get("ca_download_url") or "").strip()
    if ca_download_url:
        commands["download_curl"] = f"curl -fsSL {shlex.quote(ca_download_url)} -o workerbee-ca.crt"
    return commands


def _dashboard_ca_download_url(dashboard_url: str | None) -> str | None:
    if not dashboard_url:
        return None
    return f"{str(dashboard_url).rstrip('/')}/workerbee-ca.crt"


def _dashboard_ca_sha256_url(dashboard_url: str | None) -> str | None:
    if not dashboard_url:
        return None
    return f"{str(dashboard_url).rstrip('/')}/workerbee-ca.sha256"


def export_global_ingress_ca(
    state_root: Path,
    *,
    output: Path,
    runtime: str = "auto",
) -> dict[str, Any]:
    root = state_root.resolve()
    status = global_ingress_status(root, runtime=runtime)
    ca_raw = str(status.get("ca_bundle") or "")
    ca = Path(ca_raw) if ca_raw else None
    if not ca:
        raise FileNotFoundError(
            "WorkerBee Caddy CA is not ready; start WorkerBee first with "
            "`workerbee mcp start`."
        )
    if not _safe_is_file(ca):
        raise FileNotFoundError(
            f"WorkerBee Caddy CA is not ready at {ca}; restart WorkerBee MCP or wait for "
            "`workerbee mcp status` to show ca_ready."
        )
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ca, destination)
    with suppress(OSError):
        destination.chmod(0o644)
    ca_sha256 = _safe_sha256(ca)
    return {
        "ok": True,
        "ca_export": True,
        "state_root": str(root),
        "ca_bundle": str(ca),
        "output": str(destination),
        "ca_ready": True,
        "ca_sha256": ca_sha256,
        "ca_download_url": status.get("ca_download_url"),
        "dashboard_ca_download_url": status.get("dashboard_ca_download_url"),
        "dashboard_ca_sha256_url": status.get("dashboard_ca_sha256_url"),
        "ca_commands": ca_command_guidance(status),
    }


def global_ingress_status(state_root: Path, *, runtime: str = "auto") -> dict[str, Any]:
    root = state_root.resolve()
    info = load_global_ingress_info(root)
    if not info:
        return {
            "enabled": False,
            "running": False,
            "stale": False,
            "state_root": str(root),
        }
    selected = str(info.get("runtime") or "")
    if not selected:
        selected = resolve_runtime(runtime)
    container = str(info.get("caddy_container") or "")
    runtime_probe: dict[str, Any] = {"running": False, "skipped": not bool(container)}
    if container:
        if selected == CONTAINERD_RUNTIME:
            from workerbee.containerd_helper import (
                containerd_privilege_env,
                containerd_privilege_status,
                temporary_containerd_privilege_env,
            )

            privilege = containerd_privilege_status(state_root=root, runtime=selected)
            with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
                runtime_probe = _caddy_container_probe(root, selected, container)
        else:
            runtime_probe = _caddy_container_probe(root, selected, container)
    health_probe = _global_dashboard_health_probe(info)
    runtime_running = bool(runtime_probe.get("running"))
    health_running = bool(health_probe.get("ok"))
    running = runtime_running or health_running
    probe_error = runtime_probe.get("error") if not runtime_running else None
    ca = Path(str(info.get("ca_bundle") or ""))
    ca_ready = _safe_is_file(ca)
    return {
        **info,
        "enabled": bool(running),
        "running": bool(running),
        "stale": not bool(running),
        "ca_ready": ca_ready,
        "ca_sha256": _safe_sha256(ca) if ca_ready else None,
        "dashboard_ca_download_url": info.get("dashboard_ca_download_url")
        or _dashboard_ca_download_url(str(info.get("dashboard_url") or "")),
        "dashboard_ca_sha256_url": info.get("dashboard_ca_sha256_url")
        or _dashboard_ca_sha256_url(str(info.get("dashboard_url") or "")),
        "ca_commands": ca_command_guidance(info) if ca_ready else {},
        "runtime_running": runtime_running,
        "https_running": health_running,
        "runtime_probe": runtime_probe,
        "health_probe": health_probe,
        "probe_error": probe_error,
    }


def _caddy_container_running(state_root: Path, runtime: str, container: str) -> bool:
    return bool(_caddy_container_probe(state_root, runtime, container).get("running"))


def _caddy_container_probe(state_root: Path, runtime: str, container: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            runtime_command_args(
                runtime,
                state_root=state_root,
                project=None,
                system=True,
                args=["ps", "-q", "--filter", f"name=^{container}$"],
            ),
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - dashboard status should degrade, not crash
        return {
            "running": False,
            "method": "id-filter",
            "error": str(exc),
        }
    if proc.returncode == 0 and bool(proc.stdout.strip()):
        return {
            "running": True,
            "method": "id-filter",
            "returncode": proc.returncode,
        }
    fallback = _caddy_container_name_probe(state_root, runtime, container)
    if fallback.get("running"):
        return fallback
    error = fallback.get("error")
    if proc.returncode != 0 and not error:
        error = (proc.stderr or proc.stdout or "").strip() or f"runtime ps exited {proc.returncode}"
    return {
        **fallback,
        "primary_returncode": proc.returncode,
        "error": error,
    }


def _caddy_container_name_running(state_root: Path, runtime: str, container: str) -> bool:
    return bool(_caddy_container_name_probe(state_root, runtime, container).get("running"))


def _caddy_container_name_probe(state_root: Path, runtime: str, container: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            runtime_command_args(
                runtime,
                state_root=state_root,
                project=None,
                system=True,
                args=["ps", "--format", "{{.Names}}", "--filter", f"name={container}"],
            ),
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - dashboard status should degrade, not crash
        return {
            "running": False,
            "method": "name-filter",
            "error": str(exc),
        }
    names = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    running = proc.returncode == 0 and container in names
    return {
        "running": running,
        "method": "name-filter",
        "returncode": proc.returncode,
        "matched_names": sorted(names),
        "error": None
        if proc.returncode == 0
        else (proc.stderr or proc.stdout or "").strip() or f"runtime ps exited {proc.returncode}",
    }


def _global_dashboard_health_probe(info: dict[str, Any]) -> dict[str, Any]:
    raw_url = str(info.get("dashboard_url") or "").strip()
    if not raw_url or not info.get("https_port"):
        return {
            "ok": False,
            "skipped": True,
            "reason": "dashboard URL or HTTPS port is not recorded",
        }
    parsed = urlsplit(raw_url)
    health_url = urlunsplit((parsed.scheme, parsed.netloc, "/healthz", "", ""))
    ca_bundle = Path(str(info.get("ca_bundle") or ""))
    ca_ready = _safe_is_file(ca_bundle)
    try:
        result = request(
            health_url,
            timeout=1.0,
            ca_bundle=ca_bundle if ca_ready else None,
            verify_tls=ca_ready,
        )
        if result.status == 200:
            return {
                "ok": True,
                "url": health_url,
                "method": "direct",
                "status": result.status,
                "tls_verified": ca_ready,
                "ca_ready": ca_ready,
            }
        primary_error = None
        primary_status = result.status
    except Exception as exc:  # noqa: BLE001 - status probe only
        primary_error = str(exc)
        primary_status = None
    fallback = _loopback_dashboard_health_probe(
        parsed,
        ca_ready=ca_ready,
        ca_bundle=ca_bundle if ca_ready else None,
        connect_host=_bind_probe_host(str(info.get("bind_host") or INGRESS_BIND_LOOPBACK)),
    )
    if fallback is not None:
        fallback["primary_url"] = health_url
        if primary_error is not None:
            fallback["primary_error"] = primary_error
        if primary_status is not None:
            fallback["primary_status"] = primary_status
        return fallback
    return {
        "ok": False,
        "url": health_url,
        "error": primary_error,
        "status": primary_status,
        "tls_verified": False,
        "ca_ready": ca_ready,
    }


def _loopback_dashboard_health_probe(
    parsed: SplitResult,
    *,
    ca_ready: bool,
    ca_bundle: Path | None = None,
    connect_host: str = INGRESS_BIND_LOOPBACK,
) -> dict[str, Any] | None:
    if parsed.scheme != "https" or not parsed.port:
        return None
    health_url = urlunsplit(
        (parsed.scheme, _host_port(connect_host, parsed.port), "/healthz", "", "")
    )
    try:
        result = request_https_via_loopback(
            health_url,
            timeout=1.0,
            server_hostname=parsed.hostname or "dashboard.workerbee.localhost",
            host_header=parsed.netloc,
            verify_tls=ca_ready,
            ca_bundle=ca_bundle if ca_ready else None,
        )
    except Exception as exc:  # noqa: BLE001 - status probe only
        return {
            "ok": False,
            "url": health_url,
            "host_header": parsed.netloc,
            "method": "loopback-host-header",
            "ca_ready": ca_ready,
            "tls_verified": False,
            "error": str(exc),
        }
    return {
        "ok": result.status == 200,
        "url": health_url,
        "host_header": parsed.netloc,
        "method": "loopback-host-header",
        "ca_ready": ca_ready,
        "tls_verified": bool(ca_ready and result.status == 200),
        "status": result.status,
    }


def _caddy_ca_verification_error(recovery: dict[str, Any]) -> str:
    details = json.dumps(recovery, indent=2, sort_keys=True, default=str)
    return (
        "WorkerBee Caddy did not serve a certificate trusted by its exported CA "
        "after one recovery attempt.\n"
        f"{details}"
    )


def _container_name(state_root: Path) -> str:
    digest = hashlib.sha1(str(state_root).encode("utf-8")).hexdigest()[:10]  # noqa: S324
    return f"workerbee-caddy-{digest}"


def _normalize_domain(raw: str) -> str:
    domain = raw.strip().strip(".").lower()
    if not domain:
        raise ValueError("WorkerBee ingress domain cannot be empty")
    if "://" in domain or "/" in domain or ":" in domain or any(ch.isspace() for ch in domain):
        raise ValueError(f"invalid WorkerBee ingress domain: {raw}")
    return domain


def _default_lan_domain() -> str:
    lan_ip = _detect_lan_ip()
    if not lan_ip:
        raise ValueError(
            "could not determine a LAN IP for WorkerBee ingress; pass --ingress-domain"
        )
    return f"{lan_ip.replace('.', '-')}.sslip.io"


def _dns_enabled(mode: str | None) -> bool:
    selected = mode
    if selected is None:
        selected = os.getenv("WORKERBEE_INGRESS_DNS")
    selected = str(selected or "").strip().lower()
    return selected in {"1", "true", "yes", "on", "enable", "enabled", "forwarding"}


def _detect_lan_ip() -> str | None:
    candidates: list[str] = []
    with suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        candidates.append(str(sock.getsockname()[0]))
    with suppress(OSError):
        for family, _type, _proto, _canon, address in socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        ):
            if family == socket.AF_INET and address:
                candidates.append(str(address[0]))
    for candidate in candidates:
        with suppress(ValueError):
            parsed = ip_address(candidate)
            if parsed.version == 4 and not parsed.is_loopback and not parsed.is_unspecified:
                return candidate
    return None


def _bind_probe_host(bind_host: str) -> str:
    if bind_host in {"", INGRESS_BIND_LAN}:
        return INGRESS_BIND_LOOPBACK
    if bind_host == "::":
        return "::1"
    return bind_host


def _host_port(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _dns_ok(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def _missing_container(output: str) -> bool:
    lowered = output.lower()
    return "no such container" in lowered or "not found" in lowered


def _podman_is_rootless() -> bool:
    try:
        proc = subprocess.run(
            ["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        return False
    return proc.stdout.strip().lower() == "true"


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_sha256(path: Path) -> str | None:
    try:
        return _sha256(path)
    except OSError:
        return None


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False
