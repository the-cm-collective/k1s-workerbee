from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


def _source_env() -> dict[str, str]:
    env = os.environ.copy()
    source = str(Path("src").resolve())
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{source}{os.pathsep}{current}" if current else source
    return env


def test_distribution_import_cli_and_mcp_identity_contract() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    release = pyproject["tool"]["workerbee"]["release"]

    assert project["name"] == "k1s-workerbee"
    assert release["import-package"] == "workerbee"
    assert project["scripts"]["workerbee"] == "workerbee.cli:main"
    assert release["mcp-entry-point"] == "workerbee.mcp_server:serve_mcp"
    assert release["runtime-import"] == "ae"


def test_module_cli_entry_point_smoke() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "workerbee", "--version"],
        env=_source_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "workerbee 1.0.0a1"


def test_mcp_serve_cli_entry_point_smoke() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "workerbee", "mcp", "serve", "--help"],
        env=_source_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "--allow-remote-mcp" in proc.stdout
