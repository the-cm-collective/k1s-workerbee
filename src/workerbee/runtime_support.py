"""Container runtime diagnostics and cleanup helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError

WORKERBEE_LABEL = "workerbee.managed=true"
CONTAINERD_RUNTIME = "containerd"
CONTAINERD_SYSTEM_NAMESPACE = "workerbee-system"


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


def runtime_diagnostics(requested: str = "auto") -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "requested": requested,
        "podman": _runtime_version("podman"),
        "docker": _runtime_version("docker"),
        "nerdctl": _runtime_version(nerdctl_binary()),
        "buildctl": _runtime_version(buildctl_binary()),
        "containerd": {
            "address": containerd_address(),
            "socket_exists": _containerd_socket_exists(containerd_address()),
            "system_namespace": CONTAINERD_SYSTEM_NAMESPACE,
        },
    }
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


def containerd_namespace(project: str | None = None, *, system: bool = False) -> str:
    if system or not project:
        return CONTAINERD_SYSTEM_NAMESPACE
    return f"workerbee-{project_slug_for_runtime(project)}"


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


def containerd_base_args(
    *,
    state_root: Path,
    project: str | None = None,
    system: bool = False,
) -> list[str]:
    data_root = containerd_data_root(state_root, project=project, system=system)
    data_root.mkdir(parents=True, exist_ok=True)
    return [
        nerdctl_binary(),
        "--address",
        containerd_address(),
        "--namespace",
        containerd_namespace(project, system=system),
        "--data-root",
        str(data_root),
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


def _cleanup_containerd_runtime(
    *,
    state_root: Path,
    execute: bool,
    purge_images: bool,
) -> dict[str, Any]:
    state_hash = _state_hash(state_root)
    project_names = {
        name.removeprefix("workerbee-") for name in _known_project_networks(state_root)
    }
    namespaces = [(CONTAINERD_SYSTEM_NAMESPACE, None)]
    namespaces.extend(
        (containerd_namespace(project), project)
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
            targets = {f"workerbee-{project}"}
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
    return {f"workerbee-{name}" for name in projects}


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
        "run WorkerBee with `--runtime containerd`. WorkerBee does not install container "
        "runtimes automatically."
    )


def _state_hash(state_root: Path) -> str:
    return hashlib.sha1(str(state_root.resolve()).encode("utf-8")).hexdigest()[:12]  # noqa: S324


def _containerd_socket_exists(address: str) -> bool | None:
    if address.startswith("unix://"):
        return Path(address.removeprefix("unix://")).exists()
    if address.startswith("/"):
        return Path(address).exists()
    return None
