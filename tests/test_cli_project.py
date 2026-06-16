import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from workerbee import cli
from workerbee.cli import build_parser

LAN_BIND_HOST = "0.0.0.0"  # noqa: S104 - explicit LAN bind fixture


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


def test_build_image_parser_accepts_hardening_profile(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "build-image",
            str(tmp_path),
            "--tag",
            "workerbee-demo:test",
            "--hardening-profile",
            "hardened",
        ]
    )

    assert args.cmd == "build-image"
    assert args.context == tmp_path
    assert args.tag == "workerbee-demo:test"
    assert args.hardening_profile == "hardened"


def test_runbook_parser_accepts_project_scoped_commands(tmp_path: Path) -> None:
    update = build_parser().parse_args(
        [
            "runbook",
            "update",
            "--cwd",
            str(tmp_path),
            "--project",
            "Demo App",
            "--file",
            str(tmp_path / "runbook.md"),
            "--mode",
            "replace",
            "--source",
            "agent",
            "--summary",
            "first deploy",
        ]
    )

    assert update.cmd == "runbook"
    assert update.runbook_cmd == "update"
    assert update.runbook_cwd == tmp_path
    assert update.runbook_project == "Demo App"
    assert update.runbook_file == tmp_path / "runbook.md"
    assert update.mode == "replace"
    assert update.source == "agent"
    assert update.summary == "first deploy"

    export = build_parser().parse_args(
        [
            "runbook",
            "export",
            "--path",
            "docs/workerbee-runbook.md",
            "--overwrite",
        ]
    )

    assert export.runbook_cmd == "export"
    assert export.path == "docs/workerbee-runbook.md"
    assert export.overwrite is True

    imported = build_parser().parse_args(
        [
            "runbook",
            "import",
            "--path",
            "docs/workerbee-runbook.md",
            "--mode",
            "append",
        ]
    )

    assert imported.runbook_cmd == "import"
    assert imported.path == "docs/workerbee-runbook.md"
    assert imported.mode == "append"


def test_runbook_update_cli_reads_file_content(tmp_path: Path, monkeypatch) -> None:
    runbook_file = tmp_path / "runbook.md"
    runbook_file.write_text("# Runbook\n\nUse validated path.\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, **kwargs: object) -> None:
            captured["init"] = kwargs

        def project_runbook_update(self, **kwargs: object) -> dict[str, object]:
            captured["update"] = kwargs
            return {"ok": True, "project": kwargs["project"], "changed": True}

    monkeypatch.setattr(cli, "WorkerBeeDaemon", FakeDaemon)

    assert (
        cli.main(
            [
                "--json",
                "--state-root",
                str(tmp_path / "state"),
                "runbook",
                "update",
                "--cwd",
                str(tmp_path),
                "--project",
                "Demo App",
                "--file",
                str(runbook_file),
                "--mode",
                "replace",
                "--source",
                "test",
            ]
        )
        == 0
    )

    assert captured["init"]["default_project"] == "Demo App"
    assert captured["update"]["project"] == "Demo App"
    assert captured["update"]["content"] == "# Runbook\n\nUse validated path.\n"
    assert captured["update"]["mode"] == "replace"
    assert captured["update"]["source"] == "test"


def test_runbook_update_json_error_includes_workerbee_error_code(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    class FakeDaemon:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def project_runbook_update(self, **_kwargs: object) -> dict[str, object]:
            raise cli.WorkerBeeError(
                code="RUNBOOK_SECRET_DETECTED",
                message="runbook update appears to contain a secret value",
                details={"findings": [{"field": "content", "line": 1, "marker": "token"}]},
            )

    monkeypatch.setattr(cli, "WorkerBeeDaemon", FakeDaemon)

    assert (
        cli.main(
            [
                "--json",
                "--state-root",
                str(tmp_path / "state"),
                "runbook",
                "update",
                "--cwd",
                str(tmp_path),
                "--project",
                "Demo App",
                "--content",
                "token=abcdefghi",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["ok"] is False
    assert payload["error"]["code"] == "RUNBOOK_SECRET_DETECTED"
    assert payload["error"]["details"] == {
        "findings": [{"field": "content", "line": 1, "marker": "token"}]
    }


def test_logs_cli_accepts_mcp_style_profile_target(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, **kwargs: object) -> None:
            captured["init"] = kwargs

        def profile_logs(self, **kwargs: object) -> dict[str, object]:
            captured["profile_logs"] = kwargs
            return {"ok": True, "stdout": "padawan logs\n"}

    monkeypatch.setattr(cli, "WorkerBeeDaemon", FakeDaemon)
    monkeypatch.setattr(cli, "ensure_containerd_privilege", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "containerd_privilege_env", lambda _privilege: {})
    monkeypatch.setattr(cli, "temporary_containerd_privilege_env", lambda _env: nullcontext())
    monkeypatch.setattr(
        cli,
        "containerd_privilege_summary",
        lambda _privilege: {"runtime": "containerd"},
    )

    rc = cli.main(
        [
            "--json",
            "--state-root",
            str(tmp_path),
            "--project",
            "Demo App",
            "--runtime",
            "containerd",
            "logs",
            "--target",
            "profile",
            "--app",
            "padawan",
            "--profile",
            "k1s-dev-min-sqlite",
            "--tail",
            "120",
        ]
    )

    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["stdout"] == "padawan logs\n"
    assert payload["containerd_privilege"] == {"runtime": "containerd"}
    assert captured["init"]["default_project"] == "Demo App"
    assert captured["profile_logs"] == {
        "app": "padawan",
        "project": "Demo App",
        "profile": "k1s-dev-min-sqlite",
        "namespace": None,
        "tail": 120,
    }


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
            "--allow-remote-mcp",
            "--ingress-exposure",
            "lan",
            "--ingress-domain",
            "workerbee.home.arpa",
            "--ingress-bind",
            LAN_BIND_HOST,
            "--ingress-ca-port",
            "19080",
            "--ingress-dns",
            "forwarding",
            "--ingress-dns-port",
            "1053",
            "--ingress-dns-bind",
            "127.0.0.1",
            "--ingress-dns-answer",
            "192.168.1.23",
            "--ingress-dns-upstream",
            "127.0.0.1:5300",
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
    assert args.allow_remote_mcp is True
    assert args.ingress_exposure == "lan"
    assert args.ingress_domain == "workerbee.home.arpa"
    assert args.ingress_bind == LAN_BIND_HOST
    assert args.ingress_ca_port == 19080
    assert args.ingress_dns == "forwarding"
    assert args.ingress_dns_port == 1053
    assert args.ingress_dns_bind == "127.0.0.1"
    assert args.ingress_dns_answer == "192.168.1.23"
    assert args.ingress_dns_upstream == ["127.0.0.1:5300"]


def test_ingress_ca_parser_accepts_output(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--state-root",
            str(tmp_path),
            "ingress",
            "ca",
            "--output",
            str(tmp_path / "workerbee-ca.crt"),
        ]
    )

    assert args.cmd == "ingress"
    assert args.ingress_cmd == "ca"
    assert args.ca_action == "export"
    assert args.output == tmp_path / "workerbee-ca.crt"


def test_ingress_ca_parser_accepts_regenerate() -> None:
    args = build_parser().parse_args(
        [
            "ingress",
            "ca",
            "regenerate",
            "--confirm-regenerate",
        ]
    )

    assert args.cmd == "ingress"
    assert args.ingress_cmd == "ca"
    assert args.ca_action == "regenerate"
    assert args.confirm_regenerate is True


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
            "--ingress-exposure",
            "lan",
            "--ingress-domain",
            "workerbee.home.arpa",
            "--ingress-bind",
            LAN_BIND_HOST,
            "--ingress-ca-port",
            "19080",
            "--ingress-dns",
            "forwarding",
            "--ingress-dns-port",
            "1053",
            "--ingress-dns-bind",
            "127.0.0.1",
            "--ingress-dns-answer",
            "192.168.1.23",
            "--ingress-dns-upstream",
            "127.0.0.1:5300",
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
    assert args.ingress_exposure == "lan"
    assert args.ingress_domain == "workerbee.home.arpa"
    assert args.ingress_bind == LAN_BIND_HOST
    assert args.ingress_ca_port == 19080
    assert args.ingress_dns == "forwarding"
    assert args.ingress_dns_port == 1053
    assert args.ingress_dns_bind == "127.0.0.1"
    assert args.ingress_dns_answer == "192.168.1.23"
    assert args.ingress_dns_upstream == "127.0.0.1:5300"


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
            '"mcp_port":9999,"mcp_timeout":90,'
            '"ingress_exposure":"lan","ingress_domain":"workerbee.home.arpa",'
            f'"ingress_bind":"{LAN_BIND_HOST}","ingress_ca_port":19080,'
            '"ingress_dns":"forwarding","ingress_dns_port":1053,'
            '"ingress_dns_bind":"127.0.0.1","ingress_dns_answer":"192.168.1.23",'
            '"ingress_dns_upstream":"127.0.0.1:5300"}'
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
    assert config.ingress_exposure == "lan"
    assert config.ingress_domain == "workerbee.home.arpa"
    assert config.ingress_bind == LAN_BIND_HOST
    assert config.ingress_ca_port == 19080
    assert config.ingress_dns == "forwarding"
    assert config.ingress_dns_port == 1053
    assert config.ingress_dns_bind == "127.0.0.1"
    assert config.ingress_dns_answer == "192.168.1.23"
    assert config.ingress_dns_upstreams == ("127.0.0.1:5300",)
    assert captured["timeout"] == 90


def test_ingress_ca_exports_ready_ca(tmp_path: Path, capsys) -> None:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    ca = global_dir / "caddy-local-root.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\ncert\n", encoding="utf-8")
    (global_dir / "ingress.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "runtime": "podman",
                "ca_bundle": str(ca),
                "ca_download_url": "http://ca.workerbee.home.arpa:19080/workerbee-ca.crt",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "workerbee-ca.crt"

    rc = cli.main(
        [
            "--state-root",
            str(tmp_path),
            "ingress",
            "ca",
            "--output",
            str(output),
        ]
    )

    text = capsys.readouterr().out
    assert rc == 0
    assert output.read_text(encoding="utf-8") == ca.read_text(encoding="utf-8")
    assert f"ca exported: {output}" in text
    assert f"ca source: {ca}" in text
    assert "ca url: http://ca.workerbee.home.arpa:19080/workerbee-ca.crt" in text


def test_ingress_ca_regenerate_dispatches_to_daemon(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, **kwargs: object) -> None:
            captured["state_root"] = kwargs["state_root"]
            captured["runtime"] = kwargs["runtime"]

        def ingress_ca_regenerate(self, *, confirm: bool = False) -> dict[str, object]:
            captured["confirm"] = confirm
            return {
                "ok": True,
                "regenerated": True,
                "old_ca_sha256": "old",
                "new_ca_sha256": "new",
            }

    monkeypatch.setattr(cli, "WorkerBeeDaemon", FakeDaemon)

    rc = cli.main(
        [
            "--json",
            "--state-root",
            str(tmp_path),
            "ingress",
            "ca",
            "regenerate",
            "--confirm-regenerate",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["regenerated"] is True
    assert captured["state_root"] == tmp_path
    assert captured["runtime"] == "docker"
    assert captured["confirm"] is True


def test_mcp_status_prints_ca_guidance_for_lan_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def fake_status(config):
        return {
            "ok": True,
            "mcp_url": config.mcp_url,
            "dashboard_url": "https://dashboard.workerbee.home.arpa:19443/",
            "ca_download_url": "http://ca.workerbee.home.arpa:19080/workerbee-ca.crt",
            "ca_commands": {
                "export": "workerbee ingress ca --output workerbee-ca.crt",
                "trust_system": "workerbee trust install --target system",
                "trust_nss": "workerbee trust install --target nss",
                "download_curl": (
                    "curl -fsSL http://ca.workerbee.home.arpa:19080/workerbee-ca.crt "
                    "-o workerbee-ca.crt"
                ),
            },
            "dns": {
                "enabled": True,
                "bind_host": "192.168.1.23",
                "port": 53,
                "base_domain": "workerbee.home.arpa",
            },
            "running": True,
            "state_root": str(tmp_path),
        }

    monkeypatch.setattr(cli, "mcp_daemon_status", fake_status)

    assert cli.main(["--state-root", str(tmp_path), "mcp", "status"]) == 0

    text = capsys.readouterr().out
    assert "ca export: workerbee ingress ca --output workerbee-ca.crt" in text
    assert "local trust: workerbee trust install --target system" in text
    assert "browser trust: workerbee trust install --target nss" in text
    assert "lan device: curl -fsSL http://ca.workerbee.home.arpa:19080/workerbee-ca.crt" in text


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
