"""Containerized k1s edge-link runner for external-core development."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from workerbee.containerd_helper import remove_containerd_helper_tree
from workerbee.contract import WorkerBeeError
from workerbee.http import request
from workerbee.paths import resolve_k1s_root
from workerbee.ports import choose_port
from workerbee.profiles import _helper_bridge_script
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
from workerbee.secrets import secret_env_for_project, write_private_json
from workerbee.supervisor import project_slug

EDGE_LINK_PROFILE_NAME = "k1s-edge-link"
EDGE_LINK_STATE_FILE = "k1s-edge-link.json"
DEFAULT_EDGE_SITE_ID = "workerbee-edge"
DEFAULT_EDGE_NODE_ID = "workerbee-edge-node"
DEFAULT_MICROK8S_RELEASE = "k1s-dev-a"
DEFAULT_MICROK8S_NAMESPACE = "k1s-dev-a"
DEFAULT_NATS_IMAGE = "docker.io/library/nats:2.10.18-alpine"
DEFAULT_RATHOLE_IMAGE = "docker.io/rapiz1/rathole:v0.5.0"
DEFAULT_GPU_SMOKE_IMAGE = "docker.io/nvidia/cuda:12.4.1-base-ubuntu22.04"
DEFAULT_K1S_PYTHON_IMAGE = "docker.io/library/python:3.12-slim"
DEFAULT_EDGE_LOCAL_ADDR = "127.0.0.1:18081"
AI_MAX_EDGE_CELL_PROFILE = "ai-max-edge-cell-v1"
AI_MAX_EDGE_CELL_NODE_COUNT = 3
AI_MAX_EDGE_CELL_SIZE = 4
SUPPORTED_AI_MAX_FABRIC_CELL_COUNTS = {1, 2, 4, 8}
DEFAULT_EDGE_LAN_SCOPE = "workerbee-lan"
AI_MAX_INSTALLER_PROFILE = "nixos-ai-max-edge-cell-installer-v1"
AI_MAX_INSTALLER_IMAGE = "nixos-ai-max-edge-cell-installer"
AI_MAX_INSTALLER_SIGNER = "k1s-core-root-of-trust"
AI_MAX_INSTALLER_ARTIFACT_VERSION = "stage7-local"
AI_MAX_INSTALLER_ARTIFACT_DIGEST = (
    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
)
AI_MAX_INSTALLER_MANIFEST_DIGEST = (
    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
)
AI_MAX_INSTALLER_SIGNATURE_ALGORITHM = "k1s-local-sim-ed25519-sha256"
AI_MAX_INSTALLER_SIGNATURE = (
    "k1s-sim-signature:3333333333333333333333333333333333333333333333333333333333333333"
)
AI_MAX_INSTALLER_PROVENANCE_BUILDER = "k1s-public-stage7-local-simulator"
AI_MAX_INSTALLER_PROVENANCE_SOURCE_REVISION = "public-dev-stage7"
AI_MAX_INSTALLER_PROVENANCE_CREATED_AT = "2026-06-25T00:00:00Z"
AI_MAX_INSTALLER_GATEWAY_MODULE_REF = "nixos/modules/ai-max/installer/gateway.nix"
AI_MAX_INSTALLER_GATEWAY_CONFIG_REF = "nixos/configs/ai-max/gateway-installed-system.nix"
AI_MAX_INSTALLER_CELL_NODE_MODULE_REF = "nixos/modules/ai-max/installer/cell-node.nix"
AI_MAX_INSTALLER_CELL_NODE_CONFIG_REF = "nixos/configs/ai-max/cell-node-installed-system.nix"
AI_MAX_GATEWAY_BOOT_MEASUREMENT_DIGEST = (
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
AI_MAX_CELL_NODE_BOOT_MEASUREMENT_DIGEST = (
    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)
AI_MAX_GATEWAY_BOOT_NONCE = "k1s-stage9-nonce-gateway"
AI_MAX_CELL_NODE_BOOT_NONCE = "k1s-stage9-nonce-cell-node"
AI_MAX_BOOT_EVIDENCE_CREATED_AT = "2026-06-25T00:00:00Z"


@dataclass(slots=True)
class K1sEdgeLinkComponent:
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
class K1sEdgeLinkInfo:
    project: str
    profile: str
    state_root: str
    edge_dir: str
    k1s_root: str
    runtime: str
    network: str
    namespace: str
    started_at: float
    site_id: str
    node_id: str
    controller_url: str
    agent_token: str
    nats_leaf_addr: str
    nats_leaf_url: str = ""
    rathole_server_addr: str = ""
    rathole_server_addrs: list[str] = field(default_factory=list)
    rathole_token: str = ""
    registry_host: str = ""
    stack_domain: str = ""
    wildcard_apps_domain: str = ""
    advertise_host: str = ""
    agent_endpoint: str = ""
    edge_local_addr: str = DEFAULT_EDGE_LOCAL_ADDR
    agent_host_port: int | None = None
    cell_node_count: int = 0
    fabric_cell_count: int = 1
    lan_scope: str = DEFAULT_EDGE_LAN_SCOPE
    edge_cell_contract: dict[str, Any] = field(default_factory=dict)
    gateway_image: str = ""
    node_image: str = ""
    gpu: dict[str, Any] = field(default_factory=dict)
    bootstrap: dict[str, Any] = field(default_factory=dict)
    components: list[K1sEdgeLinkComponent] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("agent_token", "rathole_token", "nats_leaf_url"):
            if data.get(key):
                data[key] = "***"
        data["bootstrap"] = _mask_sensitive(data.get("bootstrap") or {})
        data["components"] = [component.public_dict() for component in self.components]
        return data


class K1sEdgeLinkRunner:
    """Run a WorkerBee-scoped k1s edge gateway and node against an external core."""

    def __init__(
        self,
        *,
        project: str,
        state_root: Path,
        runtime: str,
        cwd: Path | None = None,
        k1s_root: Path | None = None,
    ) -> None:
        self.project = project_slug(project)
        self.state_root = state_root.expanduser().resolve()
        self.project_state = self.state_root / "projects" / self.project
        self.runtime_requested = runtime
        self.cwd = (cwd or Path.cwd()).resolve()
        self.k1s_root = k1s_root.expanduser().resolve() if k1s_root else None
        self._gateway_image_override: str | None = None
        self._node_image_override: str | None = None

    def start(
        self,
        *,
        from_microk8s: bool = False,
        release: str = DEFAULT_MICROK8S_RELEASE,
        namespace: str = DEFAULT_MICROK8S_NAMESPACE,
        site_id: str = DEFAULT_EDGE_SITE_ID,
        node_id: str = DEFAULT_EDGE_NODE_ID,
        bundle: dict[str, Any] | str | None = None,
        bundle_path: str | Path | None = None,
        controller_url: str | None = None,
        agent_token: str | None = None,
        nats_leaf_addr: str | None = None,
        nats_leaf_url: str | None = None,
        rathole_server_addr: str | None = None,
        rathole_token: str | None = None,
        registry_host: str | None = None,
        stack_domain: str | None = None,
        wildcard_apps_domain: str | None = None,
        advertise_host: str | None = None,
        edge_local_addr: str | None = None,
        cell_node_count: int = 0,
        fabric_cell_count: int = 1,
        lan_scope: str = DEFAULT_EDGE_LAN_SCOPE,
        timeout: float = 180.0,
        build_images: bool = True,
    ) -> dict[str, Any]:
        self._require_containerd()
        cell_nodes = _normalize_cell_node_count(cell_node_count)
        fabric_cells = _normalize_fabric_cell_count(fabric_cell_count)
        normalized_lan_scope = _normalize_lan_scope(lan_scope)
        _validate_edge_cell_fabric_shape(cell_nodes, fabric_cells)
        bootstrap = self._resolve_bootstrap(
            from_microk8s=from_microk8s,
            release=release,
            namespace=namespace,
            site_id=site_id,
            bundle=bundle,
            bundle_path=bundle_path,
            controller_url=controller_url,
            agent_token=agent_token,
            nats_leaf_addr=nats_leaf_addr,
            nats_leaf_url=nats_leaf_url,
            rathole_server_addr=rathole_server_addr,
            rathole_token=rathole_token,
            registry_host=registry_host,
            stack_domain=stack_domain,
            wildcard_apps_domain=wildcard_apps_domain,
            edge_local_addr=edge_local_addr,
        )
        site = str(bootstrap.get("site_id") or site_id or DEFAULT_EDGE_SITE_ID)
        node = str(node_id or bootstrap.get("node_id") or DEFAULT_EDGE_NODE_ID)
        edge_local = _edge_local_addr(edge_local_addr, bootstrap)
        rathole_addrs = _rathole_server_addrs(bootstrap)
        info = self.load()
        if (
            info
            and info.site_id == site
            and info.node_id == node
            and info.edge_local_addr == edge_local
            and info.cell_node_count == cell_nodes
            and info.fabric_cell_count == fabric_cells
            and info.lan_scope == normalized_lan_scope
            and info.rathole_server_addrs == rathole_addrs
            and self._all_components_running(info)
        ):
            return {"ok": True, "started": False, "edge_link": info.public_dict()}
        if info:
            self.stop(purge=False)

        self._ensure_layout()
        self._ensure_network()
        self._write_helper_bridge()
        builds = self._ensure_images(build=build_images)
        ports: set[int] = set()
        nats_port = choose_port(14223, start=14223, end=14323, reserved=ports)
        nats_http_port = choose_port(18223, start=18223, end=18323, reserved=ports)
        agent_port = choose_port(
            19109,
            start=19109,
            end=19209,
            host="0.0.0.0",  # noqa: S104 - external core must reach the node agent.
            reserved=ports,
        )
        host = str(advertise_host or self._infer_advertise_host() or "").strip()
        if not host:
            raise WorkerBeeError(
                code="EDGE_LINK_ADVERTISE_HOST_REQUIRED",
                message="edge-link node needs a LAN-reachable advertise host",
                remediation=(
                    "Pass advertise_host/--advertise-host for the host address "
                    "reachable by the external core."
                ),
            )
        agent_endpoint = f"http://{host}:{agent_port}"
        extra_agent_count = (fabric_cells * AI_MAX_EDGE_CELL_SIZE - 1) if cell_nodes else 0
        cell_agent_ports = [
            choose_port(
                19109 + idx,
                start=19109,
                end=19209,
                host="0.0.0.0",  # noqa: S104 - external core must reach simulated node agents.
                reserved=ports,
            )
            for idx in range(1, extra_agent_count + 1)
        ]
        edge_cell_contract = _edge_cell_contract(
            gateway_node_id=node,
            site_id=site,
            advertise_host=host,
            gateway_agent_port=agent_port,
            cell_agent_ports=cell_agent_ports,
            fabric_cell_count=fabric_cells,
            lan_scope=normalized_lan_scope,
        )
        gpu = self._detect_nvidia()
        components = [
            self._start_edge_nats(
                site_id=site,
                leaf_url=str(bootstrap.get("nats_leaf_url") or ""),
                leaf_addr=str(bootstrap.get("nats_leaf_addr") or ""),
                host_port=nats_port,
                http_port=nats_http_port,
            ),
            *[
                self._start_rathole_client(
                    site_id=site,
                    server_addr=addr,
                    token=str(bootstrap.get("rathole_token") or ""),
                    edge_local_addr=edge_local,
                    index=(idx if len(rathole_addrs) > 1 else None),
                )
                for idx, addr in enumerate(rathole_addrs, start=1)
            ],
            self._start_gateway(
                site_id=site,
                node_id=node,
                bootstrap=bootstrap,
                edge_local_addr=edge_local,
            ),
            self._start_node(
                site_id=site,
                node_id=node,
                bootstrap=bootstrap,
                agent_endpoint=agent_endpoint,
                host_port=agent_port,
                gpu=gpu,
                component="node",
                role_label="gateway",
            ),
            *[
                self._start_node(
                    site_id=site,
                    node_id=str(item["node_id"]),
                    bootstrap=bootstrap,
                    agent_endpoint=str(item["agent_endpoint"]),
                    host_port=int(item["agent_host_port"]),
                    gpu=gpu,
                    component=str(item["component"]),
                    role_label=str(item["role"]),
                )
                for item in edge_cell_contract.get("members") or []
                if item.get("component") != "node"
            ],
        ]
        info = K1sEdgeLinkInfo(
            project=self.project,
            profile=EDGE_LINK_PROFILE_NAME,
            state_root=str(self.state_root),
            edge_dir=str(self.edge_dir),
            k1s_root=str(self._resolve_k1s_root()),
            runtime=CONTAINERD_RUNTIME,
            network=self.network,
            namespace=self.namespace,
            started_at=time.time(),
            site_id=site,
            node_id=node,
            controller_url=str(bootstrap["controller_url"]),
            agent_token=str(bootstrap["agent_token"]),
            nats_leaf_addr=str(bootstrap.get("nats_leaf_addr") or ""),
            nats_leaf_url=str(bootstrap.get("nats_leaf_url") or ""),
            rathole_server_addr=str(bootstrap.get("rathole_server_addr") or ""),
            rathole_server_addrs=rathole_addrs,
            rathole_token=str(bootstrap.get("rathole_token") or ""),
            registry_host=str(bootstrap.get("registry_host") or ""),
            stack_domain=str(bootstrap.get("stack_domain") or ""),
            wildcard_apps_domain=str(bootstrap.get("wildcard_apps_domain") or ""),
            advertise_host=host,
            agent_endpoint=agent_endpoint,
            edge_local_addr=edge_local,
            agent_host_port=agent_port,
            cell_node_count=cell_nodes,
            fabric_cell_count=fabric_cells,
            lan_scope=normalized_lan_scope,
            edge_cell_contract=edge_cell_contract,
            gateway_image=self.gateway_image,
            node_image=self.node_image,
            gpu=gpu,
            bootstrap=bootstrap,
            components=components,
        )
        self._write_info(info)
        readiness = self._wait_ready(info, timeout=timeout)
        return {
            "ok": bool(readiness.get("ok")),
            "started": True,
            "edge_link": info.public_dict(),
            "readiness": readiness,
            "builds": builds,
        }

    def status(self) -> dict[str, Any]:
        info = self.load()
        if not info:
            return {
                "ok": True,
                "running": False,
                "project": self.project,
                "profile": EDGE_LINK_PROFILE_NAME,
                "state_root": str(self.state_root),
                "state_dir": str(self.edge_dir),
            }
        self._require_containerd()
        components = [self._component_status(component) for component in info.components]
        running = bool(components) and all(bool(item.get("running")) for item in components)
        node = self._node_record(info)
        return {
            "ok": True,
            "running": running,
            "project": self.project,
            "profile": EDGE_LINK_PROFILE_NAME,
            "edge_link": info.public_dict(),
            "components": components,
            "node": node,
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
            purge_result = self._purge_edge_root()
            self._rm_network()
        elif self.info_file.exists():
            self.info_file.unlink()
        return {
            "ok": all(item.get("ok") is not False for item in removed)
            and not (isinstance(purge_result, dict) and purge_result.get("ok") is False),
            "project": self.project,
            "profile": EDGE_LINK_PROFILE_NAME,
            "removed": removed,
            "purged": purge,
            "purge_result": purge_result,
        }

    def validate(
        self,
        *,
        timeout: float = 180.0,
        require_gpu_smoke: bool = True,
        **start_kwargs: Any,
    ) -> dict[str, Any]:
        started = self.start(timeout=timeout, **start_kwargs)
        info = self.load()
        status = self.status()
        checks: list[dict[str, Any]] = [
            {"name": "edge-link-start", "ok": bool(started.get("ok"))},
            {"name": "components-running", "ok": bool(status.get("running"))},
        ]
        if info:
            checks.append(self._controller_health_check(info))
            node_check = self._wait_node_check(info, timeout=min(timeout, 60.0))
            checks.append(node_check)
            if info.edge_cell_contract:
                checks.append(self._wait_compute_nodes_check(info, timeout=min(timeout, 60.0)))
            gpu_check = self._gpu_advertisement_check(info, node_check.get("node"))
            checks.append(gpu_check)
            if require_gpu_smoke:
                checks.append(self._gpu_smoke_check(info, node_check.get("node")))
        ok = all(bool(check.get("ok")) for check in checks)
        return {
            "ok": ok,
            "profile": EDGE_LINK_PROFILE_NAME,
            "started": started,
            "status": status,
            "checks": checks,
        }

    def load(self) -> K1sEdgeLinkInfo | None:
        if not self.info_file.is_file():
            return None
        try:
            data = json.loads(self.info_file.read_text(encoding="utf-8"))
            components = [
                K1sEdgeLinkComponent(**item)
                for item in data.get("components", [])
                if isinstance(item, dict)
            ]
            data = {key: value for key, value in data.items() if key != "components"}
            return K1sEdgeLinkInfo(**data, components=components)
        except Exception:
            return None

    @property
    def network(self) -> str:
        return containerd_network_name(self.state_root, self.project)

    @property
    def namespace(self) -> str:
        return containerd_namespace(self.state_root, self.project)

    @property
    def edge_dir(self) -> Path:
        return self.project_state / "edge-link"

    @property
    def info_file(self) -> Path:
        return self.edge_dir / EDGE_LINK_STATE_FILE

    @property
    def gateway_image(self) -> str:
        if self._gateway_image_override:
            return self._gateway_image_override
        return os.getenv(
            "WORKERBEE_EDGE_LINK_GATEWAY_IMAGE",
            f"workerbee-{self.project}-k1s-edge-gateway:dev",
        )

    @property
    def node_image(self) -> str:
        if self._node_image_override:
            return self._node_image_override
        return os.getenv(
            "WORKERBEE_EDGE_LINK_NODE_IMAGE",
            f"workerbee-{self.project}-k1s-edge-node:dev",
        )

    def _resolve_k1s_root(self) -> Path:
        if self.k1s_root is None:
            self.k1s_root = resolve_k1s_root(self.cwd)
        return self.k1s_root

    def _require_containerd(self) -> None:
        selected = resolve_runtime(self.runtime_requested)
        if selected != CONTAINERD_RUNTIME:
            raise WorkerBeeError(
                code="K1S_EDGE_LINK_REQUIRES_CONTAINERD",
                message="WorkerBee k1s edge-link requires explicit direct containerd runtime",
                details={"requested_runtime": self.runtime_requested, "selected_runtime": selected},
                remediation=(
                    "Start WorkerBee with `--runtime containerd --containerd-privilege "
                    "sudo-helper` before using edge-link commands."
                ),
            )

    def _ensure_layout(self) -> None:
        for rel in ("bin", "config", "data", "logs", "state"):
            (self.edge_dir / rel).mkdir(parents=True, exist_ok=True)

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

    def _write_helper_bridge(self) -> Path:
        path = self.edge_dir / "bin" / "workerbee-nerdctl"
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_helper_bridge_script(), encoding="utf-8")
            path.chmod(0o755)
        return path

    def _ensure_images(self, *, build: bool) -> list[dict[str, Any]]:
        if not build:
            return []
        root = self._resolve_k1s_root()
        builds: list[dict[str, Any]] = []
        for role, image, dockerfile in (
            ("gateway", self.gateway_image, root / "ops" / "images" / "gateway.Dockerfile"),
            ("node", self.node_image, root / "ops" / "images" / "node.Dockerfile"),
        ):
            try:
                proc = self._run_nerdctl(
                    ["build", "-t", image, "-f", str(dockerfile), str(root)],
                    timeout=900,
                )
                builds.append(
                    {
                        "ok": True,
                        "role": role,
                        "image": image,
                        "dockerfile": str(dockerfile),
                        "returncode": proc.returncode,
                        "stdout": proc.stdout,
                    }
                )
            except RuntimeError as exc:
                if os.getenv("WORKERBEE_EDGE_LINK_STRICT_BUILD", "0") == "1":
                    raise
                if role == "gateway":
                    self._gateway_image_override = DEFAULT_K1S_PYTHON_IMAGE
                else:
                    self._node_image_override = DEFAULT_K1S_PYTHON_IMAGE
                builds.append(
                    {
                        "ok": False,
                        "role": role,
                        "image": image,
                        "dockerfile": str(dockerfile),
                        "error": str(exc),
                        "fallback_image": DEFAULT_K1S_PYTHON_IMAGE,
                        "fallback": "source-mount",
                    }
                )
        return builds

    def _resolve_bootstrap(
        self,
        *,
        from_microk8s: bool,
        release: str,
        namespace: str,
        site_id: str,
        bundle: dict[str, Any] | str | None,
        bundle_path: str | Path | None,
        controller_url: str | None,
        agent_token: str | None,
        nats_leaf_addr: str | None,
        nats_leaf_url: str | None,
        rathole_server_addr: str | None,
        rathole_token: str | None,
        registry_host: str | None,
        stack_domain: str | None,
        wildcard_apps_domain: str | None,
        edge_local_addr: str | None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if from_microk8s:
            data.update(
                self._microk8s_bundle(
                    release=release,
                    namespace=namespace,
                    site_id=site_id,
                )
            )
        if bundle_path:
            data.update(_parse_bundle_text(Path(bundle_path).read_text(encoding="utf-8")))
        if bundle:
            data.update(bundle if isinstance(bundle, dict) else _parse_bundle_text(bundle))
        manual = {
            "controller_url": controller_url,
            "agent_token": agent_token,
            "nats_leaf_addr": nats_leaf_addr,
            "nats_leaf_url": nats_leaf_url,
            "rathole_server_addr": rathole_server_addr,
            "rathole_token": rathole_token,
            "registry_host": registry_host,
            "stack_domain": stack_domain,
            "wildcard_apps_domain": wildcard_apps_domain,
            "site_id": site_id,
            "edge_local_addr": edge_local_addr,
        }
        data.update({key: value for key, value in manual.items() if value not in (None, "")})
        edge_env = data.get("suggested_edge_env")
        if isinstance(edge_env, dict):
            data.setdefault("controller_url", edge_env.get("AE_CONTROLLER_URL"))
            data.setdefault("agent_token", edge_env.get("AE_AGENT_TOKEN"))
            data.setdefault("site_id", edge_env.get("AE_SITE_ID"))
            data.setdefault("nats_leaf_addr", edge_env.get("K1S_NATS_LEAF_ADDR"))
            data.setdefault("nats_leaf_url", edge_env.get("K1S_NATS_LEAF_URL"))
            data.setdefault("rathole_server_addr", edge_env.get("AE_RATHOLE_SERVER_ADDR"))
            data.setdefault("rathole_token", edge_env.get("AE_RATHOLE_DEFAULT_TOKEN"))
            data.setdefault("registry_host", edge_env.get("AE_REGISTRY_HOST"))
            data.setdefault("stack_domain", edge_env.get("K1S_STACK_DOMAIN"))
            data.setdefault("wildcard_apps_domain", edge_env.get("K1S_WILDCARD_APPS_DOMAIN"))
            data.setdefault("edge_local_addr", edge_env.get("AE_EDGE_INGRESS_LOCAL_ADDR"))
        required = ("controller_url", "agent_token", "nats_leaf_addr", "rathole_server_addr")
        missing = sorted(key for key in required if not str(data.get(key) or "").strip())
        if missing:
            raise WorkerBeeError(
                code="EDGE_LINK_BOOTSTRAP_REQUIRED",
                message="edge-link bootstrap is missing required external-core fields",
                details={"missing": missing, "bootstrap": _mask_sensitive(data)},
                remediation=(
                    "Use from_microk8s=true/--from-microk8s, pass bundle_path/--bundle, "
                    "or provide controller_url, agent_token, and nats_leaf_addr."
                ),
            )
        write_private_json(self.edge_dir / "bootstrap.json", data)
        return data

    def _microk8s_bundle(self, *, release: str, namespace: str, site_id: str) -> dict[str, Any]:
        script = self._resolve_k1s_root() / "scripts" / "dev" / "microk8s_stack_bundle.py"
        if not script.is_file():
            raise FileNotFoundError(script)
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--from-kube",
                "--release",
                release,
                "--namespace",
                namespace,
                "--site-id",
                site_id,
                "--format",
                "json",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        if proc.returncode != 0:
            raise WorkerBeeError(
                code="EDGE_LINK_MICROK8S_BUNDLE_FAILED",
                message="failed to generate MicroK8s k1s edge bootstrap bundle",
                details={"returncode": proc.returncode, "stdout": proc.stdout},
                retryable=True,
            )
        data = json.loads(proc.stdout)
        endpoints = self._microk8s_rathole_server_addrs(
            kubectl_bin=os.getenv("KUBECTL_BIN", "kubectl"),
            release=release,
            namespace=namespace,
            port=_port_from_addr(str(data.get("rathole_server_addr") or ""), 2333),
        )
        if endpoints:
            data["rathole_server_addrs"] = endpoints
        return data

    def _microk8s_rathole_server_addrs(
        self,
        *,
        kubectl_bin: str,
        release: str,
        namespace: str,
        port: int,
    ) -> list[str]:
        service_name = f"{release}-k1s-core-ha-rathole"
        proc = subprocess.run(
            [
                kubectl_bin,
                "-n",
                namespace,
                "get",
                "endpointslice",
                "-l",
                f"kubernetes.io/service-name={service_name}",
                "-o",
                "json",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        if proc.returncode != 0:
            return []
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        addrs: list[str] = []
        for item in payload.get("items") or []:
            for endpoint in item.get("endpoints") or []:
                conditions = endpoint.get("conditions") or {}
                if conditions.get("ready") is False:
                    continue
                addresses = endpoint.get("addresses") or []
                if not addresses:
                    continue
                host = str(addresses[0] or "").strip()
                if host:
                    addrs.append(f"{host}:{int(port)}")
        return _dedupe(addrs)

    def _start_edge_nats(
        self,
        *,
        site_id: str,
        leaf_url: str,
        leaf_addr: str,
        host_port: int,
        http_port: int,
    ) -> K1sEdgeLinkComponent:
        name = self._component_name("edge-nats")
        data = self.edge_dir / "data" / "nats"
        data.mkdir(parents=True, exist_ok=True)
        config = self._write_nats_config(
            site_id=site_id,
            leaf_url=leaf_url or f"nats://{leaf_addr}",
            port=4223,
            http_port=8223,
        )
        self._rm_container(name)
        image = os.getenv("WORKERBEE_EDGE_LINK_NATS_IMAGE", DEFAULT_NATS_IMAGE)
        args = [
            "run",
            "-d",
            "--name",
            name,
            *self._restart_policy_args(),
            "--network",
            self.network,
            "-p",
            f"127.0.0.1:{host_port}:4223",
            "-p",
            f"127.0.0.1:{http_port}:8223",
            "-v",
            f"{data}:/data",
            "-v",
            f"{config}:/etc/nats/nats-edge.conf:ro",
            *self._external_host_args({"nats_leaf_addr": leaf_addr}),
            *self._label_args("edge-nats"),
            image,
            "-c",
            "/etc/nats/nats-edge.conf",
        ]
        return self._run_component(
            name=name,
            role="edge-nats",
            image=image,
            args=args,
            host_port=host_port,
            container_port=4223,
            url=f"nats://127.0.0.1:{host_port}",
        )

    def _start_rathole_client(
        self,
        *,
        site_id: str,
        server_addr: str,
        token: str,
        edge_local_addr: str,
        index: int | None = None,
    ) -> K1sEdgeLinkComponent:
        component = "rathole" if index is None else f"rathole-{index}"
        name = self._component_name(component)
        config = self._write_rathole_config(
            site_id=site_id,
            server_addr=server_addr,
            token=token,
            edge_local_addr=edge_local_addr,
            index=index,
        )
        self._rm_container(name)
        image = os.getenv("WORKERBEE_EDGE_LINK_RATHOLE_IMAGE", DEFAULT_RATHOLE_IMAGE)
        args = [
            "run",
            "-d",
            "--name",
            name,
            *self._restart_policy_args(),
            "--network",
            os.getenv("WORKERBEE_EDGE_LINK_RATHOLE_NETWORK", "host"),
            "-v",
            f"{config}:/etc/rathole/client.toml:ro",
            *self._external_host_args({"rathole_server_addr": server_addr}),
            *self._label_args("rathole-client"),
            image,
            "--client",
            "/etc/rathole/client.toml",
        ]
        return self._run_component(name=name, role="rathole-client", image=image, args=args)

    def _start_gateway(
        self,
        *,
        site_id: str,
        node_id: str,
        bootstrap: dict[str, Any],
        edge_local_addr: str,
    ) -> K1sEdgeLinkComponent:
        name = self._component_name("gateway")
        self._rm_container(name)
        data = self.edge_dir / "data" / "gateway"
        data.mkdir(parents=True, exist_ok=True)
        env = self._gateway_env(
            site_id=site_id,
            node_id=node_id,
            bootstrap=bootstrap,
            edge_local_addr=edge_local_addr,
        )
        command = (
            "cd /workspace && "
            "python -m pip install --no-cache-dir -r /workspace/requirements.txt "
            ">/tmp/k1s-pip-install.log 2>&1 && "
            "exec python -m ae.gateway"
        )
        args = [
            "run",
            "-d",
            "--name",
            name,
            *self._restart_policy_args(),
            "--network",
            self.network,
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{data}:/var/lib/ae",
            *self._source_mount_args(),
            *self._external_host_args(bootstrap),
            *self._env_args(env),
            *self._label_args("gateway"),
            self.gateway_image,
            "-ec",
            command,
        ]
        return self._run_component(name=name, role="gateway", image=self.gateway_image, args=args)

    def _start_node(
        self,
        *,
        site_id: str,
        node_id: str,
        bootstrap: dict[str, Any],
        agent_endpoint: str,
        host_port: int,
        gpu: dict[str, Any],
        component: str = "node",
        role_label: str = "gateway",
    ) -> K1sEdgeLinkComponent:
        name = self._component_name(component)
        self._rm_container(name)
        data = self.edge_dir / "data" / component
        run_dir = self.edge_dir / "run" / component
        data.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        env = self._node_env(
            site_id=site_id,
            node_id=node_id,
            bootstrap=bootstrap,
            agent_endpoint=agent_endpoint,
            gpu=gpu,
            role_label=role_label,
        )
        command = (
            "cd /workspace && "
            "python -m pip install --no-cache-dir -r /workspace/requirements.txt "
            ">/tmp/k1s-pip-install.log 2>&1 && "
            "exec python -m ae.node --runtime-backend containerd --host 0.0.0.0 "
            '--port 9109 --controller-url "$AE_CONTROLLER_URL" '
            '--advertise-endpoint "$AE_AGENT_ENDPOINT"'
        )
        args = [
            "run",
            "-d",
            "--name",
            name,
            *self._restart_policy_args(),
            "--network",
            self.network,
            "-p",
            f"0.0.0.0:{host_port}:9109",
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{data}:/var/lib/ae",
            "-v",
            f"{run_dir}:/var/run/ae",
            *self._source_mount_args(),
            *self._state_mount_args(),
            *self._nvidia_mount_args(gpu),
            *self._external_host_args(bootstrap),
            *self._env_args(env),
            *self._label_args(component),
            self.node_image,
            "-ec",
            command,
        ]
        return self._run_component(
            name=name,
            role=component,
            image=self.node_image,
            args=args,
            host_port=host_port,
            container_port=9109,
            url=agent_endpoint,
        )

    def _gateway_env(
        self,
        *,
        site_id: str,
        node_id: str,
        bootstrap: dict[str, Any],
        edge_local_addr: str,
    ) -> dict[str, str]:
        env = self._bootstrap_env(bootstrap)
        env.update(
            {
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": "/workspace/src",
                "AE_SITE_ID": site_id,
                "AE_NODE_ID": node_id,
                "EDGE_PROFILE": "k1s-ha-core",
                "EDGE_INGRESS_MODE": "core-proxy",
                "AE_TRANSPORT_BACKEND": "nats-js",
                "AE_JS_DOMAIN": "K1S",
                "AE_NATS_URL": f"nats://gateway:dev@{self._component_name('edge-nats')}:4223",
                "AE_GATEWAY_SPOOL_PATH": "/var/lib/ae/gateway.db",
                "AE_EDGE_INGRESS_LOCAL_ADDR": edge_local_addr,
                "EDGE_START_WORKER": "0",
            }
        )
        env.update(secret_env_for_project(self.project_state))
        return env

    def _node_env(
        self,
        *,
        site_id: str,
        node_id: str,
        bootstrap: dict[str, Any],
        agent_endpoint: str,
        gpu: dict[str, Any],
        role_label: str = "gateway",
    ) -> dict[str, str]:
        project_data_root = containerd_data_root(self.state_root, project=self.project)
        project_cni_conf = containerd_cni_conf_dir(self.state_root, project=self.project)
        system_data_root = containerd_data_root(self.state_root, system=True)
        system_cni_conf = containerd_cni_conf_dir(self.state_root, system=True)
        env = self._bootstrap_env(bootstrap)
        env.update(
            {
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": "/workspace/src",
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
                "AE_CONTAINER_CLI": str(self.edge_dir / "bin" / "workerbee-nerdctl"),
                "AE_NERDCTL_BIN": str(self.edge_dir / "bin" / "workerbee-nerdctl"),
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
                "AE_NODE_ID": node_id,
                "AE_NODE_NAME": node_id,
                "AE_NODE_LABELS": (
                    f"role={role_label},compute_eligible=true,profile={EDGE_LINK_PROFILE_NAME},"
                    f"site={site_id},site_id={site_id},node_id={node_id}"
                ),
                "AE_AGENT_ENDPOINT": agent_endpoint,
                "AE_NATS_URL": f"nats://worker:dev@{self._component_name('edge-nats')}:4223",
            }
        )
        env.update(self._nvidia_env(gpu))
        env.update(secret_env_for_project(self.project_state))
        return env

    def _bootstrap_env(self, bootstrap: dict[str, Any]) -> dict[str, str]:
        env: dict[str, str] = {}
        suggested = bootstrap.get("suggested_edge_env")
        if isinstance(suggested, dict):
            env.update(
                {str(key): str(value) for key, value in suggested.items() if value is not None}
            )
        mapping = {
            "AE_CONTROLLER_URL": bootstrap.get("controller_url"),
            "AE_AGENT_TOKEN": bootstrap.get("agent_token"),
            "K1S_NATS_LEAF_ADDR": bootstrap.get("nats_leaf_addr"),
            "K1S_NATS_LEAF_URL": bootstrap.get("nats_leaf_url"),
            "AE_RATHOLE_SERVER_ADDR": bootstrap.get("rathole_server_addr"),
            "AE_RATHOLE_DEFAULT_TOKEN": bootstrap.get("rathole_token"),
            "AE_REGISTRY_HOST": bootstrap.get("registry_host"),
            "K1S_STACK_DOMAIN": bootstrap.get("stack_domain"),
            "K1S_WILDCARD_APPS_DOMAIN": bootstrap.get("wildcard_apps_domain"),
        }
        env.update({key: str(value) for key, value in mapping.items() if value not in (None, "")})
        return env

    def _restart_policy_args(self) -> list[str]:
        return ["--restart", "unless-stopped"]

    def _write_nats_config(
        self,
        *,
        site_id: str,
        leaf_url: str,
        port: int,
        http_port: int,
    ) -> Path:
        src = self._resolve_k1s_root() / "ops" / "dev" / "nats-edge.conf"
        if src.is_file():
            text = src.read_text(encoding="utf-8")
            text = text.replace("sfo-edge-01", site_id)
            text = text.replace("edge-sfo-01", f"edge-{site_id}")
            text = re.sub(r'url:\s*"[^"]+"', f'url: "{leaf_url}"', text, count=1)
            text = re.sub(r"(?m)^port:\s*\d+\s*$", f"port: {port}", text)
            text = re.sub(r"(?m)^http:\s*\d+\s*$", f"http: {http_port}", text)
        else:
            text = textwrap.dedent(
                f"""\
                server_name: "edge-{site_id}"
                port: {port}
                http: {http_port}
                leafnodes {{
                  remotes = [{{ url: "{leaf_url}" }}]
                }}
                authorization {{
                  users: [
                    {{ user: "gateway", password: "dev" }}
                    {{ user: "worker", password: "dev" }}
                  ]
                }}
                """
            )
        path = self.edge_dir / "config" / "nats-edge.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _write_rathole_config(
        self,
        *,
        site_id: str,
        server_addr: str,
        token: str,
        edge_local_addr: str,
        index: int | None = None,
    ) -> Path:
        name = "rathole-client" if index is None else f"rathole-client-{index}"
        path = self.edge_dir / "config" / f"{name}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = textwrap.dedent(
            f"""\
            [client]
            remote_addr = "{server_addr}"
            default_token = "{token}"

            [client.services]
            [client.services."{site_id}"]
            local_addr = "{edge_local_addr}"
            """
        )
        path.write_text(content, encoding="utf-8")
        return path

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
    ) -> K1sEdgeLinkComponent:
        proc = self._run_nerdctl(args, timeout=180)
        container_id = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None
        return K1sEdgeLinkComponent(
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

    def _component_status(self, component: K1sEdgeLinkComponent) -> dict[str, Any]:
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

    def _all_components_running(self, info: K1sEdgeLinkInfo) -> bool:
        if not info.components:
            return False
        return all(
            bool(self._component_status(component).get("running")) for component in info.components
        )

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

    def _write_info(self, info: K1sEdgeLinkInfo) -> None:
        write_private_json(self.info_file, asdict(info))

    def _purge_edge_root(self) -> dict[str, Any]:
        if not self.edge_dir.exists():
            return {"ok": True, "removed": False, "path": str(self.edge_dir)}
        result = remove_containerd_helper_tree(self.state_root, self.edge_dir)
        if result.get("ok"):
            return result
        try:
            shutil.rmtree(self.edge_dir)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "removed": False,
                "path": str(self.edge_dir),
                "helper": result,
                "error": str(exc),
            }
        return {"ok": True, "removed": True, "path": str(self.edge_dir), "helper": result}

    def _label_args(self, component: str) -> list[str]:
        args: list[str] = []
        labels = [
            *workerbee_runtime_labels(state_root=self.state_root, project=self.project),
            "workerbee.component=k1s-edge-link",
            f"workerbee.k1s_edge_link_component={component}",
        ]
        for label in labels:
            args.extend(["--label", label])
        return args

    def _env_args(self, env: dict[str, str]) -> list[str]:
        args: list[str] = []
        for key, value in sorted(env.items()):
            args.extend(["-e", f"{key}={value}"])
        return args

    def _state_mount_args(self) -> list[str]:
        mounts = [
            f"{self.state_root}:{self.state_root}",
            f"{self.edge_dir / 'bin'}:{self.edge_dir / 'bin'}",
        ]
        helper_socket_mount = self._helper_socket_mount()
        if helper_socket_mount:
            mounts.append(helper_socket_mount)
        args: list[str] = []
        for mount in mounts:
            args.extend(["-v", mount])
        return args

    def _source_mount_args(self) -> list[str]:
        return ["-v", f"{self._resolve_k1s_root()}:/workspace:ro"]

    def _external_host_args(self, bootstrap: dict[str, Any]) -> list[str]:
        hosts: set[str] = set()
        controller_url = str(bootstrap.get("controller_url") or "").strip()
        if controller_url:
            parsed = urlsplit(
                controller_url if "://" in controller_url else f"http://{controller_url}"
            )
            if parsed.hostname:
                hosts.add(parsed.hostname)
        for key in ("nats_leaf_addr", "rathole_server_addr", "registry_host"):
            host = _split_host_port(str(bootstrap.get(key) or ""))
            if host:
                hosts.add(host)
        args: list[str] = []
        for host in sorted(hosts):
            if _is_ip_literal(host):
                continue
            try:
                address = socket.gethostbyname(host)
            except OSError:
                continue
            args.extend(["--add-host", f"{host}:{address}"])
        return args

    def _helper_socket_mount(self) -> str | None:
        raw = os.getenv("WORKERBEE_CONTAINERD_HELPER_SOCKET")
        if not raw:
            return None
        socket_path = Path(raw).expanduser().resolve()
        if not socket_path.exists() or socket_path.is_relative_to(self.state_root):
            return None
        return f"{socket_path}:{socket_path}"

    def _component_name(self, role: str) -> str:
        return f"{self.network}-{EDGE_LINK_PROFILE_NAME}-{role}"[:120].rstrip("-")

    def _infer_advertise_host(self) -> str | None:
        raw = os.getenv("WORKERBEE_EDGE_LINK_ADVERTISE_HOST")
        if raw:
            return raw
        for value in (
            os.getenv("WORKERBEE_INGRESS_BASE_DOMAIN"),
            os.getenv("WORKERBEE_BASE_DOMAIN"),
        ):
            host = _host_from_domain(value)
            if host:
                return host
        return _default_route_host()

    def _detect_nvidia(self) -> dict[str, Any]:
        smi = shutil.which("nvidia-smi")
        cli = shutil.which("nvidia-container-cli")
        runtime = shutil.which("nvidia-container-runtime")
        result: dict[str, Any] = {
            "present": False,
            "nvidia_smi": smi,
            "nvidia_container_cli": cli,
            "nvidia_container_runtime": runtime,
            "runtime_config_dir": "/etc/nvidia-container-runtime"
            if Path("/etc/nvidia-container-runtime").is_dir()
            else "",
            "toolkit_dir": "/usr/local/nvidia/toolkit"
            if Path("/usr/local/nvidia/toolkit").is_dir()
            else "",
        }
        if smi:
            proc = subprocess.run(
                [smi, "-L"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            result.update(
                {
                    "present": proc.returncode == 0,
                    "returncode": proc.returncode,
                    "summary": proc.stdout.strip(),
                }
            )
        return result

    def _nvidia_env(self, gpu: dict[str, Any]) -> dict[str, str]:
        env: dict[str, str] = {}
        if gpu.get("nvidia_smi"):
            env["AE_NVIDIA_SMI_BIN"] = str(gpu["nvidia_smi"])
        if gpu.get("nvidia_container_cli"):
            env["AE_NVIDIA_CONTAINER_CLI_BIN"] = str(gpu["nvidia_container_cli"])
        if gpu.get("nvidia_container_runtime"):
            env["AE_NVIDIA_CONTAINER_RUNTIME_BIN"] = str(gpu["nvidia_container_runtime"])
        if gpu.get("runtime_config_dir"):
            env["AE_NVIDIA_RUNTIME_CONFIG_DIR"] = str(gpu["runtime_config_dir"])
        if gpu.get("toolkit_dir"):
            env["AE_NVIDIA_TOOLKIT_DIR"] = str(gpu["toolkit_dir"])
        if _nvidia_library_files():
            env["AE_NVIDIA_LIBRARY_DIRS"] = "/var/lib/ae/nvidia-libs"
            env["LD_LIBRARY_PATH"] = "/var/lib/ae/nvidia-libs"
        return env

    def _nvidia_mount_args(self, gpu: dict[str, Any]) -> list[str]:
        paths = [
            gpu.get("nvidia_smi"),
            gpu.get("nvidia_container_cli"),
            gpu.get("nvidia_container_runtime"),
            gpu.get("runtime_config_dir"),
            gpu.get("toolkit_dir"),
        ]
        args: list[str] = []
        seen: set[str] = set()
        for raw in paths:
            if not raw:
                continue
            path = Path(str(raw))
            if not path.exists():
                continue
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            args.extend(["-v", f"{resolved}:{resolved}:ro"])
        lib_dir = self._prepare_nvidia_library_dir()
        if lib_dir:
            args.extend(["-v", f"{lib_dir}:/var/lib/ae/nvidia-libs:ro"])
        return args

    def _prepare_nvidia_library_dir(self) -> Path | None:
        files = _nvidia_library_files()
        if not files:
            return None
        target = self.edge_dir / "nvidia-libs"
        target.mkdir(parents=True, exist_ok=True)
        for existing in target.iterdir():
            if existing.is_file() or existing.is_symlink():
                existing.unlink()
        for src in files:
            dest = target / src.name
            try:
                shutil.copy2(src, dest, follow_symlinks=True)
            except OSError:
                continue
        return target if any(target.iterdir()) else None

    def _wait_ready(self, info: K1sEdgeLinkInfo, *, timeout: float) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.status()
            if status.get("running"):
                break
            time.sleep(1.0)
        controller_check = self._wait_controller_health_check(
            info,
            timeout=max(1.0, deadline - time.time()),
        )
        checks = [
            {"name": "components-running", "ok": bool(self.status().get("running"))},
            controller_check,
            self._wait_node_check(
                info,
                timeout=max(1.0, deadline - time.time()),
                fresh_after=info.started_at,
            ),
        ]
        if info.edge_cell_contract:
            checks.append(
                self._wait_compute_nodes_check(
                    info,
                    timeout=max(1.0, deadline - time.time()),
                    fresh_after=info.started_at,
                )
            )
        return {"ok": all(bool(check.get("ok")) for check in checks), "checks": checks}

    def _wait_controller_health_check(
        self,
        info: K1sEdgeLinkInfo,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            last = self._controller_health_check(info)
            if last.get("ok"):
                return last
            time.sleep(2.0)
        return last or {"name": "external-controller-health", "ok": False, "error": "timeout"}

    def _controller_health_check(self, info: K1sEdgeLinkInfo) -> dict[str, Any]:
        for path in ("/healthz", "/health"):
            try:
                resp = request(f"{info.controller_url.rstrip('/')}{path}", timeout=5.0)
                if resp.status == 200:
                    return {"name": "external-controller-health", "ok": True, "path": path}
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            else:
                last_error = f"status={resp.status}"
        return {"name": "external-controller-health", "ok": False, "error": last_error}

    def _wait_node_check(
        self,
        info: K1sEdgeLinkInfo,
        *,
        timeout: float,
        fresh_after: float | None = None,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            last = self._node_record(info)
            if last and self._node_record_is_fresh(last, fresh_after=fresh_after):
                return {"name": "edge-node-heartbeat", "ok": True, "node": last}
            time.sleep(2.0)
        return {"name": "edge-node-heartbeat", "ok": False, "node": last}

    def _node_record_is_fresh(
        self,
        node: dict[str, Any],
        *,
        fresh_after: float | None,
    ) -> bool:
        if fresh_after is None:
            return True
        seen_at = _parse_iso_timestamp(node.get("seen_at"))
        return seen_at is not None and seen_at >= fresh_after

    def _node_record(self, info: K1sEdgeLinkInfo) -> dict[str, Any] | None:
        return self._node_records(info).get(info.node_id)

    def _node_records(self, info: K1sEdgeLinkInfo) -> dict[str, dict[str, Any]]:
        try:
            resp = request(f"{info.controller_url.rstrip('/')}/v1/nodes", timeout=5.0)
            if resp.status != 200:
                return {}
            data = resp.json()
        except Exception:
            return {}
        nodes = data.get("nodes") if isinstance(data, dict) else []
        if not isinstance(nodes, list):
            return {}
        records: dict[str, dict[str, Any]] = {}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("node_id") or "")
            if node_id:
                records[node_id] = node
        return records

    def _wait_compute_nodes_check(
        self,
        info: K1sEdgeLinkInfo,
        *,
        timeout: float,
        fresh_after: float | None = None,
    ) -> dict[str, Any]:
        expected = list((info.edge_cell_contract or {}).get("compute_node_ids") or [])
        deadline = time.time() + timeout
        observed: dict[str, dict[str, Any]] = {}
        while time.time() < deadline:
            records = self._node_records(info)
            observed = {
                node_id: records[node_id]
                for node_id in expected
                if node_id in records
                and self._node_record_is_fresh(records[node_id], fresh_after=fresh_after)
            }
            if len(observed) == len(expected):
                break
            time.sleep(2.0)
        missing = [node_id for node_id in expected if node_id not in observed]
        return {
            "name": "edge-cell-compute-node-heartbeats",
            "ok": not missing,
            "expected_node_ids": expected,
            "observed_node_ids": sorted(observed),
            "missing_node_ids": missing,
        }

    def _gpu_advertisement_check(
        self,
        info: K1sEdgeLinkInfo,
        node: Any,
    ) -> dict[str, Any]:
        gpu = info.gpu or {}
        if not gpu.get("present"):
            return {"name": "gpu-advertised", "ok": False, "error": "host NVIDIA GPU not detected"}
        if not isinstance(node, dict):
            return {"name": "gpu-advertised", "ok": False, "error": "node heartbeat not observed"}
        count = node.get("gpu_count") or (node.get("labels") or {}).get("gpu.count")
        try:
            count_int = int(count or 0)
        except Exception:
            count_int = 0
        return {
            "name": "gpu-advertised",
            "ok": count_int >= 1,
            "gpu_count": count_int,
            "gpu_models": node.get("gpu_models") or (node.get("labels") or {}).get("gpu.models"),
        }

    def _gpu_smoke_check(self, info: K1sEdgeLinkInfo, node: Any) -> dict[str, Any]:
        if not (info.gpu or {}).get("present"):
            return {"name": "gpu-smoke", "ok": False, "error": "host NVIDIA GPU not detected"}
        if not isinstance(node, dict):
            return {"name": "gpu-smoke", "ok": False, "error": "node heartbeat not observed"}
        manifest = _gpu_smoke_manifest(info.node_id)
        try:
            resp = request(
                f"{info.agent_endpoint.rstrip('/')}/v1/ensure_app",
                method="POST",
                json_body={
                    "manifest": manifest,
                    "revision": int(time.time()),
                    "node_id": info.node_id,
                    "pod_names": [f"{manifest['metadata']['name']}-0"],
                },
                timeout=60.0,
            )
            body = resp.json() if resp.body else {}
        except Exception as exc:  # noqa: BLE001
            return {"name": "gpu-smoke", "ok": False, "error": str(exc)}
        pod_states = body.get("pod_states") if isinstance(body, dict) else []
        pod_name = ""
        if isinstance(pod_states, list) and pod_states:
            first = pod_states[0]
            if isinstance(first, dict):
                pod_name = str(first.get("pod_name") or "")
        log_text = ""
        if pod_name:
            try:
                logs = request(
                    f"{info.agent_endpoint.rstrip('/')}/v1/logs?pod_name={pod_name}&tail=80",
                    timeout=20.0,
                )
                log_body = logs.json() if logs.body else {}
                if isinstance(log_body, dict):
                    log_text = "\n".join(str(line) for line in log_body.get("lines") or [])
            except Exception:
                log_text = ""
        ok = resp.status < 400 and (
            "NVIDIA-SMI" in log_text or any(bool(item.get("ready")) for item in pod_states)
        )
        return {
            "name": "gpu-smoke",
            "ok": ok,
            "status": resp.status,
            "pod_name": pod_name,
            "response": body,
            "log_contains_nvidia_smi": "NVIDIA-SMI" in log_text,
        }


def _parse_bundle_text(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    data: dict[str, Any] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        data[key.strip()] = value.strip().strip("'\"")
    if data:
        return {"suggested_edge_env": data}
    return {}


def _mask_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).lower()
            credential_url = lowered.endswith("url") and "@" in str(item or "")
            if credential_url or any(
                part in lowered for part in ("token", "password", "secret", "key")
            ):
                result[str(key)] = "***" if item else item
            else:
                result[str(key)] = _mask_sensitive(item)
        return result
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    return value


def _missing_container(output: str) -> bool:
    lowered = output.lower()
    return "no such container" in lowered or "not found" in lowered


def _host_from_domain(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or raw
    match = re.match(r"^(\d+)-(\d+)-(\d+)-(\d+)\.sslip\.io$", host)
    if match:
        return ".".join(match.groups())
    if host and not host.startswith("127.") and host != "localhost":
        return host
    return None


def _default_route_host() -> str | None:
    proc = subprocess.run(
        ["sh", "-c", "ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}'"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    host = proc.stdout.strip()
    if host and host != "127.0.0.1":
        return host
    return None


def _nvidia_library_files() -> list[Path]:
    candidates = [
        "/usr/local/nvidia/lib64",
        "/usr/lib/x86_64-linux-gnu",
        "/usr/lib/aarch64-linux-gnu",
    ]
    result: list[Path] = []
    seen: set[str] = set()
    for raw in candidates:
        path = Path(raw)
        if not path.is_dir():
            continue
        for pattern in ("libnvidia-ml.so*", "libcuda.so*"):
            for item in path.glob(pattern):
                if not item.exists() or item.is_dir():
                    continue
                key = item.name
                if key in seen:
                    continue
                seen.add(key)
                result.append(item)
    return result


def _split_host_port(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"tcp://{raw}")
    return parsed.hostname or ""


def _port_from_addr(value: str, default_port: int) -> int:
    raw = str(value or "").strip()
    if not raw:
        return int(default_port)
    parsed = urlsplit(raw if "://" in raw else f"tcp://{raw}")
    return int(parsed.port or default_port)


def _rathole_server_addrs(bootstrap: dict[str, Any]) -> list[str]:
    raw = bootstrap.get("rathole_server_addrs")
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, list):
        values = [str(item or "").strip() for item in raw]
    else:
        values = []
    fallback = str(bootstrap.get("rathole_server_addr") or "").strip()
    if fallback and not any(values):
        values.append(fallback)
    return _dedupe([item for item in values if item])


def _edge_local_addr(explicit: str | None, bootstrap: dict[str, Any]) -> str:
    for value in (
        explicit,
        bootstrap.get("edge_local_addr"),
        os.getenv("WORKERBEE_EDGE_LINK_LOCAL_ADDR"),
        DEFAULT_EDGE_LOCAL_ADDR,
    ):
        text = str(value or "").strip()
        if text:
            return text
    return DEFAULT_EDGE_LOCAL_ADDR


def _normalize_cell_node_count(value: int) -> int:
    count = int(value or 0)
    if count in {0, AI_MAX_EDGE_CELL_NODE_COUNT}:
        return count
    raise WorkerBeeError(
        code="K1S_EDGE_CELL_UNSUPPORTED_SIZE",
        message="k1s edge-cell simulation supports legacy mode or exactly 3 cell nodes",
        details={
            "cell_node_count": count,
            "supported_cell_node_counts": [0, AI_MAX_EDGE_CELL_NODE_COUNT],
            "edge_cell_size": 1 + AI_MAX_EDGE_CELL_NODE_COUNT,
            "profile": AI_MAX_EDGE_CELL_PROFILE,
        },
        remediation=(
            "Omit cell_node_count for legacy one-node edge-link behavior, or pass "
            "cell_node_count=3/--cell-node-count 3 for the AI Max edge-cell simulation."
        ),
    )


def _normalize_fabric_cell_count(value: int) -> int:
    count = 1 if value is None else int(value)
    if count in SUPPORTED_AI_MAX_FABRIC_CELL_COUNTS:
        return count
    raise WorkerBeeError(
        code="K1S_EDGE_FABRIC_UNSUPPORTED_CELL_COUNT",
        message="k1s edge-cell fabric simulation supports 1, 2, 4, or 8 cells",
        details={
            "fabric_cell_count": count,
            "supported_fabric_cell_counts": sorted(SUPPORTED_AI_MAX_FABRIC_CELL_COUNTS),
            "edge_cell_size": AI_MAX_EDGE_CELL_SIZE,
            "profile": AI_MAX_EDGE_CELL_PROFILE,
        },
        remediation="Pass --fabric-cell-count with one of 1, 2, 4, or 8.",
    )


def _normalize_lan_scope(value: str | None) -> str:
    scope = DEFAULT_EDGE_LAN_SCOPE if value is None else str(value).strip()
    if scope:
        return scope
    raise WorkerBeeError(
        code="K1S_EDGE_FABRIC_LAN_SCOPE_REQUIRED",
        message="k1s edge-cell fabric simulation requires a non-empty LAN scope",
        details={"lan_scope": value, "profile": AI_MAX_EDGE_CELL_PROFILE},
        remediation="Pass --lan-scope with a stable local discovery scope.",
    )


def _validate_edge_cell_fabric_shape(cell_node_count: int, fabric_cell_count: int) -> None:
    if fabric_cell_count == 1 or cell_node_count == AI_MAX_EDGE_CELL_NODE_COUNT:
        return
    raise WorkerBeeError(
        code="K1S_EDGE_FABRIC_REQUIRES_EDGE_CELL",
        message="multi-cell fabric simulation requires the AI Max four-node edge-cell shape",
        details={
            "cell_node_count": cell_node_count,
            "required_cell_node_count": AI_MAX_EDGE_CELL_NODE_COUNT,
            "fabric_cell_count": fabric_cell_count,
            "profile": AI_MAX_EDGE_CELL_PROFILE,
        },
        remediation="Pass --cell-node-count 3 together with --fabric-cell-count for fabric tests.",
    )


def _edge_cell_contract(
    *,
    gateway_node_id: str,
    site_id: str,
    advertise_host: str,
    gateway_agent_port: int,
    cell_agent_ports: list[int],
    fabric_cell_count: int,
    lan_scope: str,
) -> dict[str, Any]:
    if not cell_agent_ports:
        return {}
    expected_agent_ports = fabric_cell_count * AI_MAX_EDGE_CELL_SIZE - 1
    if len(cell_agent_ports) != expected_agent_ports:
        raise WorkerBeeError(
            code="K1S_EDGE_FABRIC_PORT_ALLOCATION_MISMATCH",
            message="k1s edge-cell fabric simulation did not allocate the expected node ports",
            details={
                "allocated_agent_ports": len(cell_agent_ports),
                "expected_agent_ports": expected_agent_ports,
                "fabric_cell_count": fabric_cell_count,
                "profile": AI_MAX_EDGE_CELL_PROFILE,
            },
        )

    members: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    all_cell_node_ids: list[str] = []
    port_iter = iter(cell_agent_ports)

    def member(
        *,
        node_id: str,
        role: str,
        component: str,
        port: int,
        cell_index: int,
    ) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "role": role,
            "compute_eligible": True,
            "component": component,
            "agent_host_port": int(port),
            "agent_endpoint": f"http://{advertise_host}:{int(port)}",
            "cell_index": cell_index,
            "labels": {
                "role": role,
                "compute_eligible": "true",
                "site_id": site_id,
                "lan_scope": lan_scope,
                "cell_index": str(cell_index),
            },
        }

    for cell_index in range(1, fabric_cell_count + 1):
        cell_gateway_node_id = (
            gateway_node_id if cell_index == 1 else f"{gateway_node_id}-gateway-{cell_index}"
        )
        gateway_component = "node" if cell_index == 1 else f"cell-{cell_index}-gateway"
        gateway_port = gateway_agent_port if cell_index == 1 else next(port_iter)
        cell_members = [
            member(
                node_id=cell_gateway_node_id,
                role="gateway",
                component=gateway_component,
                port=gateway_port,
                cell_index=cell_index,
            )
        ]
        cell_node_ids: list[str] = []
        for node_index in range(1, AI_MAX_EDGE_CELL_NODE_COUNT + 1):
            cell_node_id = (
                f"{gateway_node_id}-cell-{node_index}"
                if cell_index == 1
                else f"{cell_gateway_node_id}-cell-{node_index}"
            )
            component = (
                f"cell-node-{node_index}"
                if cell_index == 1
                else f"cell-{cell_index}-node-{node_index}"
            )
            cell_node_ids.append(cell_node_id)
            all_cell_node_ids.append(cell_node_id)
            cell_members.append(
                member(
                    node_id=cell_node_id,
                    role="cell-node",
                    component=component,
                    port=next(port_iter),
                    cell_index=cell_index,
                )
            )
        members.extend(cell_members)
        cells.append(
            {
                "cell_index": cell_index,
                "gateway_node_id": cell_gateway_node_id,
                "cell_node_ids": cell_node_ids,
                "compute_node_ids": [item["node_id"] for item in cell_members],
            }
        )

    compute_node_ids = [member["node_id"] for member in members]
    gateway_peer_ids = [cell["gateway_node_id"] for cell in cells[1:]]
    boot_assurance = _ai_max_boot_assurance_contract()
    installer = _ai_max_installer_contract(boot_assurance)
    autonomy_state = _ai_max_autonomy_state_machine()
    return {
        "profile": AI_MAX_EDGE_CELL_PROFILE,
        "size": AI_MAX_EDGE_CELL_SIZE,
        "fabric_cell_count": fabric_cell_count,
        "fabric_size": len(compute_node_ids),
        "lan_scope": lan_scope,
        "gateway_node_id": gateway_node_id,
        "gateway_peer_ids": gateway_peer_ids,
        "cell_node_ids": cells[0]["cell_node_ids"],
        "all_cell_node_ids": all_cell_node_ids,
        "compute_node_ids": compute_node_ids,
        "gateway_discovery": {
            "mode": "lan-local",
            "fabric_cell_count": fabric_cell_count,
            "lan_scope": lan_scope,
            "gateway_peer_ids": gateway_peer_ids,
        },
        "installer": installer,
        "boot_assurance": boot_assurance,
        "assurance_enforcement": _ai_max_assurance_enforcement_view(members, installer),
        "autonomy_state": autonomy_state,
        "disconnected_drill_report": _ai_max_disconnected_drill_report(autonomy_state),
        "cells": cells,
        "members": members,
    }


def _ai_max_boot_assurance_contract() -> dict[str, Any]:
    return {
        "secure_image_validation": "enabled",
        "boot_validation": "measured-verified",
        "tamper_detection": "enabled",
        "validation_failure_action": "disable-quarantine",
        "core_alerting": "when-connected",
    }


def _ai_max_installer_contract(boot_assurance: dict[str, Any]) -> dict[str, Any]:
    artifact = _ai_max_installer_artifact_manifest()
    signature = _ai_max_installer_signature_envelope(artifact)
    role_scaffolds = _ai_max_installer_role_scaffolds(artifact)
    boot_evidence = _ai_max_boot_evidence_records(artifact)
    return {
        "profile": AI_MAX_INSTALLER_PROFILE,
        "image": AI_MAX_INSTALLER_IMAGE,
        "signed_by": AI_MAX_INSTALLER_SIGNER,
        "signer": {
            "authority": AI_MAX_INSTALLER_SIGNER,
            "source": "k1s-core-controller",
        },
        "artifact": artifact,
        "signature": signature,
        "role_scaffolds": role_scaffolds,
        "boot_evidence": boot_evidence,
        "tampered_boot_evidence_fixture": _ai_max_tampered_boot_evidence_fixture(artifact),
        "verification": _ai_max_installer_verification_status(artifact, signature, role_scaffolds),
        "assurance": dict(boot_assurance),
        "install_paths": [
            {
                "path": "gateway",
                "post_install": {
                    "auto_boot": "enabled",
                    "connect_target": "core",
                    "usb_device_policy": "signed-only",
                    "display_mode": "telemetry",
                },
            },
            {
                "path": "cell-node",
                "post_install": {
                    "auto_boot": "enabled",
                    "connect_target": "gateway",
                    "usb_device_policy": "limited",
                    "display_mode": "connect-monitor-to-gateway",
                },
            },
        ],
    }


def _ai_max_installer_artifact_manifest() -> dict[str, Any]:
    return {
        "name": AI_MAX_INSTALLER_IMAGE,
        "profile": AI_MAX_INSTALLER_PROFILE,
        "image": AI_MAX_INSTALLER_IMAGE,
        "version": AI_MAX_INSTALLER_ARTIFACT_VERSION,
        "artifact_digest": AI_MAX_INSTALLER_ARTIFACT_DIGEST,
        "manifest_digest": AI_MAX_INSTALLER_MANIFEST_DIGEST,
        "path_coverage": ["gateway", "cell-node"],
        "provenance": {
            "builder": AI_MAX_INSTALLER_PROVENANCE_BUILDER,
            "source_revision": AI_MAX_INSTALLER_PROVENANCE_SOURCE_REVISION,
            "created_at": AI_MAX_INSTALLER_PROVENANCE_CREATED_AT,
        },
    }


def _ai_max_installer_signature_envelope(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": AI_MAX_INSTALLER_SIGNATURE_ALGORITHM,
        "signing_key_id": AI_MAX_INSTALLER_SIGNER,
        "signed_digest": artifact["manifest_digest"],
        "signature": AI_MAX_INSTALLER_SIGNATURE,
    }


def _ai_max_installer_role_scaffolds(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": "gateway",
            "module_ref": AI_MAX_INSTALLER_GATEWAY_MODULE_REF,
            "config_ref": AI_MAX_INSTALLER_GATEWAY_CONFIG_REF,
            "derived_from_manifest_digest": artifact["manifest_digest"],
            "post_install": {
                "auto_boot": "enabled",
                "connect_target": "core",
                "usb_device_policy": "signed-only",
                "display_mode": "telemetry",
            },
        },
        {
            "role": "cell-node",
            "module_ref": AI_MAX_INSTALLER_CELL_NODE_MODULE_REF,
            "config_ref": AI_MAX_INSTALLER_CELL_NODE_CONFIG_REF,
            "derived_from_manifest_digest": artifact["manifest_digest"],
            "post_install": {
                "auto_boot": "enabled",
                "connect_target": "gateway",
                "usb_device_policy": "limited",
                "display_mode": "connect-monitor-to-gateway",
            },
        },
    ]


def _ai_max_boot_evidence_records(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _ai_max_boot_evidence_record(
            node_id="gateway-1",
            role="gateway",
            artifact=artifact,
            boot_measurement_digest=AI_MAX_GATEWAY_BOOT_MEASUREMENT_DIGEST,
            nonce=AI_MAX_GATEWAY_BOOT_NONCE,
            status="verified",
            failure_reasons=[],
        ),
        _ai_max_boot_evidence_record(
            node_id="cell-node-1",
            role="cell-node",
            artifact=artifact,
            boot_measurement_digest=AI_MAX_CELL_NODE_BOOT_MEASUREMENT_DIGEST,
            nonce=AI_MAX_CELL_NODE_BOOT_NONCE,
            status="verified",
            failure_reasons=[],
        ),
    ]


def _ai_max_tampered_boot_evidence_fixture(artifact: dict[str, Any]) -> dict[str, Any]:
    return _ai_max_boot_evidence_record(
        node_id="gateway-1",
        role="gateway",
        artifact={
            **artifact,
            "artifact_digest": (
                "sha256:6666666666666666666666666666666666666666666666666666666666666666"
            ),
        },
        boot_measurement_digest=AI_MAX_GATEWAY_BOOT_MEASUREMENT_DIGEST,
        nonce="stale-nonce",
        status="rejected",
        failure_reasons=["artifact-digest-mismatch", "stale-nonce"],
    )


def _ai_max_boot_evidence_record(
    *,
    node_id: str,
    role: str,
    artifact: dict[str, Any],
    boot_measurement_digest: str,
    nonce: str,
    status: str,
    failure_reasons: list[str],
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "role": role,
        "installer_profile": AI_MAX_INSTALLER_PROFILE,
        "installer_image": AI_MAX_INSTALLER_IMAGE,
        "artifact_digest": artifact["artifact_digest"],
        "manifest_digest": artifact["manifest_digest"],
        "boot_measurement_digest": boot_measurement_digest,
        "signing_key_id": AI_MAX_INSTALLER_SIGNER,
        "verifier_trust_root": AI_MAX_INSTALLER_SIGNER,
        "nonce": nonce,
        "created_at": AI_MAX_BOOT_EVIDENCE_CREATED_AT,
        "verification": {
            "status": status,
            "verifier": "k1s-local-boot-evidence-verifier-v1",
            "trust_root": AI_MAX_INSTALLER_SIGNER,
            "failure_reasons": list(failure_reasons),
        },
    }


def _ai_max_assurance_enforcement_view(
    members: list[dict[str, Any]], installer: dict[str, Any]
) -> dict[str, Any]:
    healthy_members = [
        _ai_max_assurance_member(
            member,
            status="verified",
            schedulable=True,
            quarantined=False,
            failure_reasons=[],
            alert="none",
        )
        for member in members
    ]
    quarantined_node_id = next(
        (str(member["node_id"]) for member in members if member.get("role") == "cell-node"),
        "cell-node-1",
    )
    tampered_members = [
        _ai_max_assurance_member(
            member,
            status="tampered" if member.get("node_id") == quarantined_node_id else "verified",
            schedulable=member.get("node_id") != quarantined_node_id,
            quarantined=member.get("node_id") == quarantined_node_id,
            failure_reasons=(
                ["boot-measurement-mismatch"]
                if member.get("node_id") == quarantined_node_id
                else []
            ),
            alert="pending" if member.get("node_id") == quarantined_node_id else "none",
        )
        for member in members
    ]
    return {
        "mode": "local-simulated",
        "policy": "exclude-quarantined-from-placement",
        "status": "healthy",
        "usable_fabric_size": len(healthy_members),
        "quarantined_count": 0,
        "members": healthy_members,
        "boot_evidence_status": [
            {
                "node_id": evidence["node_id"],
                "role": evidence["role"],
                "status": evidence["verification"]["status"],
                "failure_reasons": list(evidence["verification"]["failure_reasons"]),
            }
            for evidence in list(installer.get("boot_evidence") or [])
        ],
        "tampered_quarantine_fixture": {
            "status": "quarantined",
            "quarantined_node_id": quarantined_node_id,
            "usable_fabric_size": len([item for item in tampered_members if item["schedulable"]]),
            "quarantined_count": len([item for item in tampered_members if item["quarantined"]]),
            "members": tampered_members,
        },
    }


def _ai_max_assurance_member(
    member: dict[str, Any],
    *,
    status: str,
    schedulable: bool,
    quarantined: bool,
    failure_reasons: list[str],
    alert: str,
) -> dict[str, Any]:
    return {
        "node_id": str(member["node_id"]),
        "role": str(member["role"]),
        "status": status,
        "schedulable": bool(schedulable),
        "quarantined": bool(quarantined),
        "failure_reasons": list(failure_reasons),
        "alert": alert,
    }


def _ai_max_autonomy_state_machine() -> dict[str, Any]:
    supported_events = [
        "core-link-lost",
        "local-services-retained",
        "core-link-restored",
        "reconcile-completed",
        "reconcile-failed",
    ]
    supported_transitions = [
        {
            "from": "connected",
            "event": "core-link-lost",
            "to": "core-link-unavailable",
        },
        {
            "from": "core-link-unavailable",
            "event": "local-services-retained",
            "to": "degraded-local-only",
        },
        {
            "from": "degraded-local-only",
            "event": "core-link-restored",
            "to": "reconciling",
        },
        {
            "from": "reconciling",
            "event": "reconcile-completed",
            "to": "reconciled",
        },
        {
            "from": "reconciling",
            "event": "reconcile-failed",
            "to": "degraded-local-only",
        },
    ]
    sample_trace = [
        supported_transitions[0],
        supported_transitions[1],
        supported_transitions[2],
        supported_transitions[3],
    ]
    return {
        "mode": "local-simulated",
        "current_state": "connected",
        "local_service_continuity": True,
        "cache": {
            "ready": True,
            "approved_workload_ref": "inferencecell/default/ai-max-edge-cell",
            "model_artifact_ref": "models/llama:stage11-local",
            "service_endpoints": {
                "gateway-api": "http://gateway.local:18080",
                "cell-monitor": "http://gateway.local:19090",
            },
            "last_core_sync": "core-sync-stage11",
        },
        "supported_events": supported_events,
        "supported_transitions": supported_transitions,
        "sample_transition_trace": sample_trace,
        "sample_final_state": "reconciled",
    }


def _ai_max_disconnected_drill_report(autonomy_state: dict[str, Any]) -> dict[str, Any]:
    trace = list(autonomy_state["sample_transition_trace"])
    cache = dict(autonomy_state["cache"])
    local_endpoint = str(cache["service_endpoints"]["gateway-api"])
    return {
        "drill_id": "ai-max-disconnected-local-drill-stage12",
        "name": "AI Max disconnected autonomy local simulation",
        "version": "stage12-local-v1",
        "mode": "simulation-only",
        "live_core_mutation": False,
        "live_network_disruption": False,
        "starting_state": autonomy_state["current_state"],
        "core_outage_event": "core-link-lost",
        "degraded_state": "degraded-local-only",
        "local_service_available": True,
        "local_probe": {
            "kind": "simulated-http",
            "endpoint": local_endpoint,
            "expected_status": 200,
            "observed_status": 200,
            "ok": True,
            "source": "gateway-cache",
        },
        "core_restore_event": "core-link-restored",
        "reconciliation": {
            "from": "reconciling",
            "to": "reconciled",
            "event": "reconcile-completed",
            "ok": True,
            "evidence_marker": "stage12-reconcile-marker",
        },
        "transition_trace": trace,
        "final_state": autonomy_state["sample_final_state"],
        "cache_summary": {
            "ready": cache["ready"],
            "approved_workload_ref": cache["approved_workload_ref"],
            "model_artifact_ref": cache["model_artifact_ref"],
            "last_core_sync": cache["last_core_sync"],
        },
        "assertions": {
            "started_connected": autonomy_state["current_state"] == "connected",
            "degraded_local_only": trace[1]["to"] == "degraded-local-only",
            "local_service_continuity": bool(autonomy_state["local_service_continuity"]),
            "restored_to_reconciling": trace[2]["to"] == "reconciling",
            "reconciled": autonomy_state["sample_final_state"] == "reconciled",
            "no_live_disruption": True,
        },
    }


def _ai_max_installer_verification_status(
    artifact: dict[str, Any],
    signature: dict[str, Any],
    role_scaffolds: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "verified",
        "checked_by": "workerbee-local-simulator",
        "root_of_trust": AI_MAX_INSTALLER_SIGNER,
        "signature_algorithm": signature["algorithm"],
        "signed_digest": signature["signed_digest"],
        "profile_match": artifact["profile"] == AI_MAX_INSTALLER_PROFILE,
        "image_match": artifact["image"] == AI_MAX_INSTALLER_IMAGE,
        "path_coverage": list(artifact["path_coverage"]),
        "role_scaffold_ready": True,
        "role_coverage": [str(item["role"]) for item in role_scaffolds],
        "boot_evidence_ready": True,
        "boot_evidence_roles": ["gateway", "cell-node"],
    }


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _parse_iso_timestamp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _is_ip_literal(value: str) -> bool:
    try:
        socket.inet_aton(value)
        return True
    except OSError:
        return False


def _gpu_smoke_manifest(node_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "ae.dev/v1alpha1",
        "kind": "Deployment",
        "metadata": {"name": "workerbee-gpu-smoke"},
        "spec": {
            "image": os.getenv("WORKERBEE_EDGE_LINK_GPU_SMOKE_IMAGE", DEFAULT_GPU_SMOKE_IMAGE),
            "replicas": 1,
            "runtimeClassName": "nvidia",
            "command": ["sh", "-c", "nvidia-smi && sleep 5"],
            "nodeSelector": {"node_id": node_id},
            "resources": {
                "requests": {"nvidia.com/gpu": 1},
                "limits": {"nvidia.com/gpu": 1},
            },
        },
    }
