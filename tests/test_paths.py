from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace

from workerbee.ingress import ProjectIngressConfig
from workerbee.k1s_runtime import K1sRuntime
from workerbee.paths import daemon_project_state_dir, default_state_dir, default_state_root
from workerbee.runtime_support import (
    containerd_namespace,
    containerd_network_name,
    containerd_network_subnet,
)
from workerbee.supervisor import StackInfo, WorkerBeeSupervisor


def test_default_state_dir_is_project_scoped(tmp_path: Path) -> None:
    assert default_state_dir("demo", cwd=tmp_path) == tmp_path / ".workerbee" / "demo"


def test_default_state_root_honors_workerbee_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKERBEE_HOME", str(tmp_path / "home"))
    assert default_state_root() == tmp_path / "home"
    assert daemon_project_state_dir("demo") == tmp_path / "home" / "projects" / "demo"


def test_project_slug_and_state_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    sup = WorkerBeeSupervisor(
        project="My Project!",
        state_dir=tmp_path / "state",
        runtime="docker",
        cwd=tmp_path,
    )
    assert sup.project == "my-project"
    assert sup.state_dir == tmp_path / "state"


def test_poc_service_ports_use_state_scoped_high_range(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    sup = WorkerBeeSupervisor(
        project="demo",
        state_dir=tmp_path / "projects" / "demo",
        runtime="podman",
        cwd=tmp_path,
    )

    ports = sup._allocate_poc_service_ports("podman")  # noqa: SLF001

    assert set(ports) == {"store", "api", "frontend"}
    assert len(set(ports.values())) == 3
    assert all(22000 <= port <= 29999 for port in ports.values())
    assert set(ports.values()).isdisjoint({19080, 19081, 19082})


def test_parse_published_host_ports() -> None:
    from workerbee.supervisor import _parse_published_host_ports  # noqa: PLC0415

    assert _parse_published_host_ports(
        "0.0.0.0:22080->8080/tcp, [::]:22081->8080/tcp\n"
        "127.0.0.1:22082->8080/tcp"
    ) == {22080, 22081, 22082}


def test_cli_option_normalization_handles_dash_prefixed_tokens() -> None:
    from workerbee.supervisor import (  # noqa: PLC0415
        _mask_sensitive_args,
        _normalize_cli_option_args,
    )

    args = _normalize_cli_option_args(["--server", "http://local", "--token", "-dash", "status"])

    assert args == ["--server", "http://local", "--token=-dash", "status"]
    assert _mask_sensitive_args(["python", "-m", "ae.cli", *args]) == [
        "python",
        "-m",
        "ae.cli",
        "--server",
        "http://local",
        "--token=***",
        "status",
    ]


def test_k1s_runtime_apply_env_prepends_pythonpath() -> None:
    runtime = K1sRuntime(
        source="sibling",
        python_executable="/venv/bin/python",
        k1s_root=Path("/repo/k1s"),
        pythonpath="/repo/k1s/src",
        ae_origin="/repo/k1s/src/ae/__init__.py",
    )
    assert runtime.apply_env({"PYTHONPATH": "/existing"})["PYTHONPATH"] == (
        "/repo/k1s/src:/existing"
    )


def test_containerd_supervisor_env_is_state_scoped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    state_dir = tmp_path / "projects" / "demo"
    sup = WorkerBeeSupervisor(project="demo", state_dir=state_dir, runtime="containerd")
    info = StackInfo(
        project="demo",
        state_dir=str(state_dir),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin="/site-packages/ae/__init__.py",
        runtime="containerd",
        network=containerd_network_name(tmp_path, "demo"),
        controller_port=19108,
        apishim_port=18445,
        dashboard_url="http://127.0.0.1:19108/dashboard",
        controller_url="http://127.0.0.1:19108",
        apishim_url="https://127.0.0.1:18445",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["shim", "token"]),
    )

    env = sup._base_env(info)  # noqa: SLF001 - verifies runtime isolation contract

    assert env["AE_CONTAINERD_NAMESPACE"].startswith("workerbee-")
    assert env["AE_CONTAINERD_NAMESPACE"].endswith("-demo")
    assert env["AE_CONTAINERD_NETWORK"] == env["AE_NETWORK_NAME"]
    assert env["AE_CONTAINERD_NETWORK_SUBNET"] == containerd_network_subnet(tmp_path, "demo")
    assert env["AE_NERDCTL_BIN"] == "nerdctl"
    assert env["AE_CONTAINERD_CNI_CONF_DIR"] == str(state_dir / "containerd-cni-net.d")
    assert env["NETCONFPATH"] == env["AE_CONTAINERD_CNI_CONF_DIR"]
    assert env["AE_APISHIM_PUBLIC_BASE"] == "https://127.0.0.1:18445"
    assert env["AE_DASHBOARD_BOOTSTRAP_TOKEN"] == "admin-token"  # noqa: S105


def test_supervisor_stack_ingress_publishes_dashboard_and_apishim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    state_dir = tmp_path / "projects" / "demo"
    ingress = ProjectIngressConfig(
        project="demo",
        domain="demo.workerbee.localhost",
        https_port=19443,
        sites_dir=state_dir / "caddy",
        caddy_container="workerbee-caddy-test",
        caddy_file="/etc/caddy/Caddyfile",
        host_alias="127.0.0.1",
        ca_bundle=tmp_path / "ca.crt",
        global_dashboard_url="https://dashboard.workerbee.localhost:19443/",
        dashboard_port=18090,
    )
    sup = WorkerBeeSupervisor(
        project="demo",
        state_dir=state_dir,
        runtime="containerd",
        ingress=ingress,
    )
    monkeypatch.setattr(sup, "_reload_ingress", lambda: None)
    info = StackInfo(
        project="demo",
        state_dir=str(state_dir),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin="/site-packages/ae/__init__.py",
        runtime="containerd",
        network=containerd_network_name(tmp_path, "demo"),
        controller_port=19108,
        apishim_port=18445,
        dashboard_url="http://127.0.0.1:19108/dashboard",
        controller_url="http://127.0.0.1:19108",
        apishim_url="https://127.0.0.1:18445",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["shim", "token"]),
        ingress=ingress.public_dict(),
    )

    refreshed = sup._refresh_stack_ingress_info(info)  # noqa: SLF001
    env = sup._base_env(refreshed)  # noqa: SLF001

    route = (state_dir / "caddy" / "k1s-stack.caddy").read_text(encoding="utf-8")
    assert "https://k1s.demo.workerbee.localhost" in route
    assert "https://k1s-api.demo.workerbee.localhost" in route
    assert "handle /static/dash-assets/*" in route
    assert "reverse_proxy 127.0.0.1:18090" in route
    assert "reverse_proxy 127.0.0.1:19108" in route
    assert "reverse_proxy https://127.0.0.1:18445" in route
    assert "tls_insecure_skip_verify" in route
    assert refreshed.dashboard_url == "https://k1s.demo.workerbee.localhost:19443/dashboard"
    assert refreshed.ingress_urls["api_healthz"] == (
        "https://k1s-api.demo.workerbee.localhost:19443/healthz"
    )
    assert env["AE_APISHIM_PUBLIC_BASE"] == (
        "https://k1s-api.demo.workerbee.localhost:19443"
    )
    assert sup._stack_requires_ingress_restart(refreshed) is False  # noqa: SLF001


def test_supervisor_stack_ingress_detects_legacy_loopback_dashboard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    state_dir = tmp_path / "projects" / "demo"
    ingress = ProjectIngressConfig(
        project="demo",
        domain="demo.workerbee.localhost",
        https_port=19443,
        sites_dir=state_dir / "caddy",
        caddy_container="workerbee-caddy-test",
        caddy_file="/etc/caddy/Caddyfile",
        host_alias="127.0.0.1",
        ca_bundle=tmp_path / "ca.crt",
        global_dashboard_url="https://dashboard.workerbee.localhost:19443/",
    )
    sup = WorkerBeeSupervisor(
        project="demo",
        state_dir=state_dir,
        runtime="containerd",
        ingress=ingress,
    )
    info = StackInfo(
        project="demo",
        state_dir=str(state_dir),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin="/site-packages/ae/__init__.py",
        runtime="containerd",
        network=containerd_network_name(tmp_path, "demo"),
        controller_port=19108,
        apishim_port=18445,
        dashboard_url="http://127.0.0.1:19108/dashboard",
        controller_url="http://127.0.0.1:19108",
        apishim_url="https://127.0.0.1:18445",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["shim", "token"]),
    )

    assert sup._stack_requires_ingress_restart(info) is True  # noqa: SLF001


def test_containerd_supervisor_env_passes_helper_nerdctl_to_k1s(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKERBEE_NERDCTL_BIN", str(tmp_path / "workerbee-nerdctl"))
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    state_dir = tmp_path / "projects" / "demo"
    sup = WorkerBeeSupervisor(project="demo", state_dir=state_dir, runtime="containerd")
    info = StackInfo(
        project="demo",
        state_dir=str(state_dir),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin="/site-packages/ae/__init__.py",
        runtime="containerd",
        network=containerd_network_name(tmp_path, "demo"),
        controller_port=19108,
        apishim_port=18445,
        dashboard_url="http://127.0.0.1:19108/dashboard",
        controller_url="http://127.0.0.1:19108",
        apishim_url="https://127.0.0.1:18445",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["shim", "token"]),
    )

    env = sup._base_env(info)  # noqa: SLF001 - verifies runtime isolation contract

    assert env["AE_NERDCTL_BIN"] == str(tmp_path / "workerbee-nerdctl")
    assert str(tmp_path / "workerbee-nerdctl") in Path(env["AE_CONTAINER_CLI"]).read_text(
        encoding="utf-8"
    )


def test_containerd_stop_purge_uses_helper_for_project_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    monkeypatch.setattr("workerbee.supervisor._pid_alive", lambda _pid: False)
    subprocess_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> SimpleNamespace:
        subprocess_calls.append(cmd)
        stdout = "localhost/app:test\n<none>:<none>\n" if "images" in cmd else ""
        return SimpleNamespace(stdout=stdout, returncode=0)

    monkeypatch.setattr("workerbee.supervisor.subprocess.run", fake_run)
    calls: list[Path] = []
    monkeypatch.setattr(
        "workerbee.supervisor.remove_containerd_helper_tree",
        lambda _root, target: calls.append(target) or {"ok": True, "removed": True},
    )
    state_dir = tmp_path / "projects" / "demo"
    sup = WorkerBeeSupervisor(project="demo", state_dir=state_dir, runtime="containerd")
    state_dir.mkdir(parents=True)
    info = StackInfo(
        project="demo",
        state_dir=str(state_dir),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin="/site-packages/ae/__init__.py",
        runtime="containerd",
        network=containerd_network_name(tmp_path, "demo"),
        controller_port=19108,
        apishim_port=18445,
        dashboard_url="http://127.0.0.1:19108/dashboard",
        controller_url="http://127.0.0.1:19108",
        apishim_url="https://127.0.0.1:18445",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["shim", "token"]),
    )
    sup._write_stack(info)  # noqa: SLF001
    assert S_IMODE(sup.stack_file.stat().st_mode) == 0o600

    result = sup.stop(purge=True)

    assert result["ok"] is True
    assert calls == [state_dir]
    assert any("rmi" in cmd and "localhost/app:test" in cmd for cmd in subprocess_calls)
    assert any(
        cmd[-3:] == ["namespace", "remove", containerd_namespace(tmp_path, "demo")]
        for cmd in subprocess_calls
    )


def test_containerd_stop_purge_uses_helper_without_stack_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    subprocess_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> SimpleNamespace:
        subprocess_calls.append(cmd)
        stdout = "orphan-container\n" if "ps" in cmd else ""
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr("workerbee.supervisor.subprocess.run", fake_run)
    calls: list[Path] = []
    monkeypatch.setattr(
        "workerbee.supervisor.remove_containerd_helper_tree",
        lambda _root, target: calls.append(target) or {"ok": True, "removed": True},
    )
    state_dir = tmp_path / "projects" / "demo"
    state_dir.mkdir(parents=True)
    sup = WorkerBeeSupervisor(project="demo", state_dir=state_dir, runtime="containerd")

    result = sup.stop(purge=True)

    assert result["ok"] is True
    assert calls == [state_dir]
    assert any("rm" in cmd and "orphan-container" in cmd for cmd in subprocess_calls)
    assert any(
        cmd[-3:] == ["namespace", "remove", containerd_namespace(tmp_path, "demo")]
        for cmd in subprocess_calls
    )
