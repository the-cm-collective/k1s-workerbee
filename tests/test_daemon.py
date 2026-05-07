import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

from workerbee.daemon import (
    DASHBOARD_BACKGROUND_PATH,
    DASHBOARD_LOGO_PATH,
    WorkerBeeDaemon,
    _dashboard_static_asset,
    _handle_dashboard_action,
    _render_dashboard,
)
from workerbee.http import request
from workerbee.ingress import GlobalIngress, GlobalIngressInfo, global_ingress_status
from workerbee.k1s_runtime import K1sRuntime


def test_daemon_uses_state_root_for_project_supervisors(tmp_path: Path, monkeypatch) -> None:
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
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    sup = daemon.supervisor("My Project!")

    assert sup.project == "my-project"
    assert sup.state_dir == tmp_path / "projects" / "my-project"


def test_global_ingress_project_config_is_localhost_scoped(tmp_path: Path) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )
    config = ingress.project_config("alpha")

    assert config.domain == "alpha.workerbee.localhost"
    assert config.url("app.alpha.workerbee.localhost") == (
        "https://app.alpha.workerbee.localhost:19443/"
    )
    assert config.sites_dir == tmp_path / "projects" / "alpha" / "caddy"


def test_global_dashboard_uses_k1s_visual_style() -> None:
    html = _render_dashboard(
        {
            "projects": [
                {
                    "project": "alpha",
                    "mode": "eager",
                    "running": True,
                    "git_branch": "dev",
                    "dashboard_url": "http://127.0.0.1:19108/dashboard",
                    "ingress": {
                        "global_dashboard_url": "https://app.alpha.workerbee.localhost:19443/"
                    },
                    "state_dir": "/var/lib/workerbee/projects/alpha",
                }
            ],
            "global_dashboard": {"enabled": True, "runtime": "containerd"},
        },
        action_token="test-token",  # noqa: S106
    )

    assert "WorkerBee Projects" in html
    assert "brand-accent" in html
    assert 'class="card"' in html
    assert 'name="workerbee-action-token" content="test-token"' in html
    assert 'class="project-select"' in html
    assert 'data-action="start_projects"' in html
    assert 'data-action="delete_projects"' in html
    assert 'data-action="start_all_projects"' in html
    assert 'data-action="mcp_reboot"' in html
    assert f'class="brand-logo" alt="k1s logo" src="{DASHBOARD_LOGO_PATH}"' in html
    assert "font-size: 18px" in html
    assert "font-size: 13px" in html
    assert DASHBOARD_BACKGROUND_PATH in html
    assert "alpha" in html
    assert "eager / running" in html
    assert "https://app.alpha.workerbee.localhost:19443/" in html
    assert "&quot;runtime&quot;: &quot;containerd&quot;" in html


def test_dashboard_action_rejects_missing_or_wrong_token(tmp_path: Path) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")

    missing_status, missing = _handle_dashboard_action(
        daemon,
        {"action": "stop_all_projects"},
    )
    wrong_status, wrong = _handle_dashboard_action(
        daemon,
        {"action": "stop_all_projects", "token": "wrong"},
    )

    assert missing_status == 403
    assert missing["ok"] is False
    assert wrong_status == 403
    assert wrong["ok"] is False


def test_stop_projects_preserves_registry(tmp_path: Path, monkeypatch) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    stops: list[tuple[str, bool]] = []

    class FakeSupervisor:
        def __init__(self, project: str) -> None:
            self.project = project

        def stop(self, *, purge: bool = False) -> dict[str, object]:
            stops.append((self.project, purge))
            return {"ok": True, "purged": purge}

    def fake_build_supervisor(name: str, *, ingress=None) -> FakeSupervisor:
        _ = ingress
        return FakeSupervisor(name)

    monkeypatch.setattr(daemon, "_build_supervisor", fake_build_supervisor)

    result = daemon.stop_projects(["alpha"], purge=False)

    assert result["ok"] is True
    assert stops == [("alpha", False)]
    assert "alpha" in daemon._read_registry()  # noqa: SLF001


def test_delete_projects_purges_and_unregisters_successes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    daemon._register_project("beta", cwd_hint="/var/lib/workerbee/beta")  # noqa: SLF001
    synced: list[bool] = []

    class FakeSupervisor:
        def __init__(self, project: str) -> None:
            self.project = project
            self.state_dir = daemon.projects_dir / project

        def stop(self, *, purge: bool = False) -> dict[str, object]:
            if purge and self.state_dir.exists():
                shutil.rmtree(self.state_dir)
            if self.project == "beta":
                return {"ok": False, "purged": purge, "error": "failed"}
            return {"ok": True, "purged": purge}

    def fake_build_supervisor(name: str, *, ingress=None) -> FakeSupervisor:
        _ = ingress
        return FakeSupervisor(name)

    monkeypatch.setattr(daemon, "_build_supervisor", fake_build_supervisor)
    monkeypatch.setattr(daemon, "_sync_ingress_projects", lambda: synced.append(True))

    result = daemon.delete_projects(["alpha", "beta"])
    records = daemon._read_registry()  # noqa: SLF001

    assert result["ok"] is False
    assert result["unregistered"] == ["alpha"]
    assert "alpha" not in records
    assert "beta" in records
    assert synced == [True]


def test_dashboard_start_projects_schedules_ingress_sync_after_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    daemon._register_project(  # noqa: SLF001
        "alpha",
        cwd_hint="/var/lib/workerbee/alpha",
        mode="stop",
    )
    daemon.ingress = object()  # type: ignore[assignment]
    scheduled: list[object] = []
    synced: list[bool] = []
    starts: list[str] = []
    daemon.configure_dashboard_lifecycle(scheduler=scheduled.append)

    class FakeInfo:
        dashboard_url = "http://127.0.0.1:19108/dashboard"

        def public_dict(self) -> dict[str, object]:
            return {"dashboard_url": self.dashboard_url}

    class FakeSupervisor:
        def __init__(self, project: str) -> None:
            self.project = project
            self.cwd = tmp_path / "checkout"

        def status(self) -> dict[str, object]:
            return {"running": False}

        def start(self) -> FakeInfo:
            starts.append(self.project)
            return FakeInfo()

    def fake_build_supervisor(name: str, *, ingress=None) -> FakeSupervisor:
        _ = ingress
        return FakeSupervisor(name)

    monkeypatch.setattr(daemon, "_build_supervisor", fake_build_supervisor)
    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: None)
    monkeypatch.setattr(daemon, "_sync_ingress_projects", lambda: synced.append(True))

    status, payload = _handle_dashboard_action(
        daemon,
        {
            "action": "start_projects",
            "projects": ["alpha"],
            "token": daemon.dashboard_action_token,
        },
    )

    assert status == 200
    assert payload["ok"] is True
    assert starts == ["alpha"]
    assert daemon.project_mode("alpha") == "start"
    assert payload["result"]["ingress_sync"] == {"scheduled": True, "synced": False}
    assert len(scheduled) == 1
    assert synced == []

    scheduled[0]()

    assert synced == [True]


def test_dashboard_delete_projects_schedules_ingress_sync_after_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    daemon.ingress = object()  # type: ignore[assignment]
    scheduled: list[object] = []
    synced: list[bool] = []
    daemon.configure_dashboard_lifecycle(scheduler=scheduled.append)

    class FakeSupervisor:
        cwd = tmp_path

        def stop(self, *, purge: bool = False) -> dict[str, object]:
            return {"ok": True, "purged": purge}

    def fake_build_supervisor(name: str, *, ingress=None) -> FakeSupervisor:
        _ = (name, ingress)
        return FakeSupervisor()

    monkeypatch.setattr(daemon, "_build_supervisor", fake_build_supervisor)
    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: None)
    monkeypatch.setattr(daemon, "_sync_ingress_projects", lambda: synced.append(True))

    status, payload = _handle_dashboard_action(
        daemon,
        {
            "action": "delete_projects",
            "projects": ["alpha"],
            "token": daemon.dashboard_action_token,
        },
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["result"]["unregistered"] == ["alpha"]
    assert payload["result"]["ingress_sync"] == {"scheduled": True, "synced": False}
    assert len(scheduled) == 1
    assert synced == []

    scheduled[0]()

    assert synced == [True]


def test_dashboard_actions_schedule_mcp_lifecycle_without_running_callbacks(
    tmp_path: Path,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    scheduled: list[object] = []
    called: list[str] = []
    daemon.configure_dashboard_lifecycle(
        shutdown=lambda: called.append("shutdown"),
        reboot=lambda: called.append("reboot"),
        scheduler=scheduled.append,
    )

    shutdown_status, shutdown = _handle_dashboard_action(
        daemon,
        {"action": "mcp_shutdown", "token": daemon.dashboard_action_token},
    )
    reboot_status, reboot = _handle_dashboard_action(
        daemon,
        {"action": "mcp_reboot", "token": daemon.dashboard_action_token},
    )

    assert shutdown_status == 200
    assert reboot_status == 200
    assert shutdown["result"]["scheduled"] is True
    assert reboot["result"]["scheduled"] is True
    assert len(scheduled) == 2
    assert called == []


def test_global_dashboard_healthz_is_lightweight(tmp_path: Path) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    port = daemon._start_dashboard_server()  # noqa: SLF001
    try:
        result = request(f"http://127.0.0.1:{port}/healthz", timeout=2.0)
    finally:
        assert daemon._dashboard is not None  # noqa: SLF001
        daemon._dashboard.shutdown()  # noqa: SLF001
        daemon._dashboard.server_close()  # noqa: SLF001

    assert result.status == 200
    assert result.json()["ok"] is True
    assert result.json()["state_root"] == str(tmp_path.resolve())


def test_global_dashboard_static_background_asset_is_packaged() -> None:
    asset = _dashboard_static_asset(DASHBOARD_BACKGROUND_PATH)

    assert asset is not None
    body, content_type = asset
    assert content_type == "image/png"
    assert body.startswith(b"\x89PNG\r\n\x1a\n")


def test_global_dashboard_static_logo_asset_is_packaged() -> None:
    asset = _dashboard_static_asset(DASHBOARD_LOGO_PATH)

    assert asset is not None
    body, content_type = asset
    assert content_type == "image/png"
    assert body.startswith(b"\x89PNG\r\n\x1a\n")


def test_global_dashboard_static_asset_rejects_unknown_path() -> None:
    assert _dashboard_static_asset("/static/dash-assets/missing.png") is None


def test_global_ingress_containerd_uses_loopback_host_alias(tmp_path: Path) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    )
    config = ingress.project_config("alpha")

    assert config.host_alias == "127.0.0.1"


def test_global_ingress_writes_explicit_project_imports(tmp_path: Path) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )
    ingress.global_dir.mkdir(parents=True)

    ingress._write_caddyfile(["beta", "alpha", "alpha"])  # noqa: SLF001
    text = ingress.caddy_file.read_text(encoding="utf-8")

    assert "import /etc/caddy/projects/alpha/caddy/*.caddy" in text
    assert "import /etc/caddy/projects/beta/caddy/*.caddy" in text
    assert "import /etc/caddy/projects/*/caddy/*.caddy" not in text


def test_global_ingress_containerd_writes_host_network_https_port(tmp_path: Path) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    )
    ingress.global_dir.mkdir(parents=True)

    ingress._write_caddyfile(["alpha"])  # noqa: SLF001
    text = ingress.caddy_file.read_text(encoding="utf-8")

    assert "https_port 19443" in text
    assert "default_bind 127.0.0.1" in text


def test_global_ingress_uses_exported_ca_bundle_path(tmp_path: Path) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    )

    assert ingress.ca_bundle == tmp_path / "global" / "caddy-local-root.crt"
    assert "caddy-data" not in str(ingress.ca_bundle)


def test_global_ingress_public_dict_treats_unreadable_ca_as_not_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class UnreadablePath:
        def is_file(self) -> bool:
            raise PermissionError("denied")

    monkeypatch.setattr("workerbee.ingress.Path", lambda _raw: UnreadablePath())
    info = GlobalIngressInfo(
        enabled=True,
        state_root=str(tmp_path),
        https_port=19443,
        dashboard_port=18090,
        dashboard_url="https://dashboard.workerbee.localhost:19443/",
        caddy_container="workerbee-caddy-test",
        caddy_data=str(tmp_path / "global" / "caddy-data"),
        ca_bundle=str(tmp_path / "global" / "caddy-local-root.crt"),
        localhost_dns_ok=True,
        runtime="containerd",
    )

    assert info.public_dict()["ca_ready"] is False


def test_global_ingress_status_reports_missing_metadata(tmp_path: Path) -> None:
    assert global_ingress_status(tmp_path) == {
        "enabled": False,
        "running": False,
        "stale": False,
        "state_root": str(tmp_path.resolve()),
    }


def test_global_ingress_status_marks_missing_container_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    missing_ca = tmp_path / "missing-ca.crt"
    (global_dir / "ingress.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "runtime": "podman",
                "caddy_container": "workerbee-caddy-test",
                "dashboard_url": "https://dashboard.workerbee.localhost:19443/",
                "ca_bundle": str(missing_ca),
            }
        ),
        encoding="utf-8",
    )

    def fake_run(_cmd: list[str], **_kwargs):
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("workerbee.ingress.subprocess.run", fake_run)

    status = global_ingress_status(tmp_path)

    assert status["enabled"] is False
    assert status["running"] is False
    assert status["stale"] is True
    assert status["dashboard_url"] == "https://dashboard.workerbee.localhost:19443/"


def test_global_ingress_status_reports_running_container(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    ca = global_dir / "caddy-local-root.crt"
    ca.write_text("cert", encoding="utf-8")
    (global_dir / "ingress.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "runtime": "podman",
                "caddy_container": "workerbee-caddy-test",
                "ca_bundle": str(ca),
            }
        ),
        encoding="utf-8",
    )

    def fake_run(_cmd: list[str], **_kwargs):
        return SimpleNamespace(returncode=0, stdout="abc123\n")

    monkeypatch.setattr("workerbee.ingress.subprocess.run", fake_run)

    status = global_ingress_status(tmp_path)

    assert status["enabled"] is True
    assert status["running"] is True
    assert status["stale"] is False
    assert status["ca_ready"] is True


def test_global_ingress_status_falls_back_to_exact_name_match(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "ingress.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "runtime": "podman",
                "caddy_container": "workerbee-caddy-test",
                "ca_bundle": str(global_dir / "caddy-local-root.crt"),
            }
        ),
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        calls.append(cmd)
        if "--format" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout="workerbee-caddy-test\nworkerbee-caddy-test-extra\n",
            )
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("workerbee.ingress.subprocess.run", fake_run)

    status = global_ingress_status(tmp_path)

    assert status["enabled"] is True
    assert status["running"] is True
    assert len(calls) == 2


def test_global_ingress_status_uses_containerd_helper_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    wrapper = global_dir / "bin" / "workerbee-nerdctl"
    socket = global_dir / "containerd-helper.sock"
    (global_dir / "ingress.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "runtime": "containerd",
                "caddy_container": "workerbee-caddy-test",
                "ca_bundle": str(global_dir / "caddy-local-root.crt"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "workerbee.containerd_helper.containerd_privilege_status",
        lambda **_kwargs: {
            "helper": {
                "responsive": True,
                "socket": str(socket),
                "wrapper": str(wrapper),
            }
        },
    )

    def fake_run(cmd: list[str], **_kwargs):
        assert cmd[0] == str(wrapper)
        assert os.environ["WORKERBEE_CONTAINERD_HELPER_SOCKET"] == str(socket)
        return SimpleNamespace(returncode=0, stdout="abc123\n")

    monkeypatch.setattr("workerbee.ingress.subprocess.run", fake_run)

    status = global_ingress_status(tmp_path)

    assert status["enabled"] is True
    assert status["running"] is True
    assert "WORKERBEE_CONTAINERD_HELPER_SOCKET" not in os.environ


def test_global_ingress_exports_caddy_ca_bundle(tmp_path: Path, monkeypatch) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    )
    ingress.global_dir.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="-----BEGIN CERTIFICATE-----\ncert\n",
            stderr="",
        )

    monkeypatch.setattr("workerbee.ingress.subprocess.run", fake_run)

    ingress._export_ca_bundle()  # noqa: SLF001

    assert ingress.ca_bundle.read_text(encoding="utf-8").startswith("-----BEGIN CERTIFICATE-----")
    assert ingress.ca_bundle.stat().st_mode & 0o777 == 0o644
    assert calls
    assert "cat" in calls[0]
    assert "/data/caddy/pki/authorities/local/root.crt" in calls[0]
