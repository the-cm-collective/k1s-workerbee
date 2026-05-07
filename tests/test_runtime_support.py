from __future__ import annotations

from pathlib import Path

from workerbee.runtime_support import (
    containerd_base_args,
    containerd_data_root,
    containerd_namespace,
    runtime_command_args,
    write_containerd_cli_wrapper,
)


def test_containerd_runtime_scopes_project_namespace_and_data_root(tmp_path: Path) -> None:
    args = containerd_base_args(state_root=tmp_path, project="My App")

    assert "--namespace" in args
    assert args[args.index("--namespace") + 1] == "workerbee-my-app"
    assert args[args.index("--data-root") + 1] == str(
        tmp_path / "projects" / "my-app" / "containerd-data"
    )
    assert containerd_namespace(system=True) == "workerbee-system"
    assert containerd_data_root(tmp_path, system=True) == tmp_path / "global" / "containerd-data"


def test_runtime_command_args_uses_nerdctl_only_for_containerd(tmp_path: Path) -> None:
    assert runtime_command_args(
        "docker",
        state_root=tmp_path,
        project="demo",
        args=["ps"],
    ) == ["docker", "ps"]

    args = runtime_command_args(
        "containerd",
        state_root=tmp_path,
        project="demo",
        args=["ps"],
    )

    assert args[-1] == "ps"
    assert args[0] == "nerdctl"
    assert args[args.index("--namespace") + 1] == "workerbee-demo"


def test_containerd_cli_wrapper_routes_caddy_exec_to_system_namespace(tmp_path: Path) -> None:
    wrapper = write_containerd_cli_wrapper(
        tmp_path / "bin" / "nerdctl-workerbee",
        state_root=tmp_path,
        project="demo",
        system_container="workerbee-caddy-abc",
    )

    text = wrapper.read_text(encoding="utf-8")
    assert "workerbee-caddy-abc" in text
    assert "workerbee-system" in text
    assert "workerbee-demo" in text
    assert wrapper.stat().st_mode & 0o111
