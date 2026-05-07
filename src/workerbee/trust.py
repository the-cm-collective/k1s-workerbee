"""Explicit local CA trust helpers for WorkerBee."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from workerbee.ingress import load_global_ingress_info


def trust_status(state_root: Path) -> dict[str, Any]:
    info = load_global_ingress_info(state_root)
    ca_raw = str((info or {}).get("ca_bundle") or "")
    ca = Path(ca_raw) if ca_raw else None
    return {
        "state_root": str(state_root.resolve()),
        "ca_bundle": str(ca) if ca is not None else None,
        "ca_ready": bool(ca and ca.is_file()),
        "system_trust_backend": _trust_backend(),
        "certutil": shutil.which("certutil"),
    }


def trust_install(state_root: Path) -> dict[str, Any]:
    status = trust_status(state_root)
    ca_raw = status.get("ca_bundle")
    ca = Path(str(ca_raw or ""))
    if not ca.is_file():
        raise FileNotFoundError(
            "WorkerBee Caddy CA not found; start `workerbee mcp serve` and open the "
            "global dashboard once so Caddy creates its local root"
        )
    installed: list[str] = []
    backend = str(status["system_trust_backend"])
    if backend == "debian":
        _sudo_install(ca, Path("/usr/local/share/ca-certificates/workerbee-caddy-local.crt"))
        _sudo_run(["update-ca-certificates"])
        installed.append("system:debian")
    elif backend == "fedora":
        _sudo_install(ca, Path("/etc/pki/ca-trust/source/anchors/workerbee-caddy-local.crt"))
        _sudo_run(["update-ca-trust", "extract"])
        installed.append("system:fedora")
    if shutil.which("certutil"):
        _install_nss(ca)
        installed.append("nss")
    return {**status, "installed": installed, "ok": bool(installed)}


def _trust_backend() -> str:
    if shutil.which("update-ca-certificates"):
        return "debian"
    if shutil.which("update-ca-trust"):
        return "fedora"
    return "none"


def _sudo_install(src: Path, dst: Path) -> None:
    dst_parent = str(dst.parent)
    _sudo_run(["mkdir", "-p", dst_parent])
    _sudo_run(["install", "-m", "0644", str(src), str(dst)])


def _sudo_run(cmd: list[str]) -> None:
    if os.geteuid() == 0:
        run_cmd = cmd
    else:
        sudo = shutil.which("sudo")
        if sudo is None:
            raise RuntimeError("sudo is required to install WorkerBee CA into system trust")
        run_cmd = [sudo, *cmd]
    subprocess.run(run_cmd, check=True)


def _install_nss(ca: Path) -> None:
    certutil = shutil.which("certutil")
    if certutil is None:
        return
    nssdb = Path.home() / ".pki" / "nssdb"
    nssdb.mkdir(parents=True, exist_ok=True)
    nickname = "WorkerBee Caddy Local Root"
    subprocess.run([certutil, "-d", f"sql:{nssdb}", "-D", "-n", nickname], check=False)
    subprocess.run(
        [certutil, "-d", f"sql:{nssdb}", "-A", "-t", "C,,", "-n", nickname, "-i", str(ca)],
        check=True,
    )
