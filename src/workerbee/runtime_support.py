"""Container runtime diagnostics and cleanup helpers."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError

WORKERBEE_LABEL = "workerbee.managed=true"


@dataclass(frozen=True, slots=True)
class RuntimeCommand:
    runtime: str

    def run(
        self,
        args: list[str],
        *,
        timeout: int = 30,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [self.runtime, *args],
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
    if requested in {"podman", "docker"}:
        if shutil.which(requested) is None:
            raise WorkerBeeError(
                code="RUNTIME_MISSING",
                message=f"{requested} not found on PATH",
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
    }
    try:
        runtime = resolve_runtime(requested)
        diagnostics["selected"] = runtime
        diagnostics["ok"] = True
        if runtime == "podman":
            diagnostics["podman_rootless"] = _podman_rootless()
        if runtime == "docker":
            diagnostics["docker_desktop_hint"] = _docker_desktop_hint()
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


def workerbee_runtime_labels(*, state_root: Path, project: str | None = None) -> list[str]:
    labels = [
        WORKERBEE_LABEL,
        f"workerbee.state_root_hash={_state_hash(state_root)}",
    ]
    if project:
        labels.append(f"workerbee.project={project}")
    return labels


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
        "Install Podman or Docker, ensure it is on PATH, then run `workerbee doctor` again. "
        "WorkerBee does not install container runtimes automatically."
    )


def _state_hash(state_root: Path) -> str:
    return hashlib.sha1(str(state_root.resolve()).encode("utf-8")).hexdigest()[:12]  # noqa: S324
