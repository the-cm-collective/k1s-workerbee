from pathlib import Path
from types import SimpleNamespace

from workerbee import cli
from workerbee.cli import build_parser


def test_project_mode_accepts_cwd_project_and_open(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "project",
            "mode",
            "lazy",
            "--cwd",
            str(tmp_path),
            "--project",
            "Demo App",
            "--open",
        ]
    )

    assert args.cmd == "project"
    assert args.project_cmd == "mode"
    assert args.mode == "lazy"
    assert args.project_cwd == tmp_path
    assert args.project_name == "Demo App"
    assert args.open is True


def test_mcp_start_accepts_background_bind_flags(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--state-root",
            str(tmp_path),
            "--runtime",
            "containerd",
            "--containerd-privilege",
            "sudo-helper",
            "mcp",
            "start",
            "--host",
            "127.0.0.1",
            "--port",
            "9999",
            "--timeout",
            "1",
        ]
    )

    assert args.cmd == "mcp"
    assert args.mcp_cmd == "start"
    assert args.runtime == "containerd"
    assert args.containerd_privilege == "sudo-helper"
    assert args.state_root == tmp_path
    assert args.host == "127.0.0.1"
    assert args.port == 9999
    assert args.timeout == 1


def test_doctor_reports_requested_runtime(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_runtime_diagnostics(requested: str, **_kwargs):
        calls.append(requested)
        return {"ok": requested == "docker", "requested": requested}

    runtime = SimpleNamespace(
        source="test",
        k1s_root=tmp_path,
        python_executable="python",
        ae_origin="test-ae",
        apply_env=lambda env: env,
    )
    proc = SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(cli, "runtime_diagnostics", fake_runtime_diagnostics)
    monkeypatch.setattr(
        cli,
        "containerd_privilege_status",
        lambda **_kwargs: {"ok": True, "enabled": False},
    )
    monkeypatch.setattr(cli, "resolve_k1s_runtime", lambda: runtime)
    monkeypatch.setattr(cli.subprocess, "run", lambda *_args, **_kwargs: proc)

    result = cli._doctor(runtime="docker", state_root=tmp_path)

    assert calls == ["docker", "containerd"]
    assert result["runtime"]["requested"] == "docker"
    assert result["ok"] is True


def test_containerd_privilege_status_command_parses_global_policy(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--state-root",
            str(tmp_path),
            "--runtime",
            "containerd",
            "--containerd-privilege",
            "unprivileged",
            "containerd-privilege",
            "status",
        ]
    )

    assert args.cmd == "containerd-privilege"
    assert args.containerd_privilege_cmd == "status"
    assert args.runtime == "containerd"
    assert args.containerd_privilege == "unprivileged"


def test_profile_start_parses_direct_containerd_shape(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--state-root",
            str(tmp_path),
            "--runtime",
            "containerd",
            "--project",
            "k1s-dev",
            "profile",
            "start",
            "--profile",
            "k1s-ha-min",
            "--k1s-root",
            str(tmp_path / "k1s"),
            "--timeout",
            "30",
        ]
    )

    assert args.cmd == "profile"
    assert args.profile_cmd == "start"
    assert args.profile == "k1s-ha-min"
    assert args.k1s_root == tmp_path / "k1s"
    assert args.timeout == 30


def test_manifest_profile_target_parses_k1s_profile_shape(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--state-root",
            str(tmp_path),
            "--runtime",
            "containerd",
            "--project",
            "k1s-dev",
            "manifest",
            "deploy-local",
            "--stage",
            str(tmp_path / "stage"),
            "--target",
            "profile",
            "--profile",
            "k1s-dev-min-sqlite",
            "--k1s-root",
            str(tmp_path / "k1s"),
        ]
    )

    assert args.cmd == "manifest"
    assert args.manifest_cmd == "deploy-local"
    assert args.target == "profile"
    assert args.profile == "k1s-dev-min-sqlite"
    assert args.k1s_root == tmp_path / "k1s"


def test_print_returns_failure_for_structured_error(capsys) -> None:
    rc = cli._print({"ok": False, "error": {"code": "MCP_PORT_IN_USE"}}, json_out=True)

    assert rc == 1
    assert "MCP_PORT_IN_USE" in capsys.readouterr().out
