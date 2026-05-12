from __future__ import annotations

import subprocess
from pathlib import Path
from stat import S_IMODE

import pytest

from workerbee.contract import MCP_TOOL_NAMES, WorkerBeeError
from workerbee.ingress import ProjectIngressConfig
from workerbee.profiles import K1sProfileInfo, K1sProfileRunner, builtin_profiles


def test_builtin_profiles_are_direct_containerd_only() -> None:
    result = builtin_profiles()

    assert result["runtime_requirement"] == "containerd"
    assert result["host_k1s_processes"] is False
    names = {item["name"] for item in result["profiles"]}
    assert names == {
        "k1s-dev-min-sqlite",
        "k1s-dev-etcd-labs",
        "k1s-single-etcd-containerd",
        "k1s-ha-min",
    }
    assert "workerbee_v1_profile_start" in MCP_TOOL_NAMES
    assert "workerbee_v1_profile_validate" in MCP_TOOL_NAMES
    assert "workerbee_v1_profile_workload_status" in MCP_TOOL_NAMES
    assert "workerbee_v1_profile_workload_validate" in MCP_TOOL_NAMES


def test_profile_runner_rejects_non_containerd_runtime(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("workerbee.profiles.resolve_runtime", lambda _runtime: "docker")
    runner = K1sProfileRunner(
        project="demo",
        state_root=tmp_path,
        runtime="docker",
        k1s_root=tmp_path / "k1s",
    )

    with pytest.raises(WorkerBeeError) as exc:
        runner.start(profile="k1s-dev-min-sqlite", timeout=0.01)

    assert exc.value.code == "K1S_PROFILE_REQUIRES_CONTAINERD"


def test_profile_token_state_files_are_owner_only(tmp_path: Path) -> None:
    runner = K1sProfileRunner(
        project="secure-demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=tmp_path / "k1s",
    )

    tokens = runner._tokens()  # noqa: SLF001
    runner._write_info(  # noqa: SLF001
        K1sProfileInfo(
            project="secure-demo",
            profile="k1s-dev-min-sqlite",
            state_root=str(tmp_path),
            state_dir=str(tmp_path / "projects" / "secure-demo"),
            k1s_root=str(tmp_path / "k1s"),
            runtime="containerd",
            network="workerbee-secure-demo",
            namespace="workerbee-secure-demo",
            started_at=1.0,
            apishim_token=tokens["apishim_token"],
            admin_token=tokens["admin_token"],
            read_token=tokens["read_token"],
        )
    )

    token_file = tmp_path / "projects" / "secure-demo" / "profiles" / "tokens.json"
    assert S_IMODE(token_file.stat().st_mode) == 0o600
    assert S_IMODE(runner.info_file.stat().st_mode) == 0o600


def test_ha_min_starts_only_containerized_components(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.profiles.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr("workerbee.profiles.port_is_free", lambda _port: True)
    monkeypatch.setattr("workerbee.profiles.wait_for_http", lambda *_args, **_kwargs: None)
    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        commands.append([str(part) for part in cmd])
        if "network" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "missing")
        if "ps" in cmd:
            name_filters = [
                str(cmd[index + 1]).removeprefix("name=")
                for index, value in enumerate(cmd[:-1])
                if value == "--filter" and str(cmd[index + 1]).startswith("name=")
            ]
            return subprocess.CompletedProcess(
                cmd,
                0,
                "\n".join(name_filters) + ("\n" if name_filters else ""),
                "",
            )
        if "run" in cmd:
            name = cmd[cmd.index("--name") + 1]
            return subprocess.CompletedProcess(cmd, 0, f"{name}-id\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("workerbee.profiles.subprocess.run", fake_run)

    runner = K1sProfileRunner(
        project="HA Demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=tmp_path / "k1s",
    )
    result = runner.start(profile="k1s-ha-min", timeout=0.01)

    assert result["ok"] is True
    profile = result["profile"]
    assert profile["profile"] == "k1s-ha-min"
    assert profile["runtime"] == "containerd"
    assert len(profile["components"]) == 6
    roles = [component["role"] for component in profile["components"]]
    assert roles == ["etcd", "nats", "apishim", "controller", "controller", "controller"]
    run_commands = [cmd for cmd in commands if "run" in cmd]
    assert len(run_commands) == 6
    assert all("--network" in cmd for cmd in run_commands)
    assert not any("ae.controller" in " ".join(cmd[: cmd.index("run")]) for cmd in run_commands)
    nats_command = next(
        cmd
        for cmd in run_commands
        if cmd[cmd.index("--name") + 1].endswith("-k1s-ha-min-nats")
    )
    assert "-c" in nats_command
    assert nats_command[nats_command.index("-c") + 1] == "/etc/nats/nats.conf"
    assert any(
        volume.endswith("/config/nats.conf:/etc/nats/nats.conf:ro")
        for index, volume in enumerate(nats_command)
        if index > 0 and nats_command[index - 1] == "-v"
    )
    nats_config = (
        tmp_path
        / "projects"
        / "ha-demo"
        / "profiles"
        / "k1s-ha-min"
        / "config"
        / "nats.conf"
    )
    assert 'domain: "K1S"' in nats_config.read_text(encoding="utf-8")


def test_profile_port_allocation_skips_recorded_project_ports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.profiles.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr("workerbee.profiles.port_is_free", lambda _port: True)
    monkeypatch.setattr("workerbee.profiles.wait_for_http", lambda *_args, **_kwargs: None)
    recorded = tmp_path / "projects" / "other" / "profiles"
    recorded.mkdir(parents=True)
    (recorded / "k1s-profile.json").write_text(
        """{
  "components": [
    {"role": "apishim", "host_port": 18645},
    {"role": "controller", "host_port": 19608}
  ]
}
""",
        encoding="utf-8",
    )

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        if "network" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "run" in cmd:
            name = cmd[cmd.index("--name") + 1]
            return subprocess.CompletedProcess(cmd, 0, f"{name}-id\n", "")
        if "ps" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("workerbee.profiles.subprocess.run", fake_run)
    runner = K1sProfileRunner(
        project="demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=tmp_path / "k1s",
    )

    result = runner.start(profile="k1s-dev-min-sqlite", timeout=0.01)

    assert result["profile"]["controller_url"] == "http://127.0.0.1:19609"
    assert result["profile"]["apishim_url"] == "http://127.0.0.1:18646"


def test_profile_status_reads_recorded_components(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("workerbee.profiles.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr("workerbee.profiles.port_is_free", lambda _port: True)
    monkeypatch.setattr("workerbee.profiles.wait_for_http", lambda *_args, **_kwargs: None)
    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        commands.append([str(part) for part in cmd])
        if "network" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "run" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "container-id\n", "")
        if "ps" in cmd:
            name_filters = [
                str(cmd[index + 1]).removeprefix("name=")
                for index, value in enumerate(cmd[:-1])
                if value == "--filter" and str(cmd[index + 1]).startswith("name=")
            ]
            return subprocess.CompletedProcess(
                cmd,
                0,
                "\n".join(name_filters) + ("\n" if name_filters else ""),
                "",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("workerbee.profiles.subprocess.run", fake_run)
    runner = K1sProfileRunner(
        project="demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=tmp_path / "k1s",
    )

    runner.start(profile="k1s-dev-min-sqlite", timeout=0.01)
    status = runner.status()

    assert status["running"] is True
    assert status["profile"]["profile"] == "k1s-dev-min-sqlite"
    assert [item["role"] for item in status["components"]] == ["apishim", "controller"]


def test_existing_profile_start_refreshes_ingress_routes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.profiles.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr("workerbee.profiles.port_is_free", lambda _port: True)
    monkeypatch.setattr("workerbee.profiles.wait_for_http", lambda *_args, **_kwargs: None)

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        if "network" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "run" in cmd:
            name = cmd[cmd.index("--name") + 1]
            return subprocess.CompletedProcess(cmd, 0, f"{name}-id\n", "")
        if "ps" in cmd:
            name_filters = [
                str(cmd[index + 1]).removeprefix("name=")
                for index, value in enumerate(cmd[:-1])
                if value == "--filter" and str(cmd[index + 1]).startswith("name=")
            ]
            return subprocess.CompletedProcess(
                cmd,
                0,
                "\n".join(name_filters) + ("\n" if name_filters else ""),
                "",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("workerbee.profiles.subprocess.run", fake_run)
    runner = K1sProfileRunner(
        project="demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=tmp_path / "k1s",
    )

    first = runner.start(profile="k1s-dev-min-sqlite", timeout=0.01)
    assert first["profile"]["ingress_urls"] == {}

    runner.ingress = ProjectIngressConfig(
        project="demo",
        domain="demo.workerbee.localhost",
        https_port=19443,
        sites_dir=tmp_path / "global" / "caddy-sites" / "demo",
        caddy_container="workerbee-caddy-test",
        caddy_file="/etc/caddy/Caddyfile",
        host_alias="host.docker.internal",
        ca_bundle=tmp_path / "global" / "caddy-local-root.crt",
        global_dashboard_url="https://dashboard.workerbee.localhost:19443/",
        dashboard_port=18090,
    )
    second = runner.start(profile="k1s-dev-min-sqlite", timeout=0.01)

    assert second["started"] is False
    assert second["profile"]["ingress_urls"]["dashboard"] == (
        "https://k1s.demo.workerbee.localhost:19443/dashboard"
    )
    assert second["profile"]["ingress_urls"]["docs"] == (
        "https://k1s.demo.workerbee.localhost:19443/docs"
    )
    assert second["profile"]["ingress_urls"]["api_healthz"] == (
        "https://k1s-api.demo.workerbee.localhost:19443/healthz"
    )
    assert second["profile"]["ingress_urls"]["legacy_dashboard"] == (
        "https://k1s-dash.demo.workerbee.localhost:19443/dashboard"
    )
    route = tmp_path / "global" / "caddy-sites" / "demo" / "k1s-profile.caddy"
    assert route.is_file()
    route_text = route.read_text(encoding="utf-8")
    assert "handle /static/dash-assets/*" in route_text
    assert "reverse_proxy host.docker.internal:18090" in route_text
    assert "handle /api/v1*" in route_text
    assert "reverse_proxy host.docker.internal:18645" in route_text


def test_profile_controller_dashboard_uses_public_apishim_ingress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.profiles.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr("workerbee.profiles.port_is_free", lambda _port: True)
    monkeypatch.setattr("workerbee.profiles.wait_for_http", lambda *_args, **_kwargs: None)
    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        commands.append([str(part) for part in cmd])
        if "network" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "run" in cmd:
            name = cmd[cmd.index("--name") + 1]
            return subprocess.CompletedProcess(cmd, 0, f"{name}-id\n", "")
        if "ps" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("workerbee.profiles.subprocess.run", fake_run)
    runner = K1sProfileRunner(
        project="demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=tmp_path / "k1s",
        ingress=ProjectIngressConfig(
            project="demo",
            domain="demo.workerbee.localhost",
            https_port=19443,
            sites_dir=tmp_path / "global" / "caddy-sites" / "demo",
            caddy_container="workerbee-caddy-test",
            caddy_file="/etc/caddy/Caddyfile",
            host_alias="host.docker.internal",
            ca_bundle=tmp_path / "global" / "caddy-local-root.crt",
            global_dashboard_url="https://dashboard.workerbee.localhost:19443/",
            dashboard_port=18090,
        ),
    )

    result = runner.start(profile="k1s-dev-min-sqlite", timeout=0.01)

    controller_cmd = next(
        cmd
        for cmd in commands
        if "run" in cmd and cmd[cmd.index("--name") + 1].endswith("-controller-0")
    )
    env = {
        controller_cmd[index + 1].split("=", 1)[0]: controller_cmd[index + 1].split("=", 1)[1]
        for index, value in enumerate(controller_cmd[:-1])
        if value == "-e"
    }
    apishim = next(
        component
        for component in result["profile"]["components"]
        if component["role"] == "apishim"
    )
    assert env["AE_APISHIM_SERVER"] == f"http://{apishim['name']}:8445"
    assert env["AE_APISHIM_PUBLIC_BASE"] == "https://k1s.demo.workerbee.localhost:19443"
    assert env["AE_DASHBOARD_BOOTSTRAP_TOKEN"] == env["AE_API_ADMIN_TOKEN"]
    assert env["AE_CADDY_PREFER_HOST_PORT_UPSTREAMS"] == "1"


def test_profile_connection_uses_internal_loopback_and_keeps_public_urls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.profiles.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr("workerbee.profiles.port_is_free", lambda _port: True)
    monkeypatch.setattr("workerbee.profiles.wait_for_http", lambda *_args, **_kwargs: None)

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        if "network" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "run" in cmd:
            name = cmd[cmd.index("--name") + 1]
            return subprocess.CompletedProcess(cmd, 0, f"{name}-id\n", "")
        if "ps" in cmd:
            name_filters = [
                str(cmd[index + 1]).removeprefix("name=")
                for index, value in enumerate(cmd[:-1])
                if value == "--filter" and str(cmd[index + 1]).startswith("name=")
            ]
            return subprocess.CompletedProcess(
                cmd,
                0,
                "\n".join(name_filters) + ("\n" if name_filters else ""),
                "",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    ca_bundle = tmp_path / "global" / "caddy-local-root.crt"
    ca_bundle.parent.mkdir(parents=True)
    ca_bundle.write_text("test-ca", encoding="utf-8")
    monkeypatch.setattr("workerbee.profiles.subprocess.run", fake_run)
    runner = K1sProfileRunner(
        project="demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=tmp_path / "k1s",
        ingress=ProjectIngressConfig(
            project="demo",
            domain="demo.workerbee.localhost",
            https_port=19443,
            sites_dir=tmp_path / "global" / "caddy-sites" / "demo",
            caddy_container="workerbee-caddy-test",
            caddy_file="/etc/caddy/Caddyfile",
            host_alias="127.0.0.1",
            ca_bundle=ca_bundle,
            global_dashboard_url="https://dashboard.workerbee.localhost:19443/",
        ),
    )

    started = runner.start(profile="k1s-dev-min-sqlite", timeout=0.01)
    connection = runner.connection(profile="k1s-dev-min-sqlite")

    assert connection["server"] == started["profile"]["controller_url"]
    assert connection["api_server"] == started["profile"]["apishim_url"]
    assert connection["server"].startswith("http://127.0.0.1:")
    assert connection["api_server"].startswith("http://127.0.0.1:")
    assert connection["public_server"] == "https://k1s.demo.workerbee.localhost:19443/"
    assert connection["public_api_server"] == "https://k1s-api.demo.workerbee.localhost:19443/"
    assert connection["urls"]["dashboard"] == (
        "https://k1s.demo.workerbee.localhost:19443/dashboard"
    )


def test_profile_stop_purge_uses_containerd_helper_for_profile_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.profiles.resolve_runtime", lambda _runtime: "containerd")
    profile_root = tmp_path / "projects" / "demo" / "profiles"
    profile_root.mkdir(parents=True)
    (profile_root / "root-owned-placeholder").write_text("data", encoding="utf-8")
    calls: list[tuple[Path, Path]] = []

    def fake_remove_tree(state_root: Path, target: Path) -> dict[str, object]:
        calls.append((state_root, target))
        return {"ok": True, "removed": True, "path": str(target)}

    runner = K1sProfileRunner(
        project="demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=tmp_path / "k1s",
    )
    monkeypatch.setattr("workerbee.profiles.remove_containerd_helper_tree", fake_remove_tree)
    monkeypatch.setattr(runner, "_rm_network", lambda: {"ok": True})

    result = runner.stop(purge=True)

    assert result["ok"] is True
    assert result["purged"] is True
    assert result["purge_result"] == {
        "ok": True,
        "removed": True,
        "path": str(profile_root),
    }
    assert calls == [(tmp_path.resolve(), profile_root)]
