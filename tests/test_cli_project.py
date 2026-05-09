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


def test_config_set_parses_user_level_defaults(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "config",
            "set",
            "--runtime",
            "containerd",
            "--containerd-privilege",
            "sudo-helper",
            "--state-root",
            str(tmp_path),
            "--mcp-host",
            "127.0.0.1",
            "--mcp-port",
            "8765",
            "--mcp-timeout",
            "90",
        ]
    )

    assert args.cmd == "config"
    assert args.config_cmd == "set"
    assert args.runtime == "containerd"
    assert args.containerd_privilege == "sudo-helper"
    assert args.state_root == tmp_path
    assert args.mcp_host == "127.0.0.1"
    assert args.mcp_port == 8765
    assert args.mcp_timeout == 90


def test_agent_install_parser_accepts_explicit_append(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "agent",
            "install",
            "--target",
            str(tmp_path / "AGENTS.md"),
            "--append",
            "--allow-create",
        ]
    )

    assert args.cmd == "agent"
    assert args.agent_cmd == "install"
    assert args.target == tmp_path / "AGENTS.md"
    assert args.append is True
    assert args.allow_create is True


def test_agent_instructions_cli_prints_canonical_block(capsys) -> None:
    assert cli.main(["agent", "instructions"]) == 0

    output = capsys.readouterr().out
    assert "workerbee-agent-instructions:v1 start" in output
    assert "first time WorkerBee is coming up" in output


def test_agent_install_cli_appends_to_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("# Existing\n", encoding="utf-8")

    assert cli.main(["agent", "install", "--target", str(target), "--append"]) == 0

    text = target.read_text(encoding="utf-8")
    assert "# Existing" in text
    assert "workerbee-agent-instructions:v1 start" in text


def test_cli_defaults_apply_to_mcp_commands(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        (
            '{"runtime":"containerd","containerd_privilege":"sudo-helper",'
            f'"state_root":"{tmp_path / "state"}","mcp_host":"127.0.0.2",'
            '"mcp_port":9999,"mcp_timeout":90}'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKERBEE_CONFIG", str(config_file))
    captured = {}

    def fake_restart(config, *, timeout):
        captured["config"] = config
        captured["timeout"] = timeout
        return {"ok": True, "mcp_url": config.mcp_url, "running": True}

    monkeypatch.setattr(cli, "restart_mcp_daemon", fake_restart)

    assert cli.main(["--json", "mcp", "restart"]) == 0

    config = captured["config"]
    assert config.runtime == "containerd"
    assert config.containerd_privilege == "sudo-helper"
    assert config.state_root == tmp_path / "state"
    assert config.host == "127.0.0.2"
    assert config.port == 9999
    assert captured["timeout"] == 90


def test_explicit_cli_flags_override_defaults(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        '{"runtime":"containerd","state_root":"/tmp/wrong","mcp_port":9999}',
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKERBEE_CONFIG", str(config_file))
    captured = {}

    def fake_status(config):
        captured["config"] = config
        return {"ok": True, "mcp_url": config.mcp_url, "running": True}

    monkeypatch.setattr(cli, "mcp_daemon_status", fake_status)

    assert (
        cli.main(
            [
                "--json",
                "--runtime",
                "podman",
                "--state-root",
                str(tmp_path / "explicit"),
                "mcp",
                "status",
                "--port",
                "7777",
            ]
        )
        == 0
    )

    config = captured["config"]
    assert config.runtime == "podman"
    assert config.state_root == tmp_path / "explicit"
    assert config.port == 7777


def test_containerd_supervisor_command_uses_state_root_and_privilege(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured = {}

    class FakeSupervisor:
        def __init__(self, *, project, runtime, state_dir, cwd):
            captured["project"] = project
            captured["runtime"] = runtime
            captured["state_dir"] = state_dir
            captured["cwd"] = cwd
            self.state_dir = state_dir

        def deploy_poc_stack(self, *, timeout_seconds):
            captured["timeout"] = timeout_seconds
            captured["helper_env"] = cli.os.environ.get("WORKERBEE_NERDCTL_BIN")
            return {"ok": True, "state_dir": str(self.state_dir)}

    def fake_ensure_containerd_privilege(**kwargs):
        captured["privilege_kwargs"] = kwargs
        return {"runtime": "containerd", "effective_mode": "sudo-helper"}

    monkeypatch.setattr(cli, "WorkerBeeSupervisor", FakeSupervisor)
    monkeypatch.setattr(cli, "ensure_containerd_privilege", fake_ensure_containerd_privilege)
    monkeypatch.setattr(
        cli,
        "containerd_privilege_env",
        lambda _privilege: {"WORKERBEE_NERDCTL_BIN": str(tmp_path / "workerbee-nerdctl")},
    )

    rc = cli.main(
        [
            "--json",
            "--state-root",
            str(tmp_path),
            "--runtime",
            "containerd",
            "--containerd-privilege",
            "sudo-helper",
            "--project",
            "demo",
            "deploy-poc",
            "--timeout",
            "1",
        ]
    )

    assert rc == 0
    assert captured["state_dir"] == tmp_path / "projects" / "demo"
    assert captured["privilege_kwargs"]["state_root"] == tmp_path
    assert captured["privilege_kwargs"]["runtime"] == "containerd"
    assert captured["privilege_kwargs"]["mode"] == "sudo-helper"
    assert captured["helper_env"] == str(tmp_path / "workerbee-nerdctl")


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
