"""Containerized k1s profile runner for advanced WorkerBee validation."""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import textwrap
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib import request as urllib_request

from workerbee.containerd_helper import remove_containerd_helper_tree
from workerbee.contract import WorkerBeeError
from workerbee.http import request, wait_for_http
from workerbee.ingress import ProjectIngressConfig
from workerbee.paths import resolve_k1s_root
from workerbee.ports import port_is_free
from workerbee.runtime_support import (
    CONTAINERD_RUNTIME,
    containerd_address,
    containerd_cni_bin_dir,
    containerd_cni_conf_dir,
    containerd_data_root,
    containerd_namespace,
    containerd_network_name,
    containerd_network_subnet,
    resolve_runtime,
    runtime_command_args,
    workerbee_runtime_labels,
)
from workerbee.supervisor import project_slug

DEFAULT_K1S_PYTHON_IMAGE = "docker.io/library/python:3.12-slim"
DEFAULT_ETCD_IMAGE = "quay.io/coreos/etcd:v3.5.14"
DEFAULT_NATS_IMAGE = "docker.io/library/nats:2.10.18-alpine"
PROFILE_STATE_FILE = "k1s-profile.json"


@dataclass(frozen=True, slots=True)
class K1sProfileDescriptor:
    name: str
    description: str
    controllers: int
    state_backend: str
    transport_backend: str
    etcd: bool = False
    nats: bool = False
    ha_mode: bool = False
    workload_runtime: str = CONTAINERD_RUNTIME

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class K1sProfileComponent:
    name: str
    role: str
    image: str
    container_id: str | None = None
    host_port: int | None = None
    container_port: int | None = None
    url: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class K1sProfileInfo:
    project: str
    profile: str
    state_root: str
    state_dir: str
    k1s_root: str
    runtime: str
    network: str
    namespace: str
    started_at: float
    apishim_token: str
    admin_token: str
    read_token: str
    controller_url: str | None = None
    apishim_url: str | None = None
    dashboard_url: str | None = None
    ingress_urls: dict[str, str] = field(default_factory=dict)
    components: list[K1sProfileComponent] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("apishim_token", "admin_token", "read_token"):
            data[key] = "***"
        data["components"] = [component.public_dict() for component in self.components]
        return data


BUILTIN_PROFILES: dict[str, K1sProfileDescriptor] = {
    "k1s-dev-min-sqlite": K1sProfileDescriptor(
        name="k1s-dev-min-sqlite",
        description="Single containerized k1s controller with sqlite state.",
        controllers=1,
        state_backend="sqlite",
        transport_backend="http",
    ),
    "k1s-dev-etcd-labs": K1sProfileDescriptor(
        name="k1s-dev-etcd-labs",
        description="Single containerized k1s controller with labs-style etcd state.",
        controllers=1,
        state_backend="etcd",
        transport_backend="http",
        etcd=True,
    ),
    "k1s-single-etcd-containerd": K1sProfileDescriptor(
        name="k1s-single-etcd-containerd",
        description="Single containerized k1s controller with etcd and direct containerd.",
        controllers=1,
        state_backend="etcd",
        transport_backend="http",
        etcd=True,
    ),
    "k1s-ha-min": K1sProfileDescriptor(
        name="k1s-ha-min",
        description="Three containerized k1s controllers with shared etcd and NATS.",
        controllers=3,
        state_backend="etcd",
        transport_backend="nats-js",
        etcd=True,
        nats=True,
        ha_mode=True,
    ),
}


def builtin_profiles() -> dict[str, Any]:
    return {
        "ok": True,
        "profiles": [profile.public_dict() for profile in BUILTIN_PROFILES.values()],
        "runtime_requirement": CONTAINERD_RUNTIME,
        "host_k1s_processes": False,
    }


class K1sProfileRunner:
    """Start and manage WorkerBee-scoped, containerized k1s profiles."""

    def __init__(
        self,
        *,
        project: str,
        state_root: Path,
        runtime: str,
        cwd: Path | None = None,
        k1s_root: Path | None = None,
        ingress: ProjectIngressConfig | None = None,
    ) -> None:
        self.project = project_slug(project)
        self.state_root = state_root.expanduser().resolve()
        self.project_state = self.state_root / "projects" / self.project
        self.runtime_requested = runtime
        self.cwd = (cwd or Path.cwd()).resolve()
        self.k1s_root = k1s_root.expanduser().resolve() if k1s_root else None
        self.ingress = ingress

    def start(self, *, profile: str, timeout: float = 180.0) -> dict[str, Any]:
        descriptor = _profile_descriptor(profile)
        self._require_containerd()
        info = self.load()
        if info and info.profile != descriptor.name:
            self.stop(purge=False)
            info = None
        if info and self._all_components_running(info):
            info = self._refresh_ingress_info(info, descriptor)
            return {"ok": True, "started": False, "profile": info.public_dict()}

        self._ensure_layout(descriptor)
        self._ensure_network()
        self._write_helper_bridge()
        if info:
            self.stop(purge=False)

        controller_port = self._choose_profile_port(
            19608,
            start=19608,
            end=19708,
            span=descriptor.controllers,
        )
        apishim_port = self._choose_profile_port(18645, start=18645, end=18745)
        tokens = self._tokens()
        profile_dir = self._profile_dir(descriptor.name)
        components: list[K1sProfileComponent] = []

        if descriptor.etcd:
            components.append(
                self._start_etcd(
                    descriptor,
                    host_port=self._choose_profile_port(12379, start=12379, end=12479),
                )
            )
        if descriptor.nats:
            components.append(self._start_nats(descriptor))
        components.append(
            self._start_apishim(
                descriptor,
                host_port=apishim_port,
                token=tokens["apishim_token"],
            )
        )
        for index in range(descriptor.controllers):
            components.append(
                self._start_controller(
                    descriptor,
                    index=index,
                    host_port=controller_port + index,
                    apishim_host_port=apishim_port,
                    apishim_token=tokens["apishim_token"],
                    admin_token=tokens["admin_token"],
                    read_token=tokens["read_token"],
                )
            )

        ingress_urls = self._write_ingress_sites(
            descriptor=descriptor,
            controller_port=controller_port,
            apishim_port=apishim_port,
        )
        info = K1sProfileInfo(
            project=self.project,
            profile=descriptor.name,
            state_root=str(self.state_root),
            state_dir=str(profile_dir),
            k1s_root=str(self._resolve_k1s_root()),
            runtime=CONTAINERD_RUNTIME,
            network=self.network,
            namespace=self.namespace,
            started_at=time.time(),
            apishim_token=tokens["apishim_token"],
            admin_token=tokens["admin_token"],
            read_token=tokens["read_token"],
            controller_url=f"http://127.0.0.1:{controller_port}",
            apishim_url=f"http://127.0.0.1:{apishim_port}",
            dashboard_url=(
                ingress_urls.get("dashboard")
                or f"http://127.0.0.1:{controller_port}/dashboard"
            ),
            ingress_urls=ingress_urls,
            components=components,
        )
        self._write_info(info)
        readiness = self._wait_ready(info, timeout=timeout)
        return {
            "ok": bool(readiness.get("ok")),
            "started": True,
            "profile": info.public_dict(),
            "readiness": readiness,
        }

    def status(self, *, refresh_ingress: bool = True) -> dict[str, Any]:
        info = self.load()
        if not info:
            return {
                "ok": True,
                "running": False,
                "project": self.project,
                "state_root": str(self.state_root),
                "state_dir": str(self.project_state),
            }
        self._require_containerd()
        components = [self._component_status(component) for component in info.components]
        running = bool(components) and all(bool(item.get("running")) for item in components)
        if running and refresh_ingress:
            info = self._refresh_ingress_info(info, _profile_descriptor(info.profile))
        leader = self._leader_status(info)
        return {
            "ok": True,
            "running": running,
            "project": self.project,
            "profile": info.public_dict(),
            "components": components,
            "leader": leader,
        }

    def connection(self, *, profile: str | None = None, timeout: float = 180.0) -> dict[str, Any]:
        """Return an internal raw-token connection for WorkerBee-owned profile operations."""
        info = self.load()
        if profile:
            descriptor = _profile_descriptor(profile)
            needs_start = (
                not info
                or info.profile != descriptor.name
                or not self._all_components_running(info)
            )
            if needs_start:
                started = self.start(profile=descriptor.name, timeout=timeout)
                info = self.load()
                if not started.get("ok") or not info:
                    raise WorkerBeeError(
                        code="PROFILE_NOT_READY",
                        message="k1s profile did not become ready",
                        details={"profile": profile, "start": started},
                        retryable=True,
                    )
        if not info:
            raise WorkerBeeError(
                code="PROFILE_NOT_RUNNING",
                message="no k1s profile is running for this WorkerBee project",
                remediation="Start a profile first, or pass --profile to start one for this apply.",
            )
        if not self._all_components_running(info):
            raise WorkerBeeError(
                code="PROFILE_NOT_RUNNING",
                message="the recorded k1s profile is not running",
                details={"profile": info.profile},
                retryable=True,
            )
        info = self._refresh_ingress_info(info, _profile_descriptor(info.profile))
        urls = info.ingress_urls
        if not self.ingress or not urls.get("controller") or not urls.get("api"):
            raise WorkerBeeError(
                code="PROFILE_INGRESS_REQUIRED",
                message="profile workload operations require running WorkerBee MCP ingress",
                details={"project": self.project, "profile": info.profile},
                remediation=(
                    "Start WorkerBee MCP, then run profile start/status again so Caddy routes "
                    "are available."
                ),
            )
        ca_bundle = self.ingress.ca_bundle
        if not ca_bundle.is_file():
            raise WorkerBeeError(
                code="CA_NOT_READY",
                message="WorkerBee Caddy CA bundle is not ready",
                details={"ca_bundle": str(ca_bundle)},
                retryable=True,
            )
        return {
            "ok": True,
            "project": self.project,
            "profile": info.profile,
            "server": info.controller_url or urls["controller"],
            "api_server": info.apishim_url or urls["api"],
            "public_server": urls["controller"],
            "public_api_server": urls["api"],
            "ca_bundle": str(ca_bundle),
            "admin_token": info.admin_token,
            "read_token": info.read_token,
            "apishim_token": info.apishim_token,
            "urls": urls,
        }

    def workload_status(
        self,
        *,
        profile: str | None = None,
        namespace: str | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        connection = self.connection(profile=profile)
        ns = project_slug(namespace or self.project)
        resources = {
            "pods": f"/api/v1/namespaces/{ns}/pods",
            "services": f"/api/v1/namespaces/{ns}/services",
            "deployments": f"/apis/apps/v1/namespaces/{ns}/deployments",
            "statefulsets": f"/apis/apps/v1/namespaces/{ns}/statefulsets",
            "jobs": f"/apis/batch/v1/namespaces/{ns}/jobs",
            "ingresses": f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses",
        }
        results: dict[str, Any] = {}
        ok = True
        for name, path in resources.items():
            resp = request(
                f"{connection['api_server'].rstrip('/')}{path}",
                token=str(connection["apishim_token"]),
                timeout=timeout,
                ca_bundle=str(connection["ca_bundle"]),
            )
            body = _safe_json(resp)
            items = body.get("items") if isinstance(body, dict) else []
            if resp.status >= 400:
                ok = False
            results[name] = {
                "status": resp.status,
                "count": len(items) if isinstance(items, list) else 0,
                "items": items if isinstance(items, list) else [],
            }
        return {
            "ok": ok,
            "project": self.project,
            "profile": connection["profile"],
            "namespace": ns,
            "api_server": connection["api_server"],
            "resources": results,
        }

    def workload_logs(
        self,
        *,
        app: str,
        profile: str | None = None,
        namespace: str | None = None,
        tail: int = 80,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        connection = self.connection(profile=profile)
        ns = project_slug(namespace or self.project)
        app_name = project_slug(app)
        pods_resp = request(
            f"{connection['api_server'].rstrip('/')}/api/v1/namespaces/{ns}/pods",
            token=str(connection["apishim_token"]),
            timeout=timeout,
            ca_bundle=str(connection["ca_bundle"]),
        )
        pods = _safe_json(pods_resp).get("items", [])
        candidates = [
            item
            for item in pods
            if isinstance(item, dict) and _pod_matches_app(item, app_name)
        ]
        logs: list[dict[str, Any]] = []
        for pod in candidates:
            metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
            pod_name = str(metadata.get("name") or "")
            if not pod_name:
                continue
            path = f"/api/v1/namespaces/{ns}/pods/{pod_name}/log?tailLines={int(tail)}"
            resp = request(
                f"{connection['api_server'].rstrip('/')}{path}",
                token=str(connection["apishim_token"]),
                timeout=timeout,
                ca_bundle=str(connection["ca_bundle"]),
            )
            logs.append({"pod": pod_name, "status": resp.status, "text": resp.text})
        return {
            "ok": bool(logs) and all(item["status"] < 400 for item in logs),
            "project": self.project,
            "profile": connection["profile"],
            "namespace": ns,
            "app": app_name,
            "pods": [item.get("metadata", {}).get("name") for item in candidates],
            "logs": logs,
        }

    def stop(self, *, purge: bool = False) -> dict[str, Any]:
        info = self.load()
        if info or purge:
            self._require_containerd()
        removed: list[dict[str, Any]] = []
        if info:
            for component in reversed(info.components):
                removed.append(self._rm_container(component.name))
        purge_result = None
        if purge:
            profile_root = self.project_state / "profiles"
            purge_result = self._purge_profile_root(profile_root)
            self._rm_network()
        elif self.info_file.exists():
            self.info_file.unlink()
        return {
            "ok": all(item.get("ok") is not False for item in removed)
            and not (isinstance(purge_result, dict) and purge_result.get("ok") is False),
            "project": self.project,
            "removed": removed,
            "purged": purge,
            "purge_result": purge_result,
        }

    def _purge_profile_root(self, profile_root: Path) -> dict[str, Any]:
        if not profile_root.exists():
            return {"ok": True, "removed": False, "path": str(profile_root)}
        result = remove_containerd_helper_tree(self.state_root, profile_root)
        if result.get("ok"):
            return result
        try:
            shutil.rmtree(profile_root)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "removed": False,
                "path": str(profile_root),
                "helper": result,
                "error": str(exc),
            }
        return {
            "ok": True,
            "removed": True,
            "path": str(profile_root),
            "fallback": "shutil",
            "helper": result,
        }

    def validate(self, *, profile: str, timeout: float = 180.0) -> dict[str, Any]:
        started = self.start(profile=profile, timeout=timeout)
        status = self.status()
        leader = status.get("leader")
        if profile == "k1s-ha-min" and not (
            isinstance(leader, dict) and leader.get("controller_id")
        ):
            deadline = time.time() + timeout
            while time.time() < deadline:
                status = self.status()
                leader = status.get("leader")
                if isinstance(leader, dict) and leader.get("controller_id"):
                    break
                time.sleep(1.0)
        checks = [
            {
                "name": "profile-start",
                "ok": bool(started.get("ok")),
            },
            {
                "name": "components-running",
                "ok": bool(status.get("running")),
            },
            {
                "name": "containerized-components",
                "ok": all(
                    item.get("container_id")
                    for item in status.get("components", [])
                    if isinstance(item, dict)
                ),
            },
        ]
        if profile == "k1s-ha-min":
            checks.append(
                {
                    "name": "ha-leader-observed",
                    "ok": bool(isinstance(leader, dict) and leader.get("controller_id")),
                }
            )
        ok = all(bool(check.get("ok")) for check in checks)
        return {
            "ok": ok,
            "profile": profile,
            "started": started,
            "status": status,
            "checks": checks,
            "note": (
                "websocket FE/BE/DB workload validation is the next layer on top of "
                "this containerized k1s profile harness"
            ),
        }

    def load(self) -> K1sProfileInfo | None:
        if not self.info_file.is_file():
            return None
        try:
            data = json.loads(self.info_file.read_text(encoding="utf-8"))
            components = [
                K1sProfileComponent(**item)
                for item in data.get("components", [])
                if isinstance(item, dict)
            ]
            data = {key: value for key, value in data.items() if key != "components"}
            return K1sProfileInfo(**data, components=components)
        except Exception:
            return None

    @property
    def network(self) -> str:
        return containerd_network_name(self.state_root, self.project)

    @property
    def namespace(self) -> str:
        return containerd_namespace(self.state_root, self.project)

    @property
    def info_file(self) -> Path:
        return self.project_state / "profiles" / PROFILE_STATE_FILE

    def _profile_dir(self, profile: str) -> Path:
        return self.project_state / "profiles" / profile

    def _resolve_k1s_root(self) -> Path:
        if self.k1s_root is None:
            self.k1s_root = resolve_k1s_root(self.cwd)
        return self.k1s_root

    def _require_containerd(self) -> None:
        selected = resolve_runtime(self.runtime_requested)
        if selected != CONTAINERD_RUNTIME:
            raise WorkerBeeError(
                code="K1S_PROFILE_REQUIRES_CONTAINERD",
                message="WorkerBee k1s profiles require explicit direct containerd runtime",
                details={"requested_runtime": self.runtime_requested, "selected_runtime": selected},
                remediation=(
                    "Start WorkerBee with `--runtime containerd --containerd-privilege "
                    "sudo-helper` before using profile commands."
                ),
            )

    def _choose_profile_port(
        self,
        preferred: int,
        *,
        start: int,
        end: int,
        span: int = 1,
    ) -> int:
        reserved = _recorded_profile_host_ports(self.state_root, exclude_project=self.project)
        candidates = [int(preferred), *range(int(start), int(end) + 1)]
        seen: set[int] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            ports = list(range(candidate, candidate + int(span)))
            if ports[-1] > int(end):
                continue
            if any(port in reserved for port in ports):
                continue
            if all(port_is_free(port) for port in ports):
                return candidate
        raise RuntimeError(f"no free profile port range found in {start}-{end}")

    def _ensure_layout(self, descriptor: K1sProfileDescriptor) -> None:
        profile_dir = self._profile_dir(descriptor.name)
        for rel in ("logs", "specs", "state", "bin", "caddy", "data"):
            (profile_dir / rel).mkdir(parents=True, exist_ok=True)

    def _ensure_network(self) -> None:
        exists = subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=self.state_root,
                project=self.project,
                args=["network", "inspect", self.network],
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        if exists.returncode == 0:
            return
        self._run_nerdctl(
            [
                "network",
                "create",
                "--subnet",
                containerd_network_subnet(self.state_root, self.project),
                self.network,
            ],
            timeout=60,
        )

    def _tokens(self) -> dict[str, str]:
        token_file = self.project_state / "profiles" / "tokens.json"
        if token_file.is_file():
            try:
                data = json.loads(token_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        "apishim_token": str(
                            data.get("apishim_token") or secrets.token_urlsafe(24)
                        ),
                        "admin_token": str(data.get("admin_token") or secrets.token_urlsafe(24)),
                        "read_token": str(data.get("read_token") or secrets.token_urlsafe(24)),
                    }
            except Exception:
                data = {}
        tokens = {
            "apishim_token": secrets.token_urlsafe(24),
            "admin_token": secrets.token_urlsafe(24),
            "read_token": secrets.token_urlsafe(24),
        }
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps(tokens, indent=2, sort_keys=True), encoding="utf-8")
        token_file.chmod(0o600)
        return tokens

    def _component_name(
        self,
        descriptor: K1sProfileDescriptor,
        role: str,
        index: int | None = None,
    ) -> str:
        suffix = role if index is None else f"{role}-{index}"
        return f"{self.network}-{descriptor.name}-{suffix}"[:120].rstrip("-")

    def _common_env(self, descriptor: K1sProfileDescriptor) -> dict[str, str]:
        profile_dir = self._profile_dir(descriptor.name)
        bridge = self._helper_bridge_path(descriptor.name)
        project_data_root = containerd_data_root(self.state_root, project=self.project)
        project_cni_conf = containerd_cni_conf_dir(self.state_root, project=self.project)
        system_data_root = containerd_data_root(self.state_root, system=True)
        system_cni_conf = containerd_cni_conf_dir(self.state_root, system=True)
        env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": "/workspace/src",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "WORKERBEE_CONTAINERD_HELPER_SOCKET": os.getenv(
                "WORKERBEE_CONTAINERD_HELPER_SOCKET",
                str(self.state_root / "global" / "containerd-helper.sock"),
            ),
            "WORKERBEE_CONTAINERD_ADDRESS": containerd_address(),
            "WORKERBEE_CONTAINERD_NAMESPACE": self.namespace,
            "WORKERBEE_CONTAINERD_DATA_ROOT": str(project_data_root),
            "WORKERBEE_CONTAINERD_CNI_PATH": containerd_cni_bin_dir(),
            "WORKERBEE_CONTAINERD_CNI_NETCONFPATH": str(project_cni_conf),
            "WORKERBEE_CONTAINERD_SYSTEM_NAMESPACE": containerd_namespace(
                self.state_root,
                system=True,
            ),
            "WORKERBEE_CONTAINERD_SYSTEM_DATA_ROOT": str(system_data_root),
            "WORKERBEE_CONTAINERD_SYSTEM_CNI_NETCONFPATH": str(system_cni_conf),
            "AE_RUNTIME_BACKEND": CONTAINERD_RUNTIME,
            "AE_INFRA_BACKEND": CONTAINERD_RUNTIME,
            "AE_CONTAINER_CLI": str(bridge),
            "AE_NERDCTL_BIN": str(bridge),
            "AE_CONTAINERD_ADDRESS": containerd_address(),
            "AE_CRI_ENDPOINT": containerd_address(),
            "AE_CONTAINERD_NAMESPACE": self.namespace,
            "AE_CONTAINERD_DATA_ROOT": str(project_data_root),
            "AE_CONTAINERD_NETWORK": self.network,
            "AE_CONTAINERD_NETWORK_SUBNET": containerd_network_subnet(
                self.state_root,
                self.project,
            ),
            "AE_CONTAINERD_CNI_BIN_DIR": containerd_cni_bin_dir(),
            "AE_CONTAINERD_CNI_CONF_DIR": str(project_cni_conf),
            "NETCONFPATH": str(project_cni_conf),
            "AE_NETWORK_NAME": self.network,
            "AE_ALLOW_PLAINTEXT_SECRETS": "1",
            "AE_API_MUTATIONS": "1",
            "AE_LABS": "1",
            "AE_DASHBOARD": "1",
            "AE_DASHBOARD_INTERACTIVE_TOOLS": "1",
            "AE_REGISTER_LOCAL_NODE": "1",
            "AE_STATE_BACKEND": descriptor.state_backend,
            "AE_TRANSPORT_BACKEND": descriptor.transport_backend,
            "AE_SPECS_DIR": str(profile_dir / "specs"),
            "AE_PROJECTION_ROOT": str(profile_dir / "state" / "projections"),
            "DEV_PROFILE_DIR": str(profile_dir / "state"),
        }
        if descriptor.state_backend == "sqlite":
            env["AE_STATE_DB"] = str(profile_dir / "state" / "controller.db")
        if descriptor.etcd:
            env.update(
                {
                    "AE_ETCD_ENDPOINTS": f"http://{self._component_name(descriptor, 'etcd')}:2379",
                    "AE_APISHIM_ETCD_ENDPOINTS": (
                        f"http://{self._component_name(descriptor, 'etcd')}:2379"
                    ),
                    "AE_ETCD_PREFIX": f"k1s/workerbee/{self.project}/{descriptor.name}",
                }
            )
        if descriptor.nats:
            env.update(
                {
                    "AE_NATS_URL": (
                        f"nats://hub-controller:dev@"
                        f"{self._component_name(descriptor, 'nats')}:4222"
                    ),
                    "AE_JS_DOMAIN": "K1S",
                    "AE_JS_REPLICAS": "1",
                }
            )
        if descriptor.ha_mode:
            env.update(
                {
                    "AE_HA_MODE": "1",
                    "AE_NODE_PROFILE": "k1s-ha-core",
                }
            )
        if self.ingress:
            env.update(
                {
                    "AE_CADDY_SITES": str(self.ingress.sites_dir),
                    "AE_CADDY_CONTAINER": self.ingress.caddy_container,
                    "AE_CADDY_FILE": self.ingress.caddy_file,
                    "AE_CADDY_HOST_ALIAS": self.ingress.host_alias,
                    "AE_CADDY_RELOAD_TIMEOUT": "10",
                    "WORKERBEE_CONTAINERD_SYSTEM_CONTAINER": self.ingress.caddy_container,
                }
            )
        return env

    def _start_etcd(
        self,
        descriptor: K1sProfileDescriptor,
        *,
        host_port: int,
    ) -> K1sProfileComponent:
        name = self._component_name(descriptor, "etcd")
        data = self._profile_dir(descriptor.name) / "data" / "etcd"
        data.mkdir(parents=True, exist_ok=True)
        self._rm_container(name)
        image = os.getenv("WORKERBEE_K1S_PROFILE_ETCD_IMAGE", DEFAULT_ETCD_IMAGE)
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.network,
            "-p",
            f"127.0.0.1:{host_port}:2379",
            "-v",
            f"{data}:/etcd-data",
            *self._label_args("k1s-profile-etcd"),
            image,
            "/usr/local/bin/etcd",
            "--name",
            "etcd",
            "--data-dir",
            "/etcd-data",
            "--listen-client-urls",
            "http://0.0.0.0:2379",
            "--advertise-client-urls",
            f"http://{name}:2379",
            "--listen-peer-urls",
            "http://0.0.0.0:2380",
            "--initial-advertise-peer-urls",
            f"http://{name}:2380",
            "--initial-cluster",
            f"etcd=http://{name}:2380",
            "--initial-cluster-state",
            "new",
        ]
        return self._run_component(
            name=name,
            role="etcd",
            image=image,
            args=args,
            host_port=host_port,
            container_port=2379,
            url=f"http://127.0.0.1:{host_port}",
        )

    def _start_nats(self, descriptor: K1sProfileDescriptor) -> K1sProfileComponent:
        name = self._component_name(descriptor, "nats")
        data = self._profile_dir(descriptor.name) / "data" / "nats"
        data.mkdir(parents=True, exist_ok=True)
        config = self._write_nats_config(descriptor)
        self._rm_container(name)
        image = os.getenv("WORKERBEE_K1S_PROFILE_NATS_IMAGE", DEFAULT_NATS_IMAGE)
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.network,
            "-v",
            f"{data}:/data",
            "-v",
            f"{config}:/etc/nats/nats.conf:ro",
            *self._label_args("k1s-profile-nats"),
            image,
            "-c",
            "/etc/nats/nats.conf",
        ]
        return self._run_component(name=name, role="nats", image=image, args=args)

    def _write_nats_config(self, descriptor: K1sProfileDescriptor) -> Path:
        config = self._profile_dir(descriptor.name) / "config" / "nats.conf"
        config.parent.mkdir(parents=True, exist_ok=True)
        content = textwrap.dedent(
            """\
            port: 4222
            http_port: 8222
            jetstream {
              store_dir: "/data"
              domain: "K1S"
            }
            authorization {
              users = [
                {user: "hub-controller", password: "dev"}
              ]
            }
            """
        )
        config.write_text(content, encoding="utf-8")
        return config

    def _start_apishim(
        self,
        descriptor: K1sProfileDescriptor,
        *,
        host_port: int,
        token: str,
    ) -> K1sProfileComponent:
        name = self._component_name(descriptor, "apishim")
        self._rm_container(name)
        env = self._common_env(descriptor)
        env.update(
            {
                "AE_APISHIM_ENABLE": "1",
                "AE_APISHIM_ALLOW_ANON": "1",
                "AE_APISHIM_TOKEN": token,
                "AE_APISHIM_READ_TOKEN": token,
                "AE_APISHIM_DB": str(self._profile_dir(descriptor.name) / "state" / "apishim.db"),
                "AE_APISHIM_RUNTIME": CONTAINERD_RUNTIME,
            }
        )
        image = os.getenv("WORKERBEE_K1S_PROFILE_PYTHON_IMAGE", DEFAULT_K1S_PYTHON_IMAGE)
        command = (
            "cd /workspace && "
            "python -m pip install --no-cache-dir -r /workspace/requirements.txt "
            ">/tmp/k1s-pip-install.log 2>&1 && "
            "exec python -m ae.apishim serve --host 0.0.0.0 --port 8445 --allow-anonymous"
        )
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.network,
            "-p",
            f"127.0.0.1:{host_port}:8445",
            *self._mount_args(descriptor.name),
            *self._env_args(env),
            *self._label_args("k1s-profile-apishim"),
            image,
            "/bin/sh",
            "-ec",
            command,
        ]
        return self._run_component(
            name=name,
            role="apishim",
            image=image,
            args=args,
            host_port=host_port,
            container_port=8445,
            url=f"http://127.0.0.1:{host_port}",
        )

    def _start_controller(
        self,
        descriptor: K1sProfileDescriptor,
        *,
        index: int,
        host_port: int,
        apishim_host_port: int,
        apishim_token: str,
        admin_token: str,
        read_token: str,
    ) -> K1sProfileComponent:
        name = self._component_name(descriptor, "controller", index)
        self._rm_container(name)
        env = self._common_env(descriptor)
        env.update(
            {
                "AE_CONTROLLER_ID": f"{descriptor.name}-controller-{index}",
                "AE_CONTROLLER_ADVERTISE_ADDR": f"http://{name}:9108",
                "AE_API_ADMIN_TOKEN": admin_token,
                "AE_API_READ_TOKEN": read_token,
                "AE_API_SCALER_TOKEN": admin_token,
                "AE_APISHIM_SERVER": f"http://{self._component_name(descriptor, 'apishim')}:8445",
                "AE_APISHIM_PUBLIC_BASE": self._profile_apishim_public_base(apishim_host_port),
                "AE_APISHIM_TOKEN": apishim_token,
                "AE_APISHIM_READ_TOKEN": read_token,
                "AE_DASHBOARD_BOOTSTRAP_TOKEN": admin_token,
            }
        )
        image = os.getenv("WORKERBEE_K1S_PROFILE_PYTHON_IMAGE", DEFAULT_K1S_PYTHON_IMAGE)
        command = (
            "cd /workspace && "
            "python -m pip install --no-cache-dir -r /workspace/requirements.txt "
            ">/tmp/k1s-pip-install.log 2>&1 && "
            "exec python -m ae.controller --loop --specs \"$AE_SPECS_DIR\" "
            "--metrics-port 9108 --watch"
        )
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.network,
            "-p",
            f"127.0.0.1:{host_port}:9108",
            *self._mount_args(descriptor.name),
            *self._env_args(env),
            *self._label_args("k1s-profile-controller"),
            image,
            "/bin/sh",
            "-ec",
            command,
        ]
        return self._run_component(
            name=name,
            role="controller",
            image=image,
            args=args,
            host_port=host_port,
            container_port=9108,
            url=f"http://127.0.0.1:{host_port}",
        )

    def _profile_apishim_public_base(self, apishim_port: int) -> str:
        if self.ingress:
            return self.ingress.url(f"k1s-api.{self.project}.workerbee.localhost", "/").rstrip("/")
        return f"http://127.0.0.1:{int(apishim_port)}"

    def _run_component(
        self,
        *,
        name: str,
        role: str,
        image: str,
        args: list[str],
        host_port: int | None = None,
        container_port: int | None = None,
        url: str | None = None,
    ) -> K1sProfileComponent:
        proc = self._run_nerdctl(args, timeout=120)
        container_id = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None
        return K1sProfileComponent(
            name=name,
            role=role,
            image=image,
            container_id=container_id,
            host_port=host_port,
            container_port=container_port,
            url=url,
        )

    def _run_nerdctl(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=self.state_root,
                project=self.project,
                args=args,
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout.strip() or f"nerdctl {' '.join(args)} failed")
        return proc

    def _label_args(self, component: str) -> list[str]:
        labels = [
            *workerbee_runtime_labels(state_root=self.state_root, project=self.project),
            "workerbee.component=k1s-profile",
            f"workerbee.k1s_profile_component={component}",
        ]
        args: list[str] = []
        for label in labels:
            args.extend(["--label", label])
        return args

    def _env_args(self, env: dict[str, str]) -> list[str]:
        args: list[str] = []
        for key, value in sorted(env.items()):
            args.extend(["-e", f"{key}={value}"])
        return args

    def _mount_args(self, profile: str) -> list[str]:
        profile_dir = self._profile_dir(profile)
        mounts = [
            f"{self._resolve_k1s_root()}:/workspace:ro",
            f"{self.state_root}:{self.state_root}",
            f"{profile_dir / 'specs'}:{profile_dir / 'specs'}",
            f"{profile_dir / 'state'}:{profile_dir / 'state'}",
        ]
        args: list[str] = []
        for mount in mounts:
            args.extend(["-v", mount])
        return args

    def _wait_ready(self, _info: K1sProfileInfo, *, timeout: float) -> dict[str, Any]:
        deadline = time.time() + timeout
        controller_ok = False
        apishim_ok = False
        errors: list[str] = []
        while time.time() < deadline:
            status = self.status()
            if not status.get("running"):
                time.sleep(1.0)
                continue
            controller_ok = True
            apishim_ok = True
            break
        if not controller_ok:
            errors.append("profile containers did not all report running before timeout")
            return {
                "ok": False,
                "controller": controller_ok,
                "apishim": apishim_ok,
                "errors": errors,
            }
        info = _info
        remaining = max(1.0, deadline - time.time())
        try:
            wait_for_http(
                f"{info.controller_url}/health",
                token=info.read_token,
                timeout_seconds=remaining,
                ok_statuses={200},
            )
        except Exception as exc:  # noqa: BLE001
            controller_ok = False
            errors.append(f"controller health failed: {exc}")
        remaining = max(1.0, deadline - time.time())
        try:
            wait_for_http(
                f"{info.apishim_url}/healthz",
                token=info.apishim_token,
                timeout_seconds=remaining,
                ok_statuses={200},
            )
        except Exception as exc:  # noqa: BLE001
            apishim_ok = False
            errors.append(f"apishim health failed: {exc}")
        return {
            "ok": controller_ok and apishim_ok,
            "controller": controller_ok,
            "apishim": apishim_ok,
            "errors": errors,
        }

    def _all_components_running(self, info: K1sProfileInfo) -> bool:
        if not info.components:
            return False
        return all(
            bool(self._component_status(component).get("running"))
            for component in info.components
        )

    def _component_status(self, component: K1sProfileComponent) -> dict[str, Any]:
        proc = subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=self.state_root,
                project=self.project,
                args=["ps", "--format", "{{.Names}}", "--filter", f"name={component.name}"],
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        names = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        return {
            "name": component.name,
            "role": component.role,
            "running": proc.returncode == 0 and component.name in names,
            "container_id": component.container_id,
            "url": component.url,
        }

    def _rm_container(self, name: str) -> dict[str, Any]:
        proc = subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=self.state_root,
                project=self.project,
                args=["rm", "-f", name],
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        ok = proc.returncode == 0 or _missing_container(proc.stdout)
        return {"ok": ok, "container": name, "stdout": proc.stdout, "returncode": proc.returncode}

    def _rm_network(self) -> dict[str, Any]:
        proc = subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=self.state_root,
                project=self.project,
                args=["network", "rm", self.network],
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        ok = proc.returncode == 0 or "not found" in proc.stdout.lower()
        return {"ok": ok, "network": self.network, "stdout": proc.stdout}

    def _write_info(self, info: K1sProfileInfo) -> None:
        self.info_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.info_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(info), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.info_file)

    def _refresh_ingress_info(
        self,
        info: K1sProfileInfo,
        descriptor: K1sProfileDescriptor,
    ) -> K1sProfileInfo:
        if not self.ingress:
            return info
        controller = next(
            (
                component
                for component in info.components
                if component.role == "controller" and component.host_port
            ),
            None,
        )
        apishim = next(
            (
                component
                for component in info.components
                if component.role == "apishim" and component.host_port
            ),
            None,
        )
        if not controller or not apishim:
            return info
        previous_urls = dict(info.ingress_urls)
        previous_dashboard = info.dashboard_url
        ingress_urls = self._write_ingress_sites(
            descriptor=descriptor,
            controller_port=int(controller.host_port),
            apishim_port=int(apishim.host_port),
        )
        if not ingress_urls:
            return info
        info.ingress_urls = ingress_urls
        info.dashboard_url = ingress_urls.get("dashboard") or info.dashboard_url
        if previous_urls != info.ingress_urls or previous_dashboard != info.dashboard_url:
            self._write_info(info)
        return info

    def _write_ingress_sites(
        self,
        *,
        descriptor: K1sProfileDescriptor,
        controller_port: int,
        apishim_port: int,
    ) -> dict[str, str]:
        if not self.ingress:
            return {}
        controller_host = f"k1s.{self.project}.workerbee.localhost"
        legacy_dash_host = f"k1s-dash.{self.project}.workerbee.localhost"
        api_host = f"k1s-api.{self.project}.workerbee.localhost"
        site = self.ingress.sites_dir / "k1s-profile.caddy"
        site.parent.mkdir(parents=True, exist_ok=True)
        asset_proxy = ""
        if self.ingress.dashboard_port:
            asset_proxy = f"""
    handle /static/dash-assets/* {{
        reverse_proxy {self.ingress.host_alias}:{self.ingress.dashboard_port}
    }}
"""
        content = f"""# Generated by WorkerBee k1s profile runner.
https://{controller_host}, https://{legacy_dash_host} {{
    header -Strict-Transport-Security
    tls internal
{asset_proxy.rstrip()}
    handle {{
        reverse_proxy {self.ingress.host_alias}:{controller_port}
    }}
}}

https://{api_host} {{
    header -Strict-Transport-Security
    tls internal
    reverse_proxy {self.ingress.host_alias}:{apishim_port}
}}
"""
        if not site.is_file() or site.read_text(encoding="utf-8") != content:
            site.write_text(content, encoding="utf-8")
        return {
            "controller": self.ingress.url(controller_host, "/"),
            "dashboard": self.ingress.url(controller_host, "/dashboard"),
            "docs": self.ingress.url(controller_host, "/docs"),
            "swagger": self.ingress.url(controller_host, "/swagger"),
            "redoc": self.ingress.url(controller_host, "/redoc"),
            "controller_openapi": self.ingress.url(controller_host, "/openapi.json"),
            "controller_health": self.ingress.url(controller_host, "/health"),
            "apishim": self.ingress.url(api_host, "/"),
            "api": self.ingress.url(api_host, "/"),
            "api_healthz": self.ingress.url(api_host, "/healthz"),
            "api_openapi_v2": self.ingress.url(api_host, "/openapi/v2"),
            "api_openapi_v3": self.ingress.url(api_host, "/openapi/v3"),
            "api_swagger_json": self.ingress.url(api_host, "/swagger.json"),
            "legacy_dashboard": self.ingress.url(legacy_dash_host, "/dashboard"),
            "profile": descriptor.name,
        }

    def _leader_status(self, info: K1sProfileInfo) -> dict[str, Any] | None:
        if info.profile != "k1s-ha-min":
            return None
        etcd = next((component for component in info.components if component.role == "etcd"), None)
        if not etcd or not etcd.url:
            return {"controller_id": None, "error": "etcd endpoint not recorded"}
        key = f"k1s/workerbee/{self.project}/{info.profile}/controlplane/leader"
        payload = {
            "key": base64.b64encode(key.encode("utf-8")).decode("ascii"),
            "limit": 1,
        }
        req = urllib_request.Request(  # noqa: S310 - local etcd endpoint
            f"{etcd.url.rstrip('/')}/v3/kv/range",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(  # noqa: S310 - local etcd endpoint
                req,
                timeout=3,
            ) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            return {"controller_id": None, "endpoint": etcd.url, "error": str(exc)}
        kvs = body.get("kvs") or []
        if not kvs:
            return {"controller_id": None, "endpoint": etcd.url, "key": key}
        raw_value = base64.b64decode(str(kvs[0].get("value") or "")).decode("utf-8")
        record = json.loads(raw_value or "{}")
        return {
            "controller_id": str(record.get("controller_id") or ""),
            "controller_epoch": int(kvs[0].get("mod_revision") or 0),
            "advertise_addr": record.get("advertise_addr"),
            "endpoint": etcd.url,
            "key": key,
        }

    def _helper_bridge_path(self, profile: str) -> Path:
        return self._profile_dir(profile) / "bin" / "workerbee-nerdctl"

    def _write_helper_bridge(self) -> None:
        for descriptor in BUILTIN_PROFILES.values():
            path = self._helper_bridge_path(descriptor.name)
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_helper_bridge_script(), encoding="utf-8")
            path.chmod(0o755)


def _profile_descriptor(profile: str) -> K1sProfileDescriptor:
    key = str(profile or "").strip()
    descriptor = BUILTIN_PROFILES.get(key)
    if descriptor is None:
        raise WorkerBeeError(
            code="K1S_PROFILE_UNKNOWN",
            message=f"unknown WorkerBee k1s profile `{profile}`",
            details={"profile": profile, "available": sorted(BUILTIN_PROFILES)},
            remediation="Run `workerbee profile list` to inspect supported profiles.",
        )
    return descriptor


def _helper_bridge_script() -> str:
    script = r'''
import base64
import json
import os
import socket
import sys


def main() -> int:
    socket_path = os.environ.get("WORKERBEE_CONTAINERD_HELPER_SOCKET")
    if not socket_path:
        sys.stderr.write("WORKERBEE_CONTAINERD_HELPER_SOCKET is not set\n")
        return 127
    argv = sys.argv[1:]
    if not _has_global_args(argv):
        argv = [*_base_args(argv), *argv]
    stdin = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    payload = {"action": "run", "argv": argv}
    if stdin:
        payload["stdin_b64"] = base64.b64encode(stdin).decode("ascii")
    response = _request(socket_path, payload)
    stdout = base64.b64decode(str(response.get("stdout_b64") or ""))
    stderr = base64.b64decode(str(response.get("stderr_b64") or ""))
    if stdout:
        sys.stdout.buffer.write(stdout)
    if stderr:
        sys.stderr.buffer.write(stderr)
    return int(response.get("returncode") or 0)


def _has_global_args(argv: list[str]) -> bool:
    return "--address" in argv and "--namespace" in argv


def _base_args(argv: list[str]) -> list[str]:
    use_system = (
        len(argv) >= 2
        and argv[0] == "exec"
        and argv[1] == os.environ.get("WORKERBEE_CONTAINERD_SYSTEM_CONTAINER")
    )
    prefix = "WORKERBEE_CONTAINERD_SYSTEM_" if use_system else "WORKERBEE_CONTAINERD_"
    return [
        "--address",
        os.environ["WORKERBEE_CONTAINERD_ADDRESS"],
        "--namespace",
        os.environ[prefix + "NAMESPACE"],
        "--data-root",
        os.environ[prefix + "DATA_ROOT"],
        "--cni-path",
        os.environ["WORKERBEE_CONTAINERD_CNI_PATH"],
        "--cni-netconfpath",
        os.environ[prefix + "CNI_NETCONFPATH"],
    ]


def _request(socket_path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(socket_path)
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


if __name__ == "__main__":
    raise SystemExit(main())
'''
    return "#!/usr/bin/env python3\n" + textwrap.dedent(script).lstrip()


def _missing_container(output: str) -> bool:
    lowered = output.lower()
    return "no such container" in lowered or "not found" in lowered


def _recorded_profile_host_ports(state_root: Path, *, exclude_project: str) -> set[int]:
    root = state_root.expanduser().resolve()
    excluded = project_slug(exclude_project)
    ports: set[int] = set()
    for info_file in root.glob("projects/*/profiles/k1s-profile.json"):
        project = info_file.parents[1].name if len(info_file.parents) >= 2 else ""
        if project_slug(project) == excluded:
            continue
        try:
            data = json.loads(info_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        components = data.get("components", [])
        if not isinstance(components, list):
            continue
        for component in components:
            if not isinstance(component, dict):
                continue
            try:
                port = int(component.get("host_port") or 0)
            except (TypeError, ValueError):
                continue
            if port > 0:
                ports.add(port)
    return ports


def _safe_json(resp: Any) -> dict[str, Any]:
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _pod_matches_app(pod: dict[str, Any], app: str) -> bool:
    metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
    name = project_slug(str(metadata.get("name") or ""))
    labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
    label_values = {project_slug(str(value)) for value in labels.values()}
    return name == app or name.startswith(f"{app}-") or app in label_values
