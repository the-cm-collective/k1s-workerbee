"""k1s runtime resolution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from workerbee.paths import resolve_k1s_python, resolve_k1s_root


@dataclass(frozen=True, slots=True)
class K1sRuntime:
    source: str
    python_executable: str
    k1s_root: Path | None
    pythonpath: str | None
    ae_origin: str | None
    ae_version: str | None = None

    def apply_env(self, env: dict[str, str]) -> dict[str, str]:
        if not self.pythonpath:
            return env
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{self.pythonpath}{os.pathsep}{current}" if current else self.pythonpath
        )
        return env


def resolve_k1s_runtime(
    *,
    cwd: Path | None = None,
    k1s_root: Path | None = None,
) -> K1sRuntime:
    """Resolve installed k1s first, unless a checkout is explicitly requested."""

    explicit_root = k1s_root or _env_root()
    if explicit_root is not None:
        return _runtime_from_root(explicit_root)

    python_bin = _env_python() or sys.executable
    installed = _probe_ae_info(python_bin, pythonpath=None)
    if installed is not None:
        return K1sRuntime(
            source="installed",
            python_executable=python_bin,
            k1s_root=None,
            pythonpath=None,
            ae_origin=installed["origin"],
            ae_version=installed.get("version"),
        )

    return _runtime_from_root(resolve_k1s_root(cwd))


def _runtime_from_root(root: Path) -> K1sRuntime:
    resolved = root.expanduser().resolve()
    python_bin = resolve_k1s_python(resolved)
    pythonpath = str(resolved / "src")
    info = _probe_ae_info(python_bin, pythonpath=pythonpath)
    if info is None:
        raise RuntimeError(f"k1s checkout is not importable with {python_bin}: {resolved}")
    return K1sRuntime(
        source="sibling",
        python_executable=python_bin,
        k1s_root=resolved,
        pythonpath=pythonpath,
        ae_origin=info["origin"],
        ae_version=info.get("version"),
    )


def _env_root() -> Path | None:
    raw = os.getenv("WORKERBEE_K1S_ROOT")
    return Path(raw) if raw else None


def _env_python() -> str | None:
    raw = os.getenv("WORKERBEE_PYTHON")
    return str(Path(raw).expanduser().resolve()) if raw else None


def _probe_ae(python_bin: str, *, pythonpath: str | None) -> str | None:
    info = _probe_ae_info(python_bin, pythonpath=pythonpath)
    return info["origin"] if info else None


def _probe_ae_info(python_bin: str, *, pythonpath: str | None) -> dict[str, str | None] | None:
    env = os.environ.copy()
    if pythonpath:
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{pythonpath}{os.pathsep}{current}" if current else pythonpath
    code = """
import json
import ae
import ae.apishim.__main__
import ae.cli.__main__
import ae.controller.__main__
print(json.dumps({
    "origin": getattr(ae, "__file__", None),
    "version": getattr(ae, "__version__", None),
}))
"""
    proc = subprocess.run(
        [python_bin, "-c", code],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return None
    return {
        "origin": str(data.get("origin") or ""),
        "version": str(data["version"]) if data.get("version") is not None else None,
    }
