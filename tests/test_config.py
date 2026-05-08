from __future__ import annotations

import json
from pathlib import Path

from workerbee.config import cli_defaults, default_config_file, load_cli_config, save_cli_config


def test_cli_config_uses_xdg_config_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("WORKERBEE_CONFIG", raising=False)

    assert default_config_file() == tmp_path / "config" / "workerbee" / "config.json"


def test_cli_config_validates_and_normalizes_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    saved = save_cli_config(
        {
            "runtime": "containerd",
            "containerd_privilege": "sudo-helper",
            "state_root": tmp_path / "state",
            "mcp_port": "8765",
            "mcp_timeout": "90",
            "unknown": "ignored",
        },
        path,
    )

    assert saved == path
    assert load_cli_config(path) == {
        "runtime": "containerd",
        "containerd_privilege": "sudo-helper",
        "state_root": str((tmp_path / "state").resolve()),
        "mcp_port": 8765,
        "mcp_timeout": 90.0,
    }


def test_cli_defaults_env_overrides_config(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"runtime": "podman", "mcp_port": 8765}), encoding="utf-8")
    monkeypatch.setenv("WORKERBEE_RUNTIME", "containerd")
    monkeypatch.setenv("WORKERBEE_MCP_PORT", "9999")

    defaults = cli_defaults(path)

    assert defaults["runtime"] == "containerd"
    assert defaults["mcp_port"] == 9999
