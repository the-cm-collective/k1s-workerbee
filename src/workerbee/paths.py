"""Path resolution helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root_from_cwd(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def default_state_dir(project: str, *, cwd: Path | None = None) -> Path:
    return repo_root_from_cwd(cwd) / ".workerbee" / project


def resolve_k1s_root(cwd: Path | None = None) -> Path:
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
    tried = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(f"could not locate k1s checkout; tried: {tried}")


def resolve_k1s_python(k1s_root: Path) -> str:
    override = os.getenv("WORKERBEE_PYTHON")
    if override:
        return str(Path(override).expanduser().resolve())
    venv_python = k1s_root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable
