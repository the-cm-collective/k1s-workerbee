"""Path resolution helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root_from_cwd(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def default_state_dir(project: str, *, cwd: Path | None = None) -> Path:
    return repo_root_from_cwd(cwd) / ".workerbee" / project


def default_state_root() -> Path:
    override = os.getenv("WORKERBEE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "workerbee").resolve()
    return (Path.home() / ".local" / "share" / "workerbee").resolve()


def daemon_project_state_dir(project: str, *, state_root: Path | None = None) -> Path:
    return (state_root or default_state_root()).resolve() / "projects" / project


def find_k1s_root(cwd: Path | None = None) -> Path | None:
    override = os.getenv("WORKERBEE_K1S_ROOT")
    candidates = []
    if override:
        candidates.append(Path(override))
    base = repo_root_from_cwd(cwd)
    candidates.extend([base.parent / "k1s", base / "k1s"])
    for candidate in candidates:
        root = candidate.expanduser().resolve()
        if (root / "src" / "ae").is_dir() and (root / "pyproject.toml").is_file():
            return root
    return None


def resolve_k1s_root(cwd: Path | None = None) -> Path:
    root = find_k1s_root(cwd)
    if root is not None:
        return root
    override = os.getenv("WORKERBEE_K1S_ROOT")
    base = repo_root_from_cwd(cwd)
    tried_paths = [Path(override)] if override else []
    tried_paths.extend([base.parent / "k1s", base / "k1s"])
    tried = ", ".join(str(p) for p in tried_paths)
    raise FileNotFoundError(f"could not locate k1s checkout; tried: {tried}")


def resolve_k1s_python(k1s_root: Path) -> str:
    override = os.getenv("WORKERBEE_PYTHON")
    if override:
        return str(Path(override).expanduser().resolve())
    venv_python = k1s_root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable
