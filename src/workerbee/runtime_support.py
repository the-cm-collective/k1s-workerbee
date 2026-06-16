"""Container runtime diagnostics and cleanup helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError

WORKERBEE_LABEL = "workerbee.managed=true"
CONTAINERD_RUNTIME = "containerd"
CONTAINERD_RESERVED_NAMESPACES = frozenset({"ae", "k8s.io", "moby", "default"})
CONTAINERD_REQUIRED_CNI_PLUGINS = ("bridge", "host-local", "loopback", "portmap")
PODMAN_COMPATIBLE_CNI_VERSION = "0.4.0"
MICROK8S_ROOT = Path("/var/snap/microk8s")
MICROK8S_CONTAINERD_SOCKET = MICROK8S_ROOT / "common" / "run" / "containerd.sock"
BUILD_SUMMARY_TAIL_LINES = 8
BUILD_SUMMARY_MATCH_LIMIT = 12
HARDENING_PROFILES = frozenset({"standard", "hardened"})
DEFAULT_HARDENING_PROFILE = "standard"
_MINIMAL_BASE_MARKERS = ("alpine", "slim", "distroless", "scratch", "wolfi", "chainguard")
_PACKAGE_MANAGER_MARKERS = {
    "apk": re.compile(r"\bapk\s+add\b"),
    "apt": re.compile(r"\bapt(?:-get)?\s+install\b"),
    "dnf": re.compile(r"\bdnf\s+install\b"),
    "microdnf": re.compile(r"\bmicrodnf\s+install\b"),
    "yum": re.compile(r"\byum\s+install\b"),
}


@dataclass(frozen=True, slots=True)
class RuntimeCommand:
    runtime: str
    base_args: tuple[str, ...] = ()

    def run(
        self,
        args: list[str],
        *,
        timeout: int = 30,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [self.runtime, *self.base_args, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(proc.stdout.strip() or f"{self.runtime} {' '.join(args)} failed")
        return proc


def resolve_runtime(requested: str = "auto") -> str:
    requested = requested.lower()
    if requested in {"podman", "docker", CONTAINERD_RUNTIME}:
        executable = nerdctl_binary() if requested == CONTAINERD_RUNTIME else requested
        if shutil.which(executable) is None:
            raise WorkerBeeError(
                code="RUNTIME_MISSING",
                message=f"{executable} not found on PATH",
                remediation=_runtime_guidance(),
            )
        return requested
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    raise WorkerBeeError(
        code="RUNTIME_MISSING",
        message="Podman or Docker is required",
        remediation=_runtime_guidance(),
    )


def runtime_diagnostics(
    requested: str = "auto",
    *,
    state_root: Path | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "requested": requested,
        "podman": _runtime_version("podman"),
        "docker": _runtime_version("docker"),
        "nerdctl": _runtime_version(nerdctl_binary()),
        "buildctl": _runtime_version(buildctl_binary()),
        "containerd": {
            "address": containerd_address(),
            "socket_exists": _containerd_socket_exists(containerd_address()),
            "microk8s_conflict": containerd_microk8s_conflict_info(),
            "reserved_namespaces": sorted(CONTAINERD_RESERVED_NAMESPACES),
        },
    }
    if state_root is not None and requested.lower() == CONTAINERD_RUNTIME:
        diagnostics["containerd"]["safety"] = containerd_safety_info(state_root)
    try:
        runtime = resolve_runtime(requested)
        diagnostics["selected"] = runtime
        diagnostics["ok"] = True
        if runtime == "podman":
            diagnostics["podman_rootless"] = _podman_rootless()
            diagnostics["podman_cni"] = podman_cni_diagnostics()
        if runtime == "docker":
            diagnostics["docker_desktop_hint"] = _docker_desktop_hint()
        if runtime == CONTAINERD_RUNTIME:
            diagnostics["containerd"]["selected"] = True
            probe = containerd_nerdctl_probe(containerd_address())
            diagnostics["containerd"]["nerdctl_probe"] = probe
            diagnostics["ok"] = bool(probe.get("ok"))
            if not probe.get("ok"):
                diagnostics["error"] = {
                    "code": probe.get("code") or "CONTAINERD_UNAVAILABLE",
                    "message": probe.get("message") or "nerdctl cannot access containerd",
                    "details": {"probe": probe},
                    "remediation": _runtime_guidance(),
                    "retryable": True,
                }
    except WorkerBeeError as exc:
        diagnostics["selected"] = None
        diagnostics["ok"] = False
        diagnostics["error"] = exc.public_dict()
    return diagnostics


def ensure_podman_network(network: str, *, podman_bin: str = "podman") -> dict[str, Any]:
    """Create a WorkerBee Podman network and normalize legacy CNI configs when needed."""
    if not network:
        raise WorkerBeeError(
            code="PODMAN_NETWORK_MISSING",
            message="Podman network name is required",
            remediation="Restart the WorkerBee project so it can allocate a scoped network name.",
        )
    cmd = RuntimeCommand(podman_bin)
    exists = cmd.run(["network", "exists", network], timeout=10)
    created = False
    if exists.returncode != 0:
        create = cmd.run(["network", "create", network], timeout=30)
        if create.returncode != 0:
            raise WorkerBeeError(
                code="PODMAN_NETWORK_CREATE_FAILED",
                message=create.stdout.strip() or f"podman network create {network} failed",
                details={"network": network, "stdout": create.stdout},
                remediation=(
                    "Inspect Podman/CNI state, remove stale WorkerBee networks, then retry."
                ),
            )
        created = True

    before = podman_cni_diagnostics(network=network)
    normalized = _normalize_workerbee_podman_cni(network)
    after = podman_cni_diagnostics(network=network)
    target = after.get("target_config")
    if isinstance(target, dict) and target.get("incompatible"):
        raise WorkerBeeError(
            code="PODMAN_CNI_INCOMPATIBLE",
            message=f"Podman network {network} has an incompatible CNI firewall config",
            details={"network": network, "diagnostics": after, "normalization": normalized},
            remediation=(
                "Use a compatible Podman CNI config version, remove stale WorkerBee CNI files, "
                "or run WorkerBee with the native containerd runtime."
            ),
        )

    inspect = cmd.run(["network", "inspect", network], timeout=10)
    if inspect.returncode != 0:
        raise WorkerBeeError(
            code="PODMAN_NETWORK_INSPECT_FAILED",
            message=inspect.stdout.strip() or f"podman network inspect {network} failed",
            details={
                "network": network,
                "stdout": inspect.stdout,
                "diagnostics": after,
                "normalization": normalized,
            },
            remediation="Inspect Podman/CNI warnings and recreate the WorkerBee project network.",
        )
    return {
        "ok": True,
        "network": network,
        "created": created,
        "diagnostics_before": before,
        "diagnostics_after": after,
        "normalization": normalized,
    }


def podman_cni_diagnostics(network: str | None = None) -> dict[str, Any]:
    configs = _podman_cni_configs()
    target = None
    invalid: list[dict[str, Any]] = []
    workerbee_invalid: list[dict[str, Any]] = []
    foreign_invalid: list[dict[str, Any]] = []
    for cfg in configs:
        if network and cfg.get("name") == network:
            target = cfg
        if cfg.get("incompatible"):
            invalid.append(cfg)
            if cfg.get("workerbee_owned"):
                workerbee_invalid.append(cfg)
            else:
                foreign_invalid.append(cfg)
    return {
        "config_dirs": [str(path) for path in _podman_cni_config_dirs()],
        "target_network": network,
        "target_config": target,
        "invalid_config_count": len(invalid),
        "workerbee_invalid_config_count": len(workerbee_invalid),
        "foreign_invalid_config_count": len(foreign_invalid),
        "invalid_configs": invalid,
        "foreign_invalid_configs": foreign_invalid,
    }


def _normalize_workerbee_podman_cni(network: str) -> dict[str, Any]:
    if not network.startswith("workerbee-"):
        return {"changed": False, "reason": "network_not_workerbee_owned", "network": network}
    changes: list[dict[str, Any]] = []
    target_seen = False
    for cfg in _podman_cni_configs():
        name = str(cfg.get("name") or "")
        if name == network:
            target_seen = True
        if not cfg.get("workerbee_owned"):
            continue
        if not cfg.get("incompatible"):
            continue
        path = Path(str(cfg.get("path") or ""))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {
                "changed": False,
                "reason": "target_config_unreadable",
                "network": network,
                "path": str(path),
                "error": str(exc),
            }
        data["cniVersion"] = PODMAN_COMPATIBLE_CNI_VERSION
        content = json.dumps(data, indent=2) + "\n"
        try:
            backup = path.with_suffix(path.suffix + ".bak-workerbee")
            if not backup.exists():
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            path.write_text(content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return {
                "changed": False,
                "reason": "target_config_write_failed",
                "network": network,
                "path": str(path),
                "error": str(exc),
            }
        changes.append(
            {
                "path": str(path),
                "name": name or None,
                "target": name == network,
                "from_cni_version": cfg.get("cni_version"),
                "to_cni_version": PODMAN_COMPATIBLE_CNI_VERSION,
            }
        )
    if not changes:
        return {
            "changed": False,
            "reason": "target_config_compatible" if target_seen else "target_config_not_found",
            "network": network,
        }
    return {"changed": True, "network": network, "changes": changes}


def _podman_cni_config_dirs() -> list[Path]:
    candidates: list[Path] = []
    for raw in (
        os.getenv("CNI_CONF_DIR"),
        os.getenv("NETCONFPATH"),
        os.getenv("CNI_CONFIG_PATH"),
    ):
        if raw:
            candidates.extend(Path(item).expanduser() for item in raw.split(os.pathsep) if item)
    candidates.extend(
        [
            Path("/etc/cni/net.d"),
            Path.home() / ".config" / "cni" / "net.d",
        ]
    )
    seen: set[Path] = set()
    result: list[Path] = []
    for candidate in candidates:
        try:
            key = candidate.resolve(strict=False)
        except OSError:
            key = candidate
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _podman_cni_configs() -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for directory in _podman_cni_config_dirs():
        try:
            entries = sorted(
                path
                for path in directory.iterdir()
                if path.is_file() and path.suffix in {".conf", ".conflist"}
            )
        except OSError:
            continue
        for path in entries:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                configs.append(
                    {
                        "path": str(path),
                        "name": None,
                        "cni_version": None,
                        "plugins": [],
                        "workerbee_owned": False,
                        "incompatible": False,
                        "error": str(exc),
                    }
                )
                continue
            name = str(data.get("name") or "")
            plugins = _cni_plugin_types(data)
            cni_version = str(data.get("cniVersion") or "")
            incompatible = _podman_cni_firewall_incompatible(cni_version, plugins)
            configs.append(
                {
                    "path": str(path),
                    "name": name or None,
                    "cni_version": cni_version or None,
                    "plugins": plugins,
                    "workerbee_owned": name.startswith("workerbee-"),
                    "incompatible": incompatible,
                }
            )
    return configs


def _cni_plugin_types(data: dict[str, Any]) -> list[str]:
    raw_plugins = data.get("plugins")
    plugins = raw_plugins if isinstance(raw_plugins, list) else [data]
    result: list[str] = []
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("type"):
            result.append(str(plugin["type"]))
    return result


def _podman_cni_firewall_incompatible(cni_version: str, plugins: list[str]) -> bool:
    if "firewall" not in plugins:
        return False
    try:
        major = int(str(cni_version).split(".", 1)[0])
    except Exception:
        return False
    return major >= 1


def cleanup_runtime(
    *,
    state_root: Path,
    runtime: str,
    execute: bool = False,
    purge_images: bool = False,
) -> dict[str, Any]:
    selected = resolve_runtime(runtime)
    if selected == CONTAINERD_RUNTIME:
        return _cleanup_containerd_runtime(
            state_root=state_root,
            execute=execute,
            purge_images=purge_images,
        )
    cmd = RuntimeCommand(selected)
    state_hash = _state_hash(state_root)
    project_networks = _known_project_networks(state_root)
    actions: list[dict[str, Any]] = []
    actions.extend(_cleanup_containers(cmd, state_hash=state_hash, execute=execute))
    actions.extend(_cleanup_networks(cmd, targets=project_networks, execute=execute))
    if purge_images:
        actions.extend(_cleanup_images(cmd, state_hash=state_hash, execute=execute))
    return {
        "ok": True,
        "runtime": selected,
        "state_root": str(state_root.resolve()),
        "execute": execute,
        "purge_images": purge_images,
        "actions": actions,
    }


def nerdctl_binary() -> str:
    return os.getenv("WORKERBEE_NERDCTL_BIN") or os.getenv("AE_NERDCTL_BIN") or "nerdctl"


def buildctl_binary() -> str:
    return os.getenv("WORKERBEE_BUILDCTL_BIN") or os.getenv("AE_BUILDCTL_BIN") or "buildctl"


def containerd_address() -> str:
    return (
        os.getenv("WORKERBEE_CONTAINERD_ADDRESS")
        or os.getenv("AE_CONTAINERD_ADDRESS")
        or os.getenv("AE_CRI_ENDPOINT")
        or "unix:///run/containerd/containerd.sock"
    )


def containerd_socket_path(address: str | None = None) -> Path | None:
    selected = address or containerd_address()
    if selected.startswith("unix://"):
        return Path(selected.removeprefix("unix://"))
    if selected.startswith("/"):
        return Path(selected)
    return None


def shared_k8s_containerd_allowed() -> bool:
    return _env_truthy("WORKERBEE_ALLOW_SHARED_K8S_CONTAINERD")


def containerd_microk8s_conflict_info(
    address: str | None = None,
    cni_netconfpath: Path | str | None = None,
    *,
    allow_shared_k8s_containerd: bool | None = None,
) -> dict[str, Any]:
    selected_address = address or containerd_address()
    socket_path = containerd_socket_path(selected_address)
    cni_path = Path(str(cni_netconfpath)).expanduser() if cni_netconfpath is not None else None
    allow_shared = (
        shared_k8s_containerd_allowed()
        if allow_shared_k8s_containerd is None
        else bool(allow_shared_k8s_containerd)
    )
    uses_microk8s_socket = bool(
        socket_path is not None and _is_microk8s_containerd_socket(socket_path)
    )
    uses_microk8s_cni = bool(cni_path is not None and _is_microk8s_path(cni_path))
    reasons = []
    if uses_microk8s_cni:
        reasons.append("microk8s_cni_netconfpath")
    if uses_microk8s_socket and not allow_shared:
        reasons.append("microk8s_containerd_socket")
    return {
        "address": selected_address,
        "socket_path": str(socket_path) if socket_path is not None else None,
        "cni_netconfpath": str(cni_path) if cni_path is not None else None,
        "microk8s_detected": _microk8s_detected(),
        "uses_microk8s_socket": uses_microk8s_socket,
        "uses_microk8s_cni_netconfpath": uses_microk8s_cni,
        "allow_shared_k8s_containerd": allow_shared,
        "blocked": bool(reasons),
        "reasons": reasons,
    }


def raise_if_containerd_microk8s_conflict(
    address: str | None = None,
    cni_netconfpath: Path | str | None = None,
    *,
    allow_shared_k8s_containerd: bool | None = None,
) -> None:
    info = containerd_microk8s_conflict_info(
        address,
        cni_netconfpath,
        allow_shared_k8s_containerd=allow_shared_k8s_containerd,
    )
    if not info["blocked"]:
        return
    raise WorkerBeeError(
        code="CONTAINERD_MICROK8S_CONFLICT",
        message=(
            "Refusing to use MicroK8s containerd or CNI paths for WorkerBee "
            "direct-containerd mode"
        ),
        details=info,
        remediation=(
            "Use Docker/Podman, a non-Kubernetes host containerd socket such as "
            "`unix:///run/containerd/containerd.sock`, or an isolated WorkerBee "
            "containerd. For an intentional controlled test against the MicroK8s "
            "containerd socket, set WORKERBEE_ALLOW_SHARED_K8S_CONTAINERD=1; "
            "WorkerBee still refuses MicroK8s CNI config paths."
        ),
    )


def containerd_socket_access_info(address: str | None = None) -> dict[str, Any]:
    selected_address = address or containerd_address()
    path = containerd_socket_path(selected_address)
    if path is None:
        return {
            "address": selected_address,
            "path": None,
            "exists": None,
            "accessible": None,
            "error": None,
            "error_code": "NON_UNIX_CONTAINERD_ADDRESS",
        }
    exists = path.exists()
    if not exists:
        return {
            "address": selected_address,
            "path": str(path),
            "exists": False,
            "accessible": False,
            "error": f"containerd socket not found: {path}",
            "error_code": "CONTAINERD_SOCKET_MISSING",
        }
    if not path.is_socket():
        return {
            "address": selected_address,
            "path": str(path),
            "exists": True,
            "accessible": False,
            "error": f"containerd path is not a socket: {path}",
            "error_code": "CONTAINERD_SOCKET_INVALID",
        }
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except PermissionError as exc:
        return {
            "address": selected_address,
            "path": str(path),
            "exists": True,
            "accessible": False,
            "error": str(exc),
            "error_code": "CONTAINERD_SOCKET_PERMISSION_DENIED",
        }
    except OSError as exc:
        return {
            "address": selected_address,
            "path": str(path),
            "exists": True,
            "accessible": False,
            "error": str(exc),
            "error_code": "CONTAINERD_SOCKET_CONNECT_FAILED",
        }
    finally:
        sock.close()
    return {
        "address": selected_address,
        "path": str(path),
        "exists": True,
        "accessible": True,
        "error": None,
        "error_code": None,
    }


def containerd_nerdctl_probe(address: str | None = None) -> dict[str, Any]:
    selected_address = address or containerd_address()
    socket_info = containerd_socket_access_info(selected_address)
    conflict = containerd_microk8s_conflict_info(selected_address)
    nerdctl = shutil.which(nerdctl_binary())
    using_workerbee_helper = bool(os.getenv("WORKERBEE_CONTAINERD_HELPER_SOCKET"))
    if conflict["blocked"]:
        return {
            "ok": False,
            "code": "CONTAINERD_MICROK8S_CONFLICT",
            "message": (
                "WorkerBee direct-containerd mode refuses MicroK8s containerd by default"
            ),
            "cmd": None,
            "stdout": "",
            "socket": socket_info,
            "namespaces": [],
            "microk8s_conflict": conflict,
        }
    if nerdctl is None:
        return {
            "ok": False,
            "code": "NERDCTL_MISSING",
            "message": f"{nerdctl_binary()} not found on PATH",
            "cmd": None,
            "stdout": "",
            "socket": socket_info,
            "namespaces": [],
        }
    if socket_info.get("accessible") is False and not using_workerbee_helper:
        return {
            "ok": False,
            "code": socket_info.get("error_code") or "CONTAINERD_SOCKET_INACCESSIBLE",
            "message": str(socket_info.get("error") or "containerd socket is not accessible"),
            "cmd": None,
            "stdout": "",
            "socket": socket_info,
            "namespaces": [],
        }
    cmd = [nerdctl, "--address", selected_address, "namespace", "ls", "--quiet"]
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "code": "NERDCTL_PROBE_FAILED",
            "message": str(exc),
            "cmd": cmd,
            "stdout": "",
            "socket": socket_info,
            "namespaces": [],
        }
    namespaces = [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and line.strip().lower() != "name"
    ]
    if proc.returncode == 0:
        return {
            "ok": True,
            "code": None,
            "message": None,
            "cmd": cmd,
            "stdout": proc.stdout,
            "socket": socket_info,
            "namespaces": sorted(set(namespaces)),
        }
    return {
        "ok": False,
        "code": _classify_nerdctl_error(proc.stdout),
        "message": (proc.stdout or "").strip() or f"nerdctl exited {proc.returncode}",
        "cmd": cmd,
        "stdout": proc.stdout,
        "socket": socket_info,
        "namespaces": [],
    }


def containerd_cni_bin_dir() -> str:
    configured = (
        os.getenv("WORKERBEE_CONTAINERD_CNI_BIN_DIR")
        or os.getenv("AE_CONTAINERD_CNI_BIN_DIR")
        or os.getenv("CNI_PATH")
    )
    if configured:
        return configured
    return str(_detect_containerd_cni_bin_dir())


def _detect_containerd_cni_bin_dir() -> Path:
    candidates = [
        Path("/opt/cni/bin"),
        Path("/usr/lib/cni"),
        Path("/usr/libexec/cni"),
        Path("/run/current-system/sw/bin"),
    ]
    for name in CONTAINERD_REQUIRED_CNI_PLUGINS:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found).resolve().parent)
    for candidate in candidates:
        if _cni_dir_complete(candidate):
            return candidate
    return Path("/opt/cni/bin")


def _cni_dir_complete(path: Path) -> bool:
    try:
        return all(path.joinpath(name).exists() for name in CONTAINERD_REQUIRED_CNI_PLUGINS)
    except OSError:
        return False


def containerd_namespace(
    state_root: Path,
    project: str | None = None,
    *,
    system: bool = False,
) -> str:
    state_hash = _state_hash(state_root)
    if system or not project:
        return f"workerbee-{state_hash}-system"
    return f"workerbee-{state_hash}-{project_slug_for_runtime(project)}"


def containerd_network_name(state_root: Path, project: str) -> str:
    return containerd_namespace(state_root, project=project)


def containerd_network_subnet(state_root: Path, project: str) -> str:
    seed = f"{state_root.expanduser().resolve()}:{project_slug_for_runtime(project)}"
    digest = hashlib.blake2s(seed.encode("utf-8"), digest_size=2).digest()
    second = 200 + (digest[0] % 16)
    third = 1 + (digest[1] % 254)
    return f"10.{second}.{third}.0/24"


def containerd_data_root(
    state_root: Path,
    project: str | None = None,
    *,
    system: bool = False,
) -> Path:
    root = state_root.expanduser().resolve()
    if system or not project:
        return root / "global" / "containerd-data"
    return root / "projects" / project_slug_for_runtime(project) / "containerd-data"


def containerd_cni_conf_dir(
    state_root: Path,
    project: str | None = None,
    *,
    system: bool = False,
) -> Path:
    root = state_root.expanduser().resolve()
    if system or not project:
        return root / "global" / "containerd-cni-net.d"
    return root / "projects" / project_slug_for_runtime(project) / "containerd-cni-net.d"


def containerd_base_args(
    *,
    state_root: Path,
    project: str | None = None,
    system: bool = False,
    ensure_dirs: bool = True,
) -> list[str]:
    data_root = containerd_data_root(state_root, project=project, system=system)
    cni_conf = containerd_cni_conf_dir(state_root, project=project, system=system)
    subnet = _containerd_cni_subnet(state_root, project=project, system=system)
    raise_if_containerd_microk8s_conflict(
        address=containerd_address(),
        cni_netconfpath=cni_conf,
    )
    if ensure_dirs:
        data_root.mkdir(parents=True, exist_ok=True)
        cni_conf.mkdir(parents=True, exist_ok=True)
        _ensure_containerd_default_bridge_config(cni_conf, subnet=subnet)
    namespace = containerd_namespace(state_root, project=project, system=system)
    _raise_if_reserved_containerd_namespace(namespace)
    return [
        nerdctl_binary(),
        "--address",
        containerd_address(),
        "--namespace",
        namespace,
        "--data-root",
        str(data_root),
        "--cni-path",
        containerd_cni_bin_dir(),
        "--cni-netconfpath",
        str(cni_conf),
    ]


def _containerd_cni_subnet(
    state_root: Path,
    project: str | None = None,
    *,
    system: bool = False,
) -> str:
    name = "system" if system or not project else project_slug_for_runtime(project)
    return containerd_network_subnet(state_root, f"{name}-default-bridge")


def _ensure_containerd_default_bridge_config(cni_conf: Path, *, subnet: str) -> None:
    """Prevent nerdctl from recreating its default bridge per WorkerBee CNI dir."""
    config = cni_conf / "nerdctl-bridge.conflist"
    data = _containerd_default_bridge_config(cni_conf, subnet=subnet)
    content = json.dumps(data, indent=2) + "\n"
    if config.exists() and config.read_text(encoding="utf-8") == content:
        return
    config.write_text(content, encoding="utf-8")


def _containerd_default_bridge_config(cni_conf: Path, *, subnet: str) -> dict[str, Any]:
    nerdctl_id = hashlib.blake2s(
        str(cni_conf.resolve(strict=False)).encode("utf-8"),
        digest_size=32,
    ).hexdigest()
    return {
        "cniVersion": "1.0.0",
        "name": "bridge",
        "nerdctlID": nerdctl_id,
        "nerdctlLabels": {"nerdctl/default-network": "true"},
        "plugins": [
            {
                "type": "bridge",
                "bridge": _containerd_bridge_name(cni_conf),
                "isGateway": True,
                "ipMasq": True,
                "hairpinMode": True,
                "ipam": {
                    "ranges": [[{"gateway": _subnet_gateway(subnet), "subnet": subnet}]],
                    "routes": [{"dst": "0.0.0.0/0"}],
                    "type": "host-local",
                },
            },
            {"type": "portmap", "capabilities": {"portMappings": True}},
            {"type": "firewall", "ingressPolicy": "same-bridge"},
            {"type": "tuning"},
        ],
    }


def _containerd_bridge_name(cni_conf: Path) -> str:
    digest = hashlib.blake2s(
        str(cni_conf.resolve(strict=False)).encode("utf-8"),
        digest_size=5,
    ).hexdigest()
    return f"wb{digest}"


def _subnet_gateway(subnet: str) -> str:
    network = ip_network(subnet, strict=False)
    return str(network.network_address + 1)


def runtime_command_args(
    runtime: str,
    *,
    state_root: Path,
    project: str | None,
    args: list[str],
    system: bool = False,
    ensure_dirs: bool = True,
) -> list[str]:
    if runtime == CONTAINERD_RUNTIME:
        return [
            *containerd_base_args(
                state_root=state_root,
                project=project,
                system=system,
                ensure_dirs=ensure_dirs,
            ),
            *args,
        ]
    return [runtime, *args]


def write_containerd_cli_wrapper(
    path: Path,
    *,
    state_root: Path,
    project: str | None = None,
    system: bool = False,
    system_container: str | None = None,
) -> Path:
    base = containerd_base_args(state_root=state_root, project=project, system=system)
    path.parent.mkdir(parents=True, exist_ok=True)
    quoted_base = " ".join(shlex.quote(part) for part in base)
    if system_container:
        system_base = " ".join(
            shlex.quote(part)
            for part in containerd_base_args(state_root=state_root, system=True)
        )
        script = (
            "#!/usr/bin/env sh\n"
            f"if [ \"$1\" = \"exec\" ] && [ \"$2\" = {shlex.quote(system_container)} ]; then\n"
            f"  exec {system_base} \"$@\"\n"
            "fi\n"
            f"exec {quoted_base} \"$@\"\n"
        )
    else:
        script = f"#!/usr/bin/env sh\nexec {quoted_base} \"$@\"\n"
    path.write_text(
        script,
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def build_image_with_runtime(
    *,
    runtime: str,
    state_root: Path,
    project: str,
    context: Path,
    tag: str,
    dockerfile: Path | None = None,
    labels: list[str] | None = None,
    hardening_profile: str | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    selected = resolve_runtime(runtime)
    build_context = context.expanduser().resolve()
    build_file = _resolve_dockerfile(build_context, dockerfile)
    profile = _normalize_hardening_profile(hardening_profile)
    hardening = _analyze_container_build_file(
        _build_file_for_analysis(build_context, build_file),
        profile=profile,
    )
    label_values = list(labels or workerbee_runtime_labels(state_root=state_root, project=project))
    profile_label = f"workerbee.hardening_profile={profile}"
    if not any(label.startswith("workerbee.hardening_profile=") for label in label_values):
        label_values.append(profile_label)
    if selected == CONTAINERD_RUNTIME:
        return _build_image_containerd(
            state_root=state_root,
            project=project,
            context=build_context,
            dockerfile=build_file,
            tag=tag,
            labels=label_values,
            hardening=hardening,
            hardening_profile=profile,
            timeout=timeout,
        )
    cmd = [selected, "build", "-t", tag]
    cmd.extend(_container_build_file_args(build_context, build_file))
    cmd.append("--no-cache")
    for label in label_values:
        cmd.extend(["--label", label])
    cmd.append(str(build_context))
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    result = {
        "ok": proc.returncode == 0,
        "runtime": selected,
        "build_backend": selected,
        "tag": tag,
        "context": str(build_context),
        "dockerfile": str(build_file) if build_file else None,
        "labels": label_values,
        "hardening_profile": profile,
        "hardening": hardening,
        "cmd": cmd,
        "stdout": proc.stdout,
        "build_summary": _build_output_summary(proc.stdout, backend=selected, tag=tag),
    }
    if proc.returncode != 0:
        raise RuntimeError(json.dumps(result, indent=2))
    return result


def workerbee_runtime_labels(*, state_root: Path, project: str | None = None) -> list[str]:
    labels = [
        WORKERBEE_LABEL,
        f"workerbee.state_root_hash={_state_hash(state_root)}",
    ]
    if project:
        labels.append(f"workerbee.project={project}")
    return labels


def project_slug_for_runtime(value: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    out = "-".join(part for part in out.split("-") if part)
    return out or "default"


def containerd_safety_info(state_root: Path, project: str | None = None) -> dict[str, Any]:
    root = state_root.expanduser().resolve()
    project_name = project_slug_for_runtime(project) if project else None
    system_namespace = containerd_namespace(root, system=True)
    project_namespace = (
        containerd_namespace(root, project=project_name) if project_name else None
    )
    system_cni_conf = containerd_cni_conf_dir(root, system=True)
    project_cni_conf = (
        containerd_cni_conf_dir(root, project=project_name) if project_name else None
    )
    conflict = containerd_microk8s_conflict_info(
        address=containerd_address(),
        cni_netconfpath=project_cni_conf or system_cni_conf,
    )
    namespaces = [system_namespace] + ([project_namespace] if project_namespace else [])
    reserved_overlap = sorted(set(namespaces).intersection(CONTAINERD_RESERVED_NAMESPACES))
    active_namespaces, namespace_probe_error = _list_containerd_namespaces()
    non_workerbee_namespaces = [
        item for item in active_namespaces if not item.startswith(f"workerbee-{_state_hash(root)}-")
    ]
    warnings = []
    nerdctl0_exists = _netdev_exists("nerdctl0")
    if _containerd_socket_exists(containerd_address()):
        warnings.append(
            "WorkerBee is using a shared host containerd socket; isolation relies on "
            "WorkerBee state-hash namespaces and state-local nerdctl/CNI roots."
        )
    if conflict["blocked"]:
        warnings.append(
            "WorkerBee direct-containerd mode is blocked because it would use MicroK8s "
            "containerd or CNI paths."
        )
    if conflict["uses_microk8s_socket"] and conflict["allow_shared_k8s_containerd"]:
        warnings.append(
            "WORKERBEE_ALLOW_SHARED_K8S_CONTAINERD is set; WorkerBee is allowed to share "
            "the MicroK8s containerd socket for this run, but CNI config paths remain isolated."
        )
    if conflict["microk8s_detected"] and nerdctl0_exists:
        warnings.append(
            "`nerdctl0` exists on a MicroK8s host. Calico IP autodetection can select the "
            "wrong interface unless MicroK8s is pinned to the real host NIC."
        )
    return {
        "state_hash": _state_hash(root),
        "address": containerd_address(),
        "socket_exists": _containerd_socket_exists(containerd_address()),
        "microk8s_detected": conflict["microk8s_detected"],
        "microk8s_conflict": conflict,
        "nerdctl0_exists": nerdctl0_exists,
        "reserved_namespaces": sorted(CONTAINERD_RESERVED_NAMESPACES),
        "reserved_overlap": reserved_overlap,
        "active_namespaces": active_namespaces,
        "non_workerbee_namespaces": non_workerbee_namespaces,
        "namespace_probe_error": namespace_probe_error,
        "system_namespace": system_namespace,
        "project_namespace": project_namespace,
        "project_network": containerd_network_name(root, project_name) if project_name else None,
        "system_data_root": str(containerd_data_root(root, system=True)),
        "project_data_root": (
            str(containerd_data_root(root, project=project_name)) if project_name else None
        ),
        "system_cni_conf_dir": str(system_cni_conf),
        "project_cni_conf_dir": str(project_cni_conf) if project_cni_conf else None,
        "cni_bin_dir": containerd_cni_bin_dir(),
        "warnings": warnings,
        "ok": not reserved_overlap and not conflict["blocked"],
    }


def _cleanup_containerd_runtime(
    *,
    state_root: Path,
    execute: bool,
    purge_images: bool,
) -> dict[str, Any]:
    state_hash = _state_hash(state_root)
    project_names = set(_known_projects(state_root))
    namespaces = [(containerd_namespace(state_root, system=True), None)]
    namespaces.extend(
        (containerd_namespace(state_root, project), project)
        for project in sorted(project_names)
    )
    actions: list[dict[str, Any]] = []
    for namespace, project in namespaces:
        base = containerd_base_args(state_root=state_root, project=project, system=project is None)
        cmd = RuntimeCommand(base[0], tuple(base[1:]))
        for action in _cleanup_containers(cmd, state_hash=state_hash, execute=execute):
            action["namespace"] = namespace
            actions.append(action)
        if project is not None:
            targets = {containerd_network_name(state_root, project)}
            for action in _cleanup_networks(cmd, targets=targets, execute=execute):
                action["namespace"] = namespace
                actions.append(action)
        if purge_images:
            for action in _cleanup_images(cmd, execute=execute, all_images=True):
                action["namespace"] = namespace
                actions.append(action)
        if execute:
            proc = cmd.run(["namespace", "rm", namespace], timeout=30)
            if proc.returncode == 0:
                actions.append(
                    {
                        "kind": "namespace",
                        "name": namespace,
                        "action": "remove",
                        "returncode": proc.returncode,
                    }
                )
    return {
        "ok": True,
        "runtime": CONTAINERD_RUNTIME,
        "state_root": str(state_root.resolve()),
        "safety": containerd_safety_info(state_root),
        "execute": execute,
        "purge_images": purge_images,
        "actions": actions,
    }


def _build_image_containerd(
    *,
    state_root: Path,
    project: str,
    context: Path,
    dockerfile: Path | None,
    tag: str,
    labels: list[str],
    hardening: dict[str, Any],
    hardening_profile: str,
    timeout: int,
) -> dict[str, Any]:
    base = containerd_base_args(state_root=state_root, project=project)
    attempts: list[dict[str, Any]] = []
    fallbacks = [fallback for fallback in ("podman", "docker") if shutil.which(fallback)]
    if shutil.which(buildctl_binary()) is None and fallbacks:
        attempts.append(
            {
                "backend": "nerdctl",
                "skipped": True,
                "reason": f"{buildctl_binary()} not found; using image save/load fallback",
            }
        )
    else:
        cmd = [*base, "build", "-t", tag]
        cmd.extend(_container_build_file_args(context, dockerfile))
        cmd.append("--no-cache")
        for label in labels:
            cmd.extend(["--label", label])
        cmd.append(str(context))
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        attempts.append(_build_attempt("nerdctl", proc.returncode, proc.stdout, tag=tag))
        if proc.returncode == 0:
            return {
                "ok": True,
                "runtime": CONTAINERD_RUNTIME,
                "build_backend": "nerdctl",
                "tag": tag,
                "context": str(context),
                "dockerfile": str(dockerfile) if dockerfile else None,
                "labels": labels,
                "hardening_profile": hardening_profile,
                "hardening": hardening,
                "cmd": cmd,
                "stdout": proc.stdout,
                "build_summary": _build_output_summary(
                    proc.stdout,
                    backend="nerdctl",
                    tag=tag,
                ),
            }
    for fallback in fallbacks:
        result = _build_with_fallback_and_load(
            fallback=fallback,
            containerd_base=base,
            state_root=state_root,
            context=context,
            dockerfile=dockerfile,
            tag=tag,
            labels=labels,
            timeout=timeout,
        )
        attempts.append(result)
        if result["returncode"] == 0:
            return {
                "ok": True,
                "runtime": CONTAINERD_RUNTIME,
                "build_backend": f"{fallback}-save-load",
                "tag": tag,
                "context": str(context),
                "dockerfile": str(dockerfile) if dockerfile else None,
                "labels": labels,
                "hardening_profile": hardening_profile,
                "hardening": hardening,
                "cmd": result["cmd"],
                "stdout": result["stdout"],
                "build_summary": result["summary"],
                "attempts": attempts,
            }
    raise RuntimeError(
        json.dumps(
            {
                "ok": False,
                "runtime": CONTAINERD_RUNTIME,
                "tag": tag,
                "context": str(context),
                "dockerfile": str(dockerfile) if dockerfile else None,
                "labels": labels,
                "hardening_profile": hardening_profile,
                "hardening": hardening,
                "attempts": attempts,
                "build_summary": _build_attempts_summary(attempts, tag=tag),
            },
            indent=2,
        )
    )


def _build_with_fallback_and_load(
    *,
    fallback: str,
    containerd_base: list[str],
    state_root: Path,
    context: Path,
    dockerfile: Path | None,
    tag: str,
    labels: list[str],
    timeout: int,
) -> dict[str, Any]:
    build_cmd = [fallback, "build", "-t", tag]
    build_cmd.extend(_container_build_file_args(context, dockerfile))
    build_cmd.append("--no-cache")
    for label in labels:
        build_cmd.extend(["--label", label])
    build_cmd.append(str(context))
    build_proc = subprocess.run(
        build_cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if build_proc.returncode != 0:
        return {
            "backend": fallback,
            "returncode": build_proc.returncode,
            "cmd": build_cmd,
            "stdout": build_proc.stdout,
            "summary": _build_output_summary(build_proc.stdout, backend=fallback, tag=tag),
        }
    transfer_dir = state_root.expanduser().resolve() / "global" / "image-transfer"
    transfer_dir.mkdir(parents=True, exist_ok=True)
    tar_fd, tar_name = tempfile.mkstemp(
        prefix="workerbee-image-",
        suffix=".tar",
        dir=transfer_dir,
    )
    os.close(tar_fd)
    tar_path = Path(tar_name)
    save_cmd = [fallback, "save", "-o", str(tar_path), tag]
    load_cmd = [*containerd_base, "load", "-i", str(tar_path)]
    try:
        save_proc = subprocess.run(
            save_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        if save_proc.returncode != 0:
            return {
                "backend": f"{fallback}-save-load",
                "returncode": save_proc.returncode,
                "cmd": [*build_cmd, "&&", *save_cmd],
                "stdout": build_proc.stdout + "\n" + save_proc.stdout,
                "summary": _build_output_summary(
                    build_proc.stdout + "\n" + save_proc.stdout,
                    backend=f"{fallback}-save-load",
                    tag=tag,
                ),
            }
        load_proc = subprocess.run(
            load_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    finally:
        with suppress(FileNotFoundError):
            tar_path.unlink()
    stdout = build_proc.stdout + "\n" + save_proc.stdout + "\n" + load_proc.stdout
    return {
        "backend": f"{fallback}-save-load",
        "returncode": load_proc.returncode,
        "cmd": [*build_cmd, "&&", *save_cmd, "&&", *load_cmd],
        "stdout": stdout,
        "summary": _build_output_summary(
            stdout,
            backend=f"{fallback}-save-load",
            tag=tag,
        ),
    }


def _build_attempt(backend: str, returncode: int, stdout: str, *, tag: str) -> dict[str, Any]:
    return {
        "backend": backend,
        "returncode": returncode,
        "stdout": stdout,
        "summary": _build_output_summary(stdout, backend=backend, tag=tag),
    }


def _build_attempts_summary(attempts: list[dict[str, Any]], *, tag: str) -> dict[str, Any]:
    stdout = "\n".join(str(item.get("stdout") or "") for item in attempts)
    summary = _build_output_summary(stdout, backend="attempts", tag=tag)
    summary["attempt_count"] = len(attempts)
    summary["failed_backends"] = [
        str(item.get("backend"))
        for item in attempts
        if isinstance(item.get("returncode"), int) and int(item["returncode"]) != 0
    ]
    return summary


def _build_output_summary(stdout: str, *, backend: str, tag: str) -> dict[str, Any]:
    lines = stdout.splitlines()
    warning_lines = [
        line.strip()
        for line in lines
        if line.strip() and _build_log_warning_line(line)
    ]
    error_lines = [
        line.strip()
        for line in lines
        if line.strip() and _build_log_error_line(line)
    ]
    return {
        "backend": backend,
        "tag": tag,
        "line_count": len(lines),
        "warning_count": len(warning_lines),
        "error_count": len(error_lines),
        "warning_lines": warning_lines[:BUILD_SUMMARY_MATCH_LIMIT],
        "error_lines": error_lines[:BUILD_SUMMARY_MATCH_LIMIT],
        "tail_lines": lines[-BUILD_SUMMARY_TAIL_LINES:],
    }


def _build_log_warning_line(line: str) -> bool:
    lowered = line.lower()
    return "warning" in lowered or "deprecated" in lowered


def _build_log_error_line(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in ("error", "failed", "failure", "unable to"))


def _normalize_hardening_profile(profile: str | None) -> str:
    value = (profile or DEFAULT_HARDENING_PROFILE).strip().lower()
    if value not in HARDENING_PROFILES:
        raise WorkerBeeError(
            code="INVALID_HARDENING_PROFILE",
            message="image hardening profile must be one of: standard, hardened",
            details={"profile": profile, "allowed": sorted(HARDENING_PROFILES)},
            remediation=(
                "Use hardening_profile='standard' for existing images or 'hardened' "
                "for generated/minimal images."
            ),
        )
    return value


def _build_file_for_analysis(context: Path, dockerfile: Path | None) -> Path | None:
    if dockerfile is not None:
        return dockerfile
    for name in ("Dockerfile", "Containerfile", "dockerfile", "containerfile"):
        candidate = context / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def _analyze_container_build_file(build_file: Path | None, *, profile: str) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    if build_file is None or not build_file.is_file():
        findings.append(
            _image_hardening_finding(
                code="IMAGE_BUILD_FILE_NOT_FOUND",
                severity="info",
                message=(
                    "No Dockerfile or Containerfile was found for static image "
                    "hardening analysis."
                ),
                remediation=(
                    "Pass dockerfile=... for repo-root builds or add a Containerfile "
                    "to the context."
                ),
            )
        )
        return {
            "profile": profile,
            "build_file": str(build_file) if build_file else None,
            "base_images": [],
            "final_base_image": None,
            "base_image_family": "unknown",
            "minimal_base": False,
            "package_managers": [],
            "declared_user": None,
            "runs_as_non_root": False,
            "exposed_ports": [],
            "multi_stage": False,
            "findings": findings,
            "passed": True,
        }
    try:
        lines = list(_logical_container_build_lines(build_file.read_text(encoding="utf-8")))
    except OSError as exc:
        findings.append(
            _image_hardening_finding(
                code="IMAGE_BUILD_FILE_READ_FAILED",
                severity="info",
                message=f"Could not read build file for static hardening analysis: {exc}",
                remediation="Verify the build file path is readable, then rerun the build.",
            )
        )
        return {
            "profile": profile,
            "build_file": str(build_file),
            "base_images": [],
            "final_base_image": None,
            "base_image_family": "unknown",
            "minimal_base": False,
            "package_managers": [],
            "declared_user": None,
            "runs_as_non_root": False,
            "exposed_ports": [],
            "multi_stage": False,
            "findings": findings,
            "passed": True,
        }
    base_images: list[str] = []
    package_managers: set[str] = set()
    exposed_ports: list[str] = []
    declared_user: str | None = None
    for line in lines:
        from_match = re.match(r"FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)", line, re.IGNORECASE)
        if from_match:
            base_images.append(from_match.group("image"))
            continue
        user_match = re.match(r"USER\s+(?P<user>\S+)", line, re.IGNORECASE)
        if user_match:
            declared_user = user_match.group("user")
            continue
        expose_match = re.match(r"EXPOSE\s+(?P<ports>.+)", line, re.IGNORECASE)
        if expose_match:
            exposed_ports.extend(expose_match.group("ports").split())
            continue
        if re.match(r"RUN\s+", line, re.IGNORECASE):
            lowered = line.lower()
            for name, pattern in _PACKAGE_MANAGER_MARKERS.items():
                if pattern.search(lowered):
                    package_managers.add(name)
    final_base_image = base_images[-1] if base_images else None
    base_family = _base_image_family(final_base_image)
    minimal_base = _base_is_minimal(final_base_image)
    runs_as_non_root = _declared_user_is_non_root(declared_user)
    if not runs_as_non_root:
        findings.append(
            _image_hardening_finding(
                code="IMAGE_USER_ROOT_OR_MISSING",
                severity="medium" if profile == "hardened" else "info",
                message="The final image does not declare a non-root USER.",
                remediation="Add USER 1000 or another non-root runtime user in the final stage.",
            )
        )
    if final_base_image and not minimal_base:
        findings.append(
            _image_hardening_finding(
                code="IMAGE_BASE_NOT_MINIMAL",
                severity="low" if profile == "hardened" else "info",
                message=f"The final base image {final_base_image!r} is not recognized as minimal.",
                remediation=(
                    "Prefer scratch, distroless, Alpine, Wolfi/Chainguard, or a slim "
                    "runtime base when compatible."
                ),
            )
        )
    if package_managers:
        findings.append(
            _image_hardening_finding(
                code="IMAGE_PACKAGE_MANAGER_USAGE",
                severity="low" if profile == "hardened" else "info",
                message="The build file installs packages with a distro package manager.",
                remediation=(
                    "Use multi-stage builds and keep package managers out of the final "
                    "runtime layer where practical."
                ),
            )
        )
    blocking = {"critical", "high", "medium"} if profile == "hardened" else {"critical", "high"}
    return {
        "profile": profile,
        "build_file": str(build_file),
        "base_images": base_images,
        "final_base_image": final_base_image,
        "base_image_family": base_family,
        "minimal_base": minimal_base,
        "package_managers": sorted(package_managers),
        "declared_user": declared_user,
        "runs_as_non_root": runs_as_non_root,
        "exposed_ports": exposed_ports,
        "multi_stage": len(base_images) > 1,
        "findings": findings,
        "passed": not any(item["severity"] in blocking for item in findings),
    }


def _logical_container_build_lines(text: str):
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current = f"{current} {stripped}" if current else stripped
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        yield current
        current = ""
    if current:
        yield current


def _base_image_family(image: str | None) -> str:
    if not image:
        return "unknown"
    lowered = image.lower()
    for marker in ("distroless", "scratch", "chainguard", "wolfi", "alpine", "slim"):
        if marker in lowered:
            return marker
    name = lowered.split("@", 1)[0].split(":", 1)[0].split("/")[-1]
    return name or "unknown"


def _base_is_minimal(image: str | None) -> bool:
    if not image:
        return False
    lowered = image.lower()
    return any(marker in lowered for marker in _MINIMAL_BASE_MARKERS)


def _declared_user_is_non_root(user: str | None) -> bool:
    if not user:
        return False
    principal = user.strip().split()[0].split(":", 1)[0]
    if principal.startswith("$") or "{" in principal or "}" in principal:
        return False
    return principal not in {"", "0", "root"}


def _image_hardening_finding(
    *,
    code: str,
    severity: str,
    message: str,
    remediation: str,
) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "remediation": remediation,
    }


def _cleanup_containers(
    cmd: RuntimeCommand,
    *,
    state_hash: str,
    execute: bool,
) -> list[dict[str, Any]]:
    ids = _ids(
        cmd.run(
            [
                "ps",
                "-aq",
                "--filter",
                f"label=workerbee.state_root_hash={state_hash}",
            ],
            timeout=15,
        ).stdout
    )
    actions = [{"kind": "container", "id": item, "action": "remove"} for item in ids]
    if execute and ids:
        cmd.run(["rm", "-f", *ids], timeout=60)
    return actions


def _cleanup_networks(
    cmd: RuntimeCommand,
    *,
    targets: set[str],
    execute: bool,
) -> list[dict[str, Any]]:
    proc = cmd.run(["network", "ls", "--format", "{{.Name}}"], timeout=15)
    names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    selected = [name for name in names if name in targets]
    actions = [{"kind": "network", "name": name, "action": "remove"} for name in selected]
    if execute:
        for name in selected:
            cmd.run(["network", "rm", name], timeout=30)
    return actions


def _known_project_networks(state_root: Path) -> set[str]:
    return {f"workerbee-{name}" for name in _known_projects(state_root)}


def _known_projects(state_root: Path) -> set[str]:
    root = state_root.expanduser().resolve()
    projects = set()
    registry = root / "registry.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        raw_projects = data.get("projects") if isinstance(data, dict) else None
        if isinstance(raw_projects, dict):
            projects.update(str(name) for name in raw_projects)
    except OSError:
        pass
    except json.JSONDecodeError:
        pass
    projects_dir = root / "projects"
    if projects_dir.is_dir():
        projects.update(path.name for path in projects_dir.iterdir() if path.is_dir())
    return {project_slug_for_runtime(name) for name in projects}


def _cleanup_images(
    cmd: RuntimeCommand,
    *,
    execute: bool,
    state_hash: str | None = None,
    all_images: bool = False,
) -> list[dict[str, Any]]:
    if not all_images and state_hash:
        proc = cmd.run(
            [
                "images",
                "--filter",
                f"label=workerbee.state_root_hash={state_hash}",
                "--format",
                "{{.Repository}}:{{.Tag}} {{.ID}}",
            ],
            timeout=15,
        )
        if proc.returncode == 0:
            images = _image_cleanup_actions_from_rows(
                proc.stdout,
                all_images=True,
                selection="label",
            )
            if execute and images:
                cmd.run(["rmi", "-f", *[str(item["id"]) for item in images]], timeout=120)
            return images
    proc = cmd.run(["images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"], timeout=15)
    images = _image_cleanup_actions_from_rows(
        proc.stdout,
        all_images=all_images,
        selection="namespace" if all_images else "name-fallback",
    )
    if execute and images:
        cmd.run(["rmi", "-f", *[str(item["id"]) for item in images]], timeout=120)
    return images


def _image_cleanup_actions_from_rows(
    raw: str,
    *,
    all_images: bool = False,
    selection: str,
) -> list[dict[str, Any]]:
    images = []
    for line in raw.splitlines():
        ref, _, image_id = line.partition(" ")
        if not image_id:
            continue
        if not all_images and not _workerbee_image_ref(ref):
            continue
        images.append(
            {
                "kind": "image",
                "id": image_id,
                "ref": ref,
                "action": "remove",
                "selection": selection,
            }
        )
    return images


def _workerbee_image_ref(ref: str) -> bool:
    return (
        ref.startswith("workerbee-")
        or ref.startswith("localhost/workerbee-")
        or "/workerbee-" in ref
    )


def _resolve_dockerfile(context: Path, dockerfile: Path | None) -> Path | None:
    if dockerfile is None:
        return None
    raw = dockerfile.expanduser()
    candidates = [raw] if raw.is_absolute() else [context / raw, raw.resolve()]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")


def _container_build_file_args(context: Path, dockerfile: Path | None = None) -> list[str]:
    if dockerfile is not None:
        return ["-f", str(dockerfile)]
    if (context / "Dockerfile").is_file():
        return []
    containerfile = context / "Containerfile"
    if containerfile.is_file():
        return ["-f", str(containerfile)]
    return []


def _ids(raw: str) -> list[str]:
    ids = []
    for line in raw.splitlines():
        item = line.strip()
        if len(item) >= 12 and all(ch in "0123456789abcdef" for ch in item.lower()):
            ids.append(item)
    return ids


def _runtime_version(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    if path is None:
        return {"present": False, "path": None, "version": None}
    try:
        proc = subprocess.run(
            [name, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        version = proc.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        version = f"error: {exc}"
    return {"present": True, "path": path, "version": version}


def _podman_rootless() -> bool | None:
    try:
        proc = subprocess.run(
            ["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        raw = proc.stdout.strip().lower()
        if raw in {"true", "false"}:
            return raw == "true"
    except Exception:
        return None
    return None


def _docker_desktop_hint() -> bool | None:
    try:
        proc = subprocess.run(
            ["docker", "context", "show"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return "desktop" in proc.stdout.strip().lower()
    except Exception:
        return None


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _microk8s_detected() -> bool:
    return MICROK8S_ROOT.exists() or shutil.which("microk8s") is not None


def _is_microk8s_containerd_socket(path: Path) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    return resolved == MICROK8S_CONTAINERD_SOCKET or (
        resolved.name == "containerd.sock" and _is_microk8s_path(resolved)
    )


def _is_microk8s_path(path: Path) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(MICROK8S_ROOT)
        return True
    except ValueError:
        return False


def _netdev_exists(name: str) -> bool:
    return Path("/sys/class/net", name).exists()


def _runtime_guidance() -> str:
    return (
        "Install Podman or Docker for the default workflow, or install nerdctl/containerd and "
        "run WorkerBee with `--runtime containerd`. Direct containerd also requires access "
        "to the configured containerd socket. WorkerBee does not install container runtimes "
        "automatically."
    )


def _state_hash(state_root: Path) -> str:
    return hashlib.sha1(str(state_root.resolve()).encode("utf-8")).hexdigest()[:12]  # noqa: S324


def _containerd_socket_exists(address: str) -> bool | None:
    if address.startswith("unix://"):
        return Path(address.removeprefix("unix://")).exists()
    if address.startswith("/"):
        return Path(address).exists()
    return None


def _list_containerd_namespaces() -> tuple[list[str], str | None]:
    probe = containerd_nerdctl_probe(containerd_address())
    if not probe.get("ok"):
        return [], str(probe.get("message") or probe.get("code") or "nerdctl probe failed")
    return list(probe.get("namespaces") or []), None


def _classify_nerdctl_error(output: str) -> str:
    lowered = output.lower()
    if "rootless containerd not running" in lowered or "containerd-rootless" in lowered:
        return "NERDCTL_ROOTLESS_MODE"
    if "permission denied" in lowered:
        return "CONTAINERD_SOCKET_PERMISSION_DENIED"
    if "no such file or directory" in lowered and "containerd.sock" in lowered:
        return "CONTAINERD_SOCKET_MISSING"
    return "NERDCTL_PROBE_FAILED"


def _raise_if_reserved_containerd_namespace(namespace: str) -> None:
    if namespace in CONTAINERD_RESERVED_NAMESPACES:
        raise WorkerBeeError(
            code="UNSAFE_CONTAINERD_NAMESPACE",
            message=f"Refusing to use reserved containerd namespace `{namespace}`",
            details={
                "namespace": namespace,
                "reserved_namespaces": sorted(CONTAINERD_RESERVED_NAMESPACES),
            },
            remediation=(
                "Use WorkerBee-managed containerd namespaces derived from the WorkerBee "
                "state root; do not target k1s or Kubernetes runtime namespaces directly."
            ),
        )
