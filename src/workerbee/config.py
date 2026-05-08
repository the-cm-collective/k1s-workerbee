"""Local WorkerBee CLI defaults."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

VALID_RUNTIMES = {"auto", "podman", "docker", "containerd"}
VALID_CONTAINERD_PRIVILEGES = {"auto", "sudo-helper", "unprivileged"}

CONFIG_ENV = "WORKERBEE_CONFIG"
CONFIG_FILE_NAME = "config.json"

DEFAULT_KEYS = {
    "runtime",
    "containerd_privilege",
    "state_root",
    "project",
    "mcp_host",
    "mcp_port",
    "mcp_timeout",
}


def default_config_file() -> Path:
    override = os.getenv(CONFIG_ENV)
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "workerbee" / CONFIG_FILE_NAME).resolve()
    return (Path.home() / ".config" / "workerbee" / CONFIG_FILE_NAME).resolve()


def load_cli_config(path: Path | None = None) -> dict[str, Any]:
    file = (path or default_config_file()).expanduser().resolve()
    if not file.exists():
        return {}
    with file.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"WorkerBee config must be a JSON object: {file}")
    return _validate_config(raw)


def save_cli_config(config: dict[str, Any], path: Path | None = None) -> Path:
    file = (path or default_config_file()).expanduser().resolve()
    clean = _validate_config(config)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return file


def clear_cli_config(path: Path | None = None) -> Path:
    file = (path or default_config_file()).expanduser().resolve()
    with suppress(FileNotFoundError):
        file.unlink()
    return file


def cli_defaults(path: Path | None = None) -> dict[str, Any]:
    defaults = load_cli_config(path)
    env = _env_defaults()
    defaults.update(env)
    return _validate_config(defaults)


def _env_defaults() -> dict[str, Any]:
    values: dict[str, Any] = {}
    mapping = {
        "WORKERBEE_RUNTIME": "runtime",
        "WORKERBEE_CONTAINERD_PRIVILEGE": "containerd_privilege",
        "WORKERBEE_STATE_ROOT": "state_root",
        "WORKERBEE_PROJECT": "project",
        "WORKERBEE_MCP_HOST": "mcp_host",
        "WORKERBEE_MCP_PORT": "mcp_port",
        "WORKERBEE_MCP_TIMEOUT": "mcp_timeout",
    }
    for env_name, key in mapping.items():
        raw = os.getenv(env_name)
        if raw not in (None, ""):
            values[key] = raw
    return values


def _validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for key, value in raw.items():
        if value in (None, ""):
            continue
        if key not in DEFAULT_KEYS:
            continue
        if key == "runtime":
            value = str(value)
            if value not in VALID_RUNTIMES:
                raise ValueError(f"invalid WorkerBee runtime default: {value}")
        elif key == "containerd_privilege":
            value = str(value)
            if value not in VALID_CONTAINERD_PRIVILEGES:
                raise ValueError(f"invalid WorkerBee containerd privilege default: {value}")
        elif key == "state_root":
            value = str(Path(str(value)).expanduser().resolve())
        elif key in {"project", "mcp_host"}:
            value = str(value)
        elif key == "mcp_port":
            value = int(value)
            if value < 1 or value > 65535:
                raise ValueError(f"invalid WorkerBee MCP port default: {value}")
        elif key == "mcp_timeout":
            value = float(value)
            if value <= 0:
                raise ValueError(f"invalid WorkerBee MCP timeout default: {value}")
        config[key] = value
    return config
