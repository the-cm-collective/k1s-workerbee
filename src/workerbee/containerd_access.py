"""Deprecated cleanup for WorkerBee containerd socket ACL leases."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError

CONTAINERD_SOCKET_ACCESS_FILE = "containerd-socket-access.json"


def release_containerd_socket_access(
    *,
    state_root: Path,
    runtime: str = "containerd",
    mode: str = "auto",
    owner_pid: int | None = None,
    lease_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    _ = (runtime, mode)
    root = state_root.expanduser().resolve()
    registry = _read_registry(root)
    current = [lease for lease in registry.get("leases") or [] if isinstance(lease, dict)]

    def should_remove(lease: dict[str, Any]) -> bool:
        if lease_id:
            return lease.get("id") == lease_id
        if owner_pid is not None:
            return int(lease.get("pid") or 0) == int(owner_pid)
        if force:
            return True
        return not _pid_alive(int(lease.get("pid") or 0))

    removed = [lease for lease in current if should_remove(lease)]
    kept = [lease for lease in current if not should_remove(lease)]
    errors: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for lease in removed:
        if not lease.get("added_acl") or lease.get("preexisting_access"):
            continue
        raw_socket = str(lease.get("socket") or "")
        user = str(lease.get("user") or "")
        if not raw_socket or not user:
            continue
        socket_path = Path(raw_socket)
        active_peer = any(
            kept_lease.get("socket") == str(socket_path)
            and kept_lease.get("user") == user
            and _pid_alive(int(kept_lease.get("pid") or 0))
            for kept_lease in kept
            if isinstance(kept_lease, dict)
        )
        if active_peer:
            actions.append(
                {
                    "kind": "acl",
                    "action": "keep",
                    "socket": str(socket_path),
                    "user": user,
                    "reason": "another active WorkerBee lease exists",
                }
            )
            continue
        if not socket_path.exists():
            actions.append(
                {
                    "kind": "acl",
                    "action": "skip",
                    "socket": str(socket_path),
                    "user": user,
                    "reason": "socket missing",
                }
            )
            continue
        try:
            _run_acl(
                ["setfacl", "-x", f"u:{user}", str(socket_path)],
                action="revoke deprecated containerd socket ACL",
            )
            actions.append(
                {"kind": "acl", "action": "revoke", "socket": str(socket_path), "user": user}
            )
        except WorkerBeeError as exc:
            errors.append(exc.public_dict())
            kept.append(lease)
    _write_registry(root, {"leases": kept})
    return {
        "ok": not errors,
        "deprecated": True,
        "released": bool(removed) and not errors,
        "removed_leases": removed,
        "remaining_leases": _annotated_leases(kept),
        "actions": actions,
        "errors": errors,
    }


def _lease_file(state_root: Path) -> Path:
    return state_root.expanduser().resolve() / "global" / CONTAINERD_SOCKET_ACCESS_FILE


def _read_registry(state_root: Path) -> dict[str, Any]:
    path = _lease_file(state_root)
    if not path.is_file():
        return {"leases": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"leases": []}
    return data if isinstance(data, dict) else {"leases": []}


def _write_registry(state_root: Path, data: dict[str, Any]) -> None:
    path = _lease_file(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _annotated_leases(leases: list[Any]) -> list[dict[str, Any]]:
    annotated = []
    for lease in leases:
        if not isinstance(lease, dict):
            continue
        pid = int(lease.get("pid") or 0)
        active = _pid_alive(pid)
        annotated.append({**lease, "active": active, "stale": bool(pid and not active)})
    return annotated


def _run_acl(args: list[str], *, action: str) -> subprocess.CompletedProcess[str]:
    sudo = shutil.which("sudo")
    cmd = args if os.geteuid() == 0 or sudo is None else [sudo, *args]
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    if proc.returncode != 0:
        raise WorkerBeeError(
            code="CONTAINERD_SOCKET_ACL_REVOKE_FAILED",
            message=f"failed to {action}",
            details={"cmd": cmd, "returncode": proc.returncode, "stdout": proc.stdout},
            remediation="Check sudo privileges and ACL support for the containerd socket.",
            retryable=True,
        )
    return proc


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
