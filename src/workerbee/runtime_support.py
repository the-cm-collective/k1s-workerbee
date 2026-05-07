"""Container runtime diagnostics and cleanup helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError

WORKERBEE_LABEL = "workerbee.managed=true"
CONTAINERD_RUNTIME = "containerd"
CONTAINERD_RESERVED_NAMESPACES = frozenset({"ae", "k8s.io", "moby", "default"})


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
        actions.extend(_cleanup_images(cmd, execute=execute))
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
    nerdctl = shutil.which(nerdctl_binary())
    using_workerbee_helper = bool(os.getenv("WORKERBEE_CONTAINERD_HELPER_SOCKET"))
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
    return (
        os.getenv("WORKERBEE_CONTAINERD_CNI_BIN_DIR")
        or os.getenv("AE_CONTAINERD_CNI_BIN_DIR")
        or os.getenv("CNI_PATH")
        or "/opt/cni/bin"
    )


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
) -> list[str]:
    data_root = containerd_data_root(state_root, project=project, system=system)
    cni_conf = containerd_cni_conf_dir(state_root, project=project, system=system)
    data_root.mkdir(parents=True, exist_ok=True)
    cni_conf.mkdir(parents=True, exist_ok=True)
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


def runtime_command_args(
    runtime: str,
    *,
    state_root: Path,
    project: str | None,
    args: list[str],
    system: bool = False,
) -> list[str]:
    if runtime == CONTAINERD_RUNTIME:
        return [*containerd_base_args(state_root=state_root, project=project, system=system), *args]
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
    labels: list[str] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    selected = resolve_runtime(runtime)
    build_context = context.expanduser().resolve()
    label_values = labels or workerbee_runtime_labels(state_root=state_root, project=project)
    if selected == CONTAINERD_RUNTIME:
        return _build_image_containerd(
            state_root=state_root,
            project=project,
            context=build_context,
            tag=tag,
            labels=label_values,
            timeout=timeout,
        )
    cmd = [selected, "build", "-t", tag]
    cmd.extend(_container_build_file_args(build_context))
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
        "labels": label_values,
        "cmd": cmd,
        "stdout": proc.stdout,
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
    namespaces = [system_namespace] + ([project_namespace] if project_namespace else [])
    reserved_overlap = sorted(set(namespaces).intersection(CONTAINERD_RESERVED_NAMESPACES))
    active_namespaces, namespace_probe_error = _list_containerd_namespaces()
    non_workerbee_namespaces = [
        item for item in active_namespaces if not item.startswith(f"workerbee-{_state_hash(root)}-")
    ]
    warnings = []
    if _containerd_socket_exists(containerd_address()):
        warnings.append(
            "WorkerBee is using a shared host containerd socket; isolation relies on "
            "WorkerBee state-hash namespaces and state-local nerdctl/CNI roots."
        )
    return {
        "state_hash": _state_hash(root),
        "address": containerd_address(),
        "socket_exists": _containerd_socket_exists(containerd_address()),
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
        "system_cni_conf_dir": str(containerd_cni_conf_dir(root, system=True)),
        "project_cni_conf_dir": (
            str(containerd_cni_conf_dir(root, project=project_name)) if project_name else None
        ),
        "cni_bin_dir": containerd_cni_bin_dir(),
        "warnings": warnings,
        "ok": not reserved_overlap,
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
            for action in _cleanup_images(cmd, execute=execute):
                action["namespace"] = namespace
                actions.append(action)
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
    tag: str,
    labels: list[str],
    timeout: int,
) -> dict[str, Any]:
    base = containerd_base_args(state_root=state_root, project=project)
    cmd = [*base, "build", "-t", tag]
    cmd.extend(_container_build_file_args(context))
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
    attempts = [{"backend": "nerdctl", "returncode": proc.returncode, "stdout": proc.stdout}]
    if proc.returncode == 0:
        return {
            "ok": True,
            "runtime": CONTAINERD_RUNTIME,
            "build_backend": "nerdctl",
            "tag": tag,
            "context": str(context),
            "labels": labels,
            "cmd": cmd,
            "stdout": proc.stdout,
        }
    for fallback in ("podman", "docker"):
        if shutil.which(fallback) is None:
            continue
        result = _build_with_fallback_and_load(
            fallback=fallback,
            containerd_base=base,
            context=context,
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
                "labels": labels,
                "cmd": result["cmd"],
                "stdout": result["stdout"],
                "attempts": attempts,
            }
    raise RuntimeError(
        json.dumps(
            {
                "ok": False,
                "runtime": CONTAINERD_RUNTIME,
                "tag": tag,
                "context": str(context),
                "labels": labels,
                "attempts": attempts,
            },
            indent=2,
        )
    )


def _build_with_fallback_and_load(
    *,
    fallback: str,
    containerd_base: list[str],
    context: Path,
    tag: str,
    labels: list[str],
    timeout: int,
) -> dict[str, Any]:
    build_cmd = [fallback, "build", "-t", tag]
    build_cmd.extend(_container_build_file_args(context))
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
        }
    save_proc = subprocess.Popen(
        [fallback, "save", tag],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert save_proc.stdout is not None
    try:
        load_proc = subprocess.run(
            [*containerd_base, "load"],
            stdin=save_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        save_proc.stdout.close()
        _save_stdout, save_stderr_bytes = save_proc.communicate(timeout=timeout)
        save_stderr = save_stderr_bytes.decode("utf-8", errors="replace")
        save_returncode = int(save_proc.returncode or 0)
    finally:
        if save_proc.poll() is None:
            save_proc.kill()
    stdout = (
        build_proc.stdout
        + "\n"
        + load_proc.stdout.decode("utf-8", errors="replace")
        + ("\n" + save_stderr if save_stderr else "")
    )
    return {
        "backend": f"{fallback}-save-load",
        "returncode": load_proc.returncode or save_returncode,
        "cmd": [*build_cmd, "&&", fallback, "save", tag, "|", *containerd_base, "load"],
        "stdout": stdout,
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


def _cleanup_images(cmd: RuntimeCommand, *, execute: bool) -> list[dict[str, Any]]:
    proc = cmd.run(["images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"], timeout=15)
    images = []
    for line in proc.stdout.splitlines():
        ref, _, image_id = line.partition(" ")
        if ref.startswith("workerbee-") and image_id:
            images.append({"kind": "image", "id": image_id, "ref": ref, "action": "remove"})
    if execute and images:
        cmd.run(["rmi", "-f", *[str(item["id"]) for item in images]], timeout=120)
    return images


def _container_build_file_args(context: Path) -> list[str]:
    if (context / "Dockerfile").is_file():
        return []
    containerfile = context / "Containerfile"
    if containerfile.is_file():
        return ["-f", str(containerfile)]
    return []


def _ids(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


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
