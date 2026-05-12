from __future__ import annotations

import json
from pathlib import Path

from workerbee.config import cli_defaults, default_config_file, load_cli_config, save_cli_config

LAN_BIND_HOST = "0.0.0.0"  # noqa: S104 - explicit LAN bind fixture


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
            "ingress_exposure": "lan",
            "ingress_domain": "workerbee.home.arpa",
            "ingress_bind": LAN_BIND_HOST,
            "ingress_ca_port": "19080",
            "ingress_dns": "forwarding",
            "ingress_dns_port": "1053",
            "ingress_dns_bind": "127.0.0.1",
            "ingress_dns_answer": "192.168.1.23",
            "ingress_dns_upstream": "127.0.0.1:5300",
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
        "ingress_exposure": "lan",
        "ingress_domain": "workerbee.home.arpa",
        "ingress_bind": LAN_BIND_HOST,
        "ingress_ca_port": 19080,
        "ingress_dns": "forwarding",
        "ingress_dns_port": 1053,
        "ingress_dns_bind": "127.0.0.1",
        "ingress_dns_answer": "192.168.1.23",
        "ingress_dns_upstream": "127.0.0.1:5300",
    }


def test_cli_defaults_env_overrides_config(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"runtime": "podman", "mcp_port": 8765}), encoding="utf-8")
    monkeypatch.setenv("WORKERBEE_RUNTIME", "containerd")
    monkeypatch.setenv("WORKERBEE_MCP_PORT", "9999")
    monkeypatch.setenv("WORKERBEE_INGRESS_EXPOSURE", "lan")

    defaults = cli_defaults(path)

    assert defaults["runtime"] == "containerd"
    assert defaults["mcp_port"] == 9999
    assert defaults["ingress_exposure"] == "lan"
