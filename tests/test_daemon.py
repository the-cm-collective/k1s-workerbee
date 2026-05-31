import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from workerbee.contract import WorkerBeeError
from workerbee.daemon import (
    DASHBOARD_BACKGROUND_PATH,
    DASHBOARD_LOGO_PATH,
    WorkerBeeDaemon,
    _caddy_exposed_routes,
    _dashboard_static_asset,
    _dashboard_summary,
    _handle_dashboard_action,
    _profile_control_plane_checks,
    _render_dashboard,
    _send_bytes,
    _send_ca_download,
    _send_html,
    _send_json,
    _send_not_found,
    _websocket_probe_once,
)
from workerbee.http import request
from workerbee.ingress import (
    GlobalIngress,
    GlobalIngressInfo,
    IngressSettings,
    global_ingress_status,
)
from workerbee.k1s_runtime import K1sRuntime

LAN_BIND_HOST = "0.0.0.0"  # noqa: S104 - explicit LAN bind fixture


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


def test_capabilities_surface_probe_and_image_build_hints(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "workerbee.daemon.runtime_diagnostics",
        lambda *_args, **_kwargs: {"ok": True, "selected": "podman"},
    )
    monkeypatch.setattr(
        "workerbee.daemon.containerd_privilege_status",
        lambda **_kwargs: {"enabled": False},
    )
    monkeypatch.setattr(
        "workerbee.k1s_runtime.resolve_k1s_runtime",
        lambda **_kwargs: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )

    payload = WorkerBeeDaemon(state_root=tmp_path, runtime="podman").capabilities()

    probe = payload["tool_hints"]["workerbee_v1_ingress_probe"]
    assert "PUT" in probe["methods"]
    assert probe["headers"] is True
    assert probe["body_fields"] == ["json_body", "body"]
    assert "dockerfile" in payload["tool_hints"]["workerbee_v1_image_build"]


def test_daemon_start_does_not_register_default_project(tmp_path: Path, monkeypatch) -> None:
    daemon = WorkerBeeDaemon(
        state_root=tmp_path,
        runtime="docker",
        default_project="demo",
        cwd=tmp_path / "checkout",
    )
    started_with: list[list[str]] = []

    class FakeIngress:
        runtime = "docker"

        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self, *, projects: list[str] | None = None) -> SimpleNamespace:
            started_with.append(list(projects or []))
            return SimpleNamespace(dashboard_url="https://dashboard.workerbee.localhost:19443/")

    monkeypatch.setattr(daemon, "_start_dashboard_server", lambda: 18090)
    monkeypatch.setattr(daemon, "_start_dns_server", lambda: None)
    monkeypatch.setattr("workerbee.daemon.GlobalIngress", FakeIngress)

    try:
        daemon.start()
    finally:
        if daemon._state_lock is not None:  # noqa: SLF001 - release startup lock in test
            daemon._state_lock.release()  # noqa: SLF001

    assert daemon._read_registry() == {}  # noqa: SLF001
    assert started_with == [[]]


def test_secret_policy_status_is_project_scoped_and_read_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("WORKERBEE_ALLOW_PLAINTEXT_SECRETS", raising=False)
    monkeypatch.delenv("WORKERBEE_SOPS_AGE_KEY_FILE", raising=False)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="podman", default_project="alpha")

    result = daemon.secret_policy_status(project="Beta Project!")

    assert result["ok"] is True
    assert result["project"] == "beta-project"
    assert result["state_root"] == str(tmp_path)
    assert result["project_state"].endswith("/projects/beta-project")
    assert result["secret_policy"]["mode"] == "sops"
    assert result["secret_policy"]["key_ready"] is False
    assert not (Path(result["project_state"]) / "secrets" / "age" / "keys.txt").exists()


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
                    "stack_running": True,
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
    assert 'data-action-scope="selected"' in html
    assert 'data-action-scope="all"' in html
    assert 'data-action-scope="row"' in html
    assert "reconcileActionJobs" in html
    assert "selectedCount" in html
    assert 'data-action="mcp_reboot"' in html
    assert 'class="row-actions"><div class="row-actions-inner">' in html
    assert ".row-actions { display: flex" not in html
    assert f'class="brand-logo" alt="k1s logo" src="{DASHBOARD_LOGO_PATH}"' in html
    assert "font-size: 18px" in html
    assert "font-size: 13px" in html
    assert DASHBOARD_BACKGROUND_PATH in html
    assert "alpha" in html
    assert ">eager<" in html
    assert ">running<" in html
    assert "http://127.0.0.1:19108/dashboard" in html
    assert "&quot;runtime&quot;: &quot;containerd&quot;" in html
    assert 'id="refresh-interval"' in html
    assert "refreshProjects" in html
    assert "refreshInFlight" in html
    assert "setInterval" not in html
    assert 'id="summary-grid"' in html
    assert 'id="jobs-grid"' in html
    assert '<h2>Projects</h2>' in html
    assert '<h2>Response</h2>' in html
    assert '<h2>Ingress & DNS</h2>' in html
    assert html.index("<h2>Projects</h2>") < html.index("<h2>Response</h2>")
    assert html.index("<h2>Response</h2>") < html.index("<h2>Ingress & DNS</h2>")
    assert 'id="copy-action-result"' in html
    assert "Copy JSON" in html
    assert 'id="response-auto-clear"' in html
    assert "responseAutoClearKey" in html
    assert "navigator.clipboard.writeText" in html
    assert "Dashboard CA" in html
    assert "/workerbee-ca.crt" in html
    assert "setResponseText('', {status: 'cleared'});" in html
    assert "}, 10000);" in html
    assert 'id="global-ingress-panel"' in html
    assert "renderGlobalIngressPanel" in html
    assert "/api/action-jobs/" in html
    assert "expandedRouteProjects" in html
    assert "refresh paused: route details open" in html
    assert "window.location.reload()" not in html


def test_projects_do_not_resolve_k1s_runtime_for_stopped_projects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    monkeypatch.setattr(
        daemon,
        "global_dashboard",
        lambda: {"enabled": True, "running": True, "https_port": 19443},
    )
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: (_ for _ in ()).throw(AssertionError("unexpected k1s runtime probe")),
    )
    for index in range(20):
        daemon._register_project(  # noqa: SLF001
            f"project-{index}",
            cwd_hint=str(tmp_path / f"project-{index}"),
        )

    payload = daemon.projects()

    assert len(payload["projects"]) == 20
    assert payload["summary"]["projects_total"] == 20
    assert all(item["running"] is False for item in payload["projects"])


def test_global_dashboard_renders_dns_enabled_state() -> None:
    html = _render_dashboard(
        {
            "projects": [],
            "global_dashboard": {
                "enabled": True,
                "running": True,
                "runtime": "podman",
                "exposure": "lan",
                "base_domain": "workerbee.home.arpa",
                "bind_host": LAN_BIND_HOST,
                "https_port": 19443,
                "dashboard_url": "https://dashboard.workerbee.home.arpa:19443/",
                "ca_download_url": "http://ca.workerbee.home.arpa:19080/workerbee-ca.crt",
                "dashboard_ca_download_url": (
                    "https://dashboard.workerbee.home.arpa:19443/workerbee-ca.crt"
                ),
                "dashboard_ca_sha256_url": (
                    "https://dashboard.workerbee.home.arpa:19443/workerbee-ca.sha256"
                ),
                "ca_sha256": "abc123",
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
                    "running": True,
                    "mode": "forwarding",
                    "bind_host": LAN_BIND_HOST,
                    "port": 53,
                    "answer": "192.168.1.23",
                    "base_domain": "workerbee.home.arpa",
                    "upstreams": ["127.0.0.1:5300"],
                    "ttl": 30,
                },
            },
        }
    )

    assert "Ingress & DNS" in html
    assert '<span class="pill ok">DNS forwarding</span>' in html
    assert "workerbee.home.arpa" in html
    assert "192.168.1.23:53" in html
    assert "127.0.0.1:5300" in html
    assert "https://dashboard.workerbee.home.arpa:19443/workerbee-ca.crt" in html
    assert "https://dashboard.workerbee.home.arpa:19443/workerbee-ca.sha256" in html
    assert "http://ca.workerbee.home.arpa:19080/workerbee-ca.crt" in html
    assert "workerbee ingress ca --output workerbee-ca.crt" in html
    assert "workerbee trust install --target system" in html


def test_global_dashboard_renders_dns_disabled_state() -> None:
    html = _render_dashboard(
        {
            "projects": [],
            "global_dashboard": {
                "enabled": True,
                "running": True,
                "base_domain": "workerbee.localhost",
                "dns": {"enabled": False, "mode": "off"},
            },
        }
    )

    assert '<span class="pill idle">DNS off</span>' in html


def test_dashboard_summary_reports_dns_state() -> None:
    summary = _dashboard_summary(
        [],
        global_dashboard={
            "running": True,
            "health_probe": {"ok": True},
            "dns": {"enabled": True, "running": True, "mode": "forwarding"},
        },
    )

    assert summary["dns_enabled"] is True
    assert summary["dns_running"] is True
    assert summary["dns_mode"] == "forwarding"


def test_dashboard_dynamic_responses_use_no_store_security_headers() -> None:
    handler = _FakeDashboardHandler()

    _send_html(handler, "<html></html>")

    assert handler.status == 200
    assert handler.headers["Cache-Control"] == "no-store"
    assert handler.headers["X-Content-Type-Options"] == "nosniff"
    assert handler.headers["Referrer-Policy"] == "no-referrer"
    assert handler.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in handler.headers["Content-Security-Policy"]

    handler = _FakeDashboardHandler()
    _send_json(handler, {"ok": True})

    assert handler.headers["Cache-Control"] == "no-store"
    assert handler.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in handler.headers

    handler = _FakeDashboardHandler()
    _send_not_found(handler)

    assert handler.status == 404
    assert handler.headers["Cache-Control"] == "no-store"
    assert handler.headers["X-Content-Type-Options"] == "nosniff"


def test_dashboard_static_assets_are_cacheable_but_nosniff() -> None:
    handler = _FakeDashboardHandler()

    _send_bytes(handler, b"asset", "image/png")

    assert handler.headers["Cache-Control"] == "public, max-age=3600"
    assert handler.headers["X-Content-Type-Options"] == "nosniff"


def test_lan_ca_download_serves_cert_and_fingerprint(tmp_path: Path) -> None:
    settings = IngressSettings(
        exposure="lan",
        base_domain="workerbee.home.arpa",
        bind_host=LAN_BIND_HOST,
        ca_http_port=19080,
    )
    daemon = WorkerBeeDaemon(state_root=tmp_path, ingress_settings=settings)
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
        ingress_settings=settings,
    )
    ingress.global_dir.mkdir(parents=True)
    ingress.ca_bundle.write_text("-----BEGIN CERTIFICATE-----\ncert\n", encoding="utf-8")
    daemon.ingress = ingress

    handler = _FakeDashboardHandler()
    _send_ca_download(handler, daemon=daemon, path="/workerbee-ca.crt")

    assert handler.status == 200
    assert handler.headers["Content-Type"] == "application/x-x509-ca-cert"
    assert handler.headers["Content-Disposition"] == 'attachment; filename="workerbee-ca.crt"'
    assert handler.headers["Cache-Control"] == "no-store"
    assert handler.wfile.data.startswith(b"-----BEGIN CERTIFICATE-----")

    handler = _FakeDashboardHandler()
    _send_ca_download(handler, daemon=daemon, path="/workerbee-ca.sha256")

    assert handler.status == 200
    assert len(handler.wfile.data.decode().strip()) == 64


def test_loopback_dashboard_ca_download_serves_cert_and_fingerprint(tmp_path: Path) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path)
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )
    ingress.global_dir.mkdir(parents=True)
    ingress.ca_bundle.write_text("-----BEGIN CERTIFICATE-----\ncert\n", encoding="utf-8")
    daemon.ingress = ingress

    handler = _FakeDashboardHandler()
    _send_ca_download(handler, daemon=daemon, path="/workerbee-ca.crt")

    assert handler.status == 200
    assert handler.headers["Content-Type"] == "application/x-x509-ca-cert"
    assert handler.wfile.data.startswith(b"-----BEGIN CERTIFICATE-----")

    handler = _FakeDashboardHandler()
    _send_ca_download(handler, daemon=daemon, path="/workerbee-ca.sha256")

    assert handler.status == 200
    assert len(handler.wfile.data.decode().strip()) == 64


def test_dashboard_ca_download_missing_bundle_returns_not_found(tmp_path: Path) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path)
    daemon.ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )

    handler = _FakeDashboardHandler()
    _send_ca_download(handler, daemon=daemon, path="/workerbee-ca.crt")

    assert handler.status == 404


def test_profile_control_plane_checks_use_loopback_with_public_host(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    read_value = "-".join(["read", "value"])
    api_value = "-".join(["api", "value"])

    def fake_loopback(url: str, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status=200)

    monkeypatch.setattr("workerbee.daemon.request_https_via_loopback", fake_loopback)
    ca_bundle = str(tmp_path / "workerbee-ca.pem")

    results = _profile_control_plane_checks(
        {
            "ca_bundle": ca_bundle,
            "read_token": read_value,
            "apishim_token": api_value,
            "urls": {
                "dashboard": "https://k1s.alpha.workerbee.localhost:19443/dashboard",
                "docs": "https://k1s.alpha.workerbee.localhost:19443/docs",
                "controller_health": "https://k1s.alpha.workerbee.localhost:19443/health",
                "api_healthz": "https://k1s-api.alpha.workerbee.localhost:19443/healthz",
                "api_openapi_v3": (
                    "https://k1s-api.alpha.workerbee.localhost:19443/openapi/v3"
                ),
            },
        }
    )

    assert all(item["ok"] for item in results)
    assert calls[0] == (
        "https://127.0.0.1:19443/dashboard",
        {
            "server_hostname": "k1s.alpha.workerbee.localhost",
            "host_header": "k1s.alpha.workerbee.localhost:19443",
            "token": None,
            "ca_bundle": ca_bundle,
        },
    )
    assert calls[2][1]["token"] == read_value
    assert calls[3][1]["server_hostname"] == "k1s-api.alpha.workerbee.localhost"
    assert calls[3][1]["token"] == api_value


def test_websocket_probe_connects_loopback_with_original_host(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeRaw:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def sendall(self, data: bytes) -> None:
            calls["request"] = data

    class FakeContext:
        def wrap_socket(self, raw, *, server_hostname: str):  # noqa: ANN001
            calls["server_hostname"] = server_hostname
            calls["raw"] = raw
            return FakeSocket()

    def fake_connect(address: tuple[str, int], *, timeout: float):
        calls["address"] = address
        calls["timeout"] = timeout
        return FakeRaw()

    def fake_context(*, cafile: str) -> FakeContext:
        calls["cafile"] = cafile
        return FakeContext()

    monkeypatch.setattr("workerbee.daemon.socket.create_connection", fake_connect)
    monkeypatch.setattr("workerbee.daemon.ssl.create_default_context", fake_context)
    monkeypatch.setattr(
        "workerbee.daemon._read_until",
        lambda *_args, **_kwargs: b"HTTP/1.1 101 Switching Protocols\r\n\r\n",
    )
    monkeypatch.setattr("workerbee.daemon._send_ws_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "workerbee.daemon._read_ws_text",
        lambda *_args, **_kwargs: "echo:workerbee",
    )

    result = _websocket_probe_once(
        "wss://api.alpha.workerbee.localhost:19443/ws",
        ca_bundle=str(tmp_path / "test-ca.pem"),
        expected="echo:workerbee",
    )

    assert result["ok"] is True
    assert calls["cafile"] == str(tmp_path / "test-ca.pem")
    assert calls["address"] == ("127.0.0.1", 19443)
    assert calls["server_hostname"] == "api.alpha.workerbee.localhost"
    assert b"Host: api.alpha.workerbee.localhost:19443\r\n" in calls["request"]


def test_projects_reports_profile_only_project_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="containerd")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    ).project_config("alpha")
    route = ingress.sites_dir / "k1s-profile.caddy"
    route.parent.mkdir(parents=True, exist_ok=True)
    route.write_text("current route", encoding="utf-8")

    class FakeSupervisor:
        state_dir = tmp_path / "projects" / "alpha"

        def status(self) -> dict[str, object]:
            return {"running": False, "apishim_running": False}

    class FakeProfileRunner:
        project_state = tmp_path / "projects" / "alpha"

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def load(self) -> SimpleNamespace:
            return SimpleNamespace(
                ingress_urls={
                    "dashboard": "https://k1s.alpha.workerbee.localhost:19443/dashboard"
                }
            )

        def status(self, *, refresh_ingress: bool = True) -> dict[str, object]:
            assert refresh_ingress is True
            return {
                "ok": True,
                "running": True,
                "profile": {
                    "profile": "k1s-dev-min-sqlite",
                    "dashboard_url": "http://127.0.0.1:19608/dashboard",
                    "ingress_urls": {
                        "dashboard": "https://k1s.alpha.workerbee.localhost:19443/dashboard"
                    },
                },
                "components": [{"role": "controller", "running": True}],
            }

    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: ingress)
    monkeypatch.setattr(daemon, "_build_supervisor", lambda _name, **_kwargs: FakeSupervisor())
    monkeypatch.setattr("workerbee.daemon.K1sProfileRunner", FakeProfileRunner)

    result = daemon.projects()
    item = result["projects"][0]

    assert item["project"] == "alpha"
    assert item["running"] is True
    assert item["stack_running"] is False
    assert item["profile_running"] is True
    assert item["status_kind"] == "profile"
    assert item["profile_name"] == "k1s-dev-min-sqlite"
    assert item["dashboard_url"] == "https://k1s.alpha.workerbee.localhost:19443/dashboard"
    assert item["profile_status"]["ingress_refresh"]["sync_needed"] is False


def test_projects_do_not_derive_profile_dashboard_without_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="containerd")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    ).project_config("alpha")

    class FakeSupervisor:
        state_dir = tmp_path / "projects" / "alpha"

        def status(self) -> dict[str, object]:
            return {"running": False, "apishim_running": False}

    class FakeProfileRunner:
        project_state = tmp_path / "projects" / "alpha"

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def status(self, *, refresh_ingress: bool = True) -> dict[str, object]:
            assert refresh_ingress is True
            return {
                "ok": True,
                "running": False,
                "project": "alpha",
                "state_dir": str(tmp_path / "projects" / "alpha"),
                "state_root": str(tmp_path),
            }

    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: ingress)
    monkeypatch.setattr(daemon, "_build_supervisor", lambda _name, **_kwargs: FakeSupervisor())
    monkeypatch.setattr("workerbee.daemon.K1sProfileRunner", FakeProfileRunner)

    item = daemon.projects()["projects"][0]

    assert item["running"] is False
    assert item["status_kind"] == "stopped"
    assert item["dashboard_url"] is None
    assert item["profile_dashboard_url"] is None


def test_projects_repairs_missing_profile_ingress_and_syncs_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="containerd")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    ).project_config("alpha")
    sync_calls: list[bool] = []

    class FakeSupervisor:
        state_dir = tmp_path / "projects" / "alpha"

        def status(self) -> dict[str, object]:
            return {"running": False, "apishim_running": False}

    class FakeProfileRunner:
        project_state = tmp_path / "projects" / "alpha"

        def __init__(self, **kwargs: object) -> None:
            self.ingress = kwargs["ingress"]

        def load(self) -> SimpleNamespace:
            return SimpleNamespace(ingress_urls={})

        def status(self, *, refresh_ingress: bool = True) -> dict[str, object]:
            assert refresh_ingress is True
            route = self.ingress.sites_dir / "k1s-profile.caddy"
            route.parent.mkdir(parents=True, exist_ok=True)
            route.write_text("repaired route", encoding="utf-8")
            return {
                "ok": True,
                "running": True,
                "profile": {
                    "profile": "k1s-dev-min-sqlite",
                    "dashboard_url": "http://127.0.0.1:19608/dashboard",
                    "ingress_urls": {
                        "dashboard": "https://k1s.alpha.workerbee.localhost:19443/dashboard"
                    },
                },
                "components": [{"role": "controller", "running": True}],
            }

    def fake_sync() -> dict[str, object]:
        sync_calls.append(True)
        return {"scheduled": False, "synced": True}

    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: ingress)
    monkeypatch.setattr(daemon, "_build_supervisor", lambda _name, **_kwargs: FakeSupervisor())
    monkeypatch.setattr(daemon, "_sync_ingress_projects_result", fake_sync)
    monkeypatch.setattr("workerbee.daemon.K1sProfileRunner", FakeProfileRunner)

    result = daemon.projects()
    item = result["projects"][0]

    assert sync_calls == [True]
    assert result["ingress_sync"] == {"scheduled": False, "synced": True, "needed": True}
    assert item["dashboard_url"] == "https://k1s.alpha.workerbee.localhost:19443/dashboard"
    assert item["profile_status"]["ingress_refresh"]["repaired"] is True
    assert item["profile_status"]["ingress_refresh"]["sync_needed"] is True


def test_projects_hide_profile_dashboard_when_ingress_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="containerd")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001

    class FakeSupervisor:
        state_dir = tmp_path / "projects" / "alpha"

        def status(self) -> dict[str, object]:
            return {"running": False, "apishim_running": False}

    class FakeProfileRunner:
        project_state = tmp_path / "projects" / "alpha"

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def status(self, *, refresh_ingress: bool = True) -> dict[str, object]:
            assert refresh_ingress is False
            return {
                "ok": True,
                "running": True,
                "profile": {
                    "profile": "k1s-dev-min-sqlite",
                    "dashboard_url": "http://127.0.0.1:19608/dashboard",
                    "ingress_urls": {},
                },
                "components": [{"role": "controller", "running": True}],
            }

    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: None)
    monkeypatch.setattr(daemon, "_build_supervisor", lambda _name, **_kwargs: FakeSupervisor())
    monkeypatch.setattr("workerbee.daemon.K1sProfileRunner", FakeProfileRunner)

    item = daemon.projects()["projects"][0]

    assert item["running"] is True
    assert item["dashboard_url"] is None
    assert item["profile_dashboard_url"] is None
    assert item["profile_status"]["ingress_refresh"]["attempted"] is False
    assert item["profile_status"]["ingress_refresh"]["reason"] == "global ingress unavailable"


def test_projects_removes_stale_stopped_profile_ingress_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="containerd")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    ).project_config("alpha")
    route = ingress.sites_dir / "k1s-profile.caddy"
    route.parent.mkdir(parents=True, exist_ok=True)
    route.write_text(
        "# Generated by WorkerBee k1s profile runner.\n"
        "https://k1s.alpha.workerbee.localhost { reverse_proxy 127.0.0.1:19608 }\n",
        encoding="utf-8",
    )
    sync_calls: list[bool] = []

    class FakeSupervisor:
        state_dir = tmp_path / "projects" / "alpha"

        def status(self) -> dict[str, object]:
            return {"running": False, "apishim_running": False}

    class FakeProfileRunner:
        project_state = tmp_path / "projects" / "alpha"

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def status(self, *, refresh_ingress: bool = True) -> dict[str, object]:
            assert refresh_ingress is True
            return {
                "ok": True,
                "running": False,
                "project": "alpha",
                "state_dir": str(tmp_path / "projects" / "alpha"),
                "state_root": str(tmp_path),
            }

    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: ingress)
    monkeypatch.setattr(daemon, "_build_supervisor", lambda _name, **_kwargs: FakeSupervisor())
    monkeypatch.setattr(
        daemon,
        "_sync_ingress_projects_result",
        lambda: sync_calls.append(True) or {"scheduled": False, "synced": True},
    )
    monkeypatch.setattr("workerbee.daemon.K1sProfileRunner", FakeProfileRunner)

    result = daemon.projects()
    item = result["projects"][0]

    assert not route.exists()
    assert sync_calls == [True]
    assert result["ingress_sync"] == {"scheduled": False, "synced": True, "needed": True}
    assert item["ingress_status"] == "idle"
    assert item["exposed_route_count"] == 0
    assert item["profile_status"]["ingress_refresh"]["route_removed"] is True


def test_projects_keeps_profile_route_when_profile_metadata_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="containerd")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    ).project_config("alpha")
    route = ingress.sites_dir / "k1s-profile.caddy"
    route.parent.mkdir(parents=True, exist_ok=True)
    route.write_text(
        "# Generated by WorkerBee k1s profile runner.\n"
        "https://k1s.alpha.workerbee.localhost { reverse_proxy 127.0.0.1:19608 }\n",
        encoding="utf-8",
    )

    class FakeSupervisor:
        state_dir = tmp_path / "projects" / "alpha"

        def status(self) -> dict[str, object]:
            return {"running": False, "apishim_running": False}

    class FakeProfileRunner:
        project_state = tmp_path / "projects" / "alpha"

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def load(self) -> SimpleNamespace:
            return SimpleNamespace(
                ingress_urls={
                    "dashboard": "https://k1s.alpha.workerbee.localhost:19443/dashboard"
                }
            )

        def status(self, *, refresh_ingress: bool = True) -> dict[str, object]:
            assert refresh_ingress is True
            return {
                "ok": True,
                "running": False,
                "project": "alpha",
                "profile": {
                    "profile": "k1s-dev-min-sqlite",
                    "ingress_urls": {
                        "dashboard": "https://k1s.alpha.workerbee.localhost:19443/dashboard"
                    },
                },
            }

    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: ingress)
    monkeypatch.setattr(daemon, "_build_supervisor", lambda _name, **_kwargs: FakeSupervisor())
    monkeypatch.setattr("workerbee.daemon.K1sProfileRunner", FakeProfileRunner)

    item = daemon.projects()["projects"][0]

    assert route.exists()
    assert item["profile_status"]["ingress_refresh"]["route_removed"] is False


def test_projects_removes_stale_stopped_stack_ingress_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="containerd")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    ).project_config("alpha")
    route = ingress.sites_dir / "k1s-stack.caddy"
    route.parent.mkdir(parents=True, exist_ok=True)
    route.write_text(
        "# Generated by WorkerBee stack supervisor.\n"
        "https://k1s.alpha.workerbee.localhost { reverse_proxy 127.0.0.1:19108 }\n",
        encoding="utf-8",
    )
    sync_calls: list[bool] = []

    class FakeSupervisor:
        state_dir = tmp_path / "projects" / "alpha"

        def status(self) -> dict[str, object]:
            return {"running": False, "apishim_running": False}

    class FakeProfileRunner:
        project_state = tmp_path / "projects" / "alpha"

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def status(self, *, refresh_ingress: bool = True) -> dict[str, object]:
            assert refresh_ingress is True
            return {
                "ok": True,
                "running": False,
                "project": "alpha",
                "state_dir": str(tmp_path / "projects" / "alpha"),
                "state_root": str(tmp_path),
            }

    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: ingress)
    monkeypatch.setattr(daemon, "_build_supervisor", lambda _name, **_kwargs: FakeSupervisor())
    monkeypatch.setattr(
        daemon,
        "_sync_ingress_projects_result",
        lambda: sync_calls.append(True) or {"scheduled": False, "synced": True},
    )
    monkeypatch.setattr("workerbee.daemon.K1sProfileRunner", FakeProfileRunner)

    result = daemon.projects()
    item = result["projects"][0]

    assert not route.exists()
    assert sync_calls == [True]
    assert result["ingress_sync"] == {"scheduled": False, "synced": True, "needed": True}
    assert item["ingress_status"] == "idle"
    assert item["exposed_route_count"] == 0


def test_projects_keeps_stack_route_when_stack_metadata_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="containerd")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    ).project_config("alpha")
    route = ingress.sites_dir / "k1s-stack.caddy"
    route.parent.mkdir(parents=True, exist_ok=True)
    route.write_text(
        "# Generated by WorkerBee stack supervisor.\n"
        "https://k1s.alpha.workerbee.localhost { reverse_proxy 127.0.0.1:19108 }\n",
        encoding="utf-8",
    )

    class FakeSupervisor:
        state_dir = tmp_path / "projects" / "alpha"

        def status(self) -> dict[str, object]:
            return {
                "running": False,
                "apishim_running": False,
                "stack": {"dashboard_url": "https://k1s.alpha.workerbee.localhost:19443/dashboard"},
            }

    class FakeProfileRunner:
        project_state = tmp_path / "projects" / "alpha"

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def status(self, *, refresh_ingress: bool = True) -> dict[str, object]:
            assert refresh_ingress is True
            return {
                "ok": True,
                "running": False,
                "project": "alpha",
                "state_dir": str(tmp_path / "projects" / "alpha"),
                "state_root": str(tmp_path),
            }

    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: ingress)
    monkeypatch.setattr(daemon, "_build_supervisor", lambda _name, **_kwargs: FakeSupervisor())
    monkeypatch.setattr("workerbee.daemon.K1sProfileRunner", FakeProfileRunner)

    item = daemon.projects()["projects"][0]

    assert route.exists()
    assert item["stack_dashboard_url"] == "https://k1s.alpha.workerbee.localhost:19443/dashboard"


def test_projects_reports_exposed_caddy_routes(tmp_path: Path, monkeypatch) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    caddy_dir = tmp_path / "projects" / "alpha" / "caddy"
    caddy_dir.mkdir(parents=True)
    (caddy_dir / "api.caddy").write_text(
        """https://api.alpha.workerbee.localhost {
    tls internal
    reverse_proxy 10.1.2.3:8000
}
""",
        encoding="utf-8",
    )

    class FakeSupervisor:
        state_dir = tmp_path / "projects" / "alpha"

        def status(self) -> dict[str, object]:
            return {"running": False}

    class FakeProfileRunner:
        project_state = tmp_path / "projects" / "alpha"

        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def status(self, *, refresh_ingress: bool = True) -> dict[str, object]:
            _ = refresh_ingress
            return {
                "ok": True,
                "running": False,
                "project": "alpha",
                "state_dir": str(self.project_state),
            }

    monkeypatch.setattr(daemon, "_project_ingress", lambda _name: None)
    monkeypatch.setattr(daemon, "_build_supervisor", lambda _name, **_kwargs: FakeSupervisor())
    monkeypatch.setattr("workerbee.daemon.K1sProfileRunner", FakeProfileRunner)

    item = daemon.projects()["projects"][0]

    assert item["ingress_status"] == "ready"
    assert item["exposed_route_count"] == 1
    assert item["exposed_hosts"] == ["api.alpha.workerbee.localhost"]
    assert item["exposed_routes"][0]["type"] == "api"
    assert item["exposed_routes"][0]["upstreams"] == ["10.1.2.3:8000"]


def test_caddy_exposed_routes_parses_profile_routes_and_static_assets(tmp_path: Path) -> None:
    site = tmp_path / "k1s-profile.caddy"
    site.write_text(
        """# Generated by WorkerBee k1s profile runner.
https://k1s.demo.workerbee.localhost, https://k1s-dash.demo.workerbee.localhost {
    header -Strict-Transport-Security
    tls internal
    handle /static/dash-assets/* {
        reverse_proxy 127.0.0.1:18090
    }
    handle {
        reverse_proxy 127.0.0.1:19108
    }
}

https://k1s-api.demo.workerbee.localhost {
    header -Strict-Transport-Security
    tls internal
    reverse_proxy 127.0.0.1:18445
}
""",
        encoding="utf-8",
    )

    routes = _caddy_exposed_routes(site, https_port=19443)

    assert [route["type"] for route in routes] == [
        "static-assets",
        "k1s-dashboard",
        "k1s-api",
    ]
    assert routes[0]["path_matchers"] == ["/static/dash-assets/*"]
    assert routes[0]["public_urls"] == [
        "https://k1s.demo.workerbee.localhost:19443/static/dash-assets/*",
        "https://k1s-dash.demo.workerbee.localhost:19443/static/dash-assets/*",
    ]
    assert routes[1]["upstreams"] == ["127.0.0.1:19108"]
    assert routes[2]["hosts"] == ["k1s-api.demo.workerbee.localhost"]


def test_caddy_exposed_routes_parses_workload_handle_path(tmp_path: Path) -> None:
    site = tmp_path / "rawform.caddy"
    site.write_text(
        """https://app.rawform.workerbee.localhost {
    tls internal
    handle_path /api/* {
        reverse_proxy 10.208.202.22:8000 {
        }
    }
    reverse_proxy 10.208.202.23:3000
}
""",
        encoding="utf-8",
    )

    routes = _caddy_exposed_routes(site, https_port=19443)

    assert routes[0]["type"] == "app"
    assert routes[0]["path_matchers"] == ["/api/*"]
    assert routes[0]["upstreams"] == ["10.208.202.22:8000"]
    assert routes[1]["public_urls"] == ["https://app.rawform.workerbee.localhost:19443/"]


def test_ingress_probe_tls_failure_recovers_after_route_reload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="podman", default_project="demo")
    ca = tmp_path / "global" / "caddy-local-root.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text("cert", encoding="utf-8")
    (tmp_path / "projects" / "demo" / "caddy").mkdir(parents=True)
    calls: list[str] = []

    monkeypatch.setattr(
        daemon,
        "global_dashboard",
        lambda: {
            "enabled": True,
            "running": True,
            "https_port": 19443,
            "ca_bundle": str(ca),
            "caddy_container": "workerbee-caddy-test",
        },
    )
    monkeypatch.setattr(
        daemon,
        "_sync_ingress_projects_result",
        lambda: calls.append("reload") or {"scheduled": False, "synced": True},
    )

    def fake_probe(**kwargs):
        calls.append("probe")
        if calls.count("probe") == 1:
            raise WorkerBeeError(
                code="PROBE_FAILED",
                message="certificate verify failed",
                details={"primary_error": "certificate verify failed"},
                retryable=True,
            )
        return {"ok": True, "url": kwargs["url"], "probe_method": "direct", "status": 200}

    monkeypatch.setattr("workerbee.daemon.probe_workerbee_url", fake_probe)

    result = daemon.ingress_probe(project="demo", host="app.demo.workerbee.localhost")

    assert result["ok"] is True
    assert calls == ["probe", "reload", "probe"]
    assert result["probe_recovery"]["reload"] == {"scheduled": False, "synced": True}
    assert result["probe_recovery"]["caddy_recovery"] is None


def test_ingress_probe_tls_failure_recovers_after_caddy_tls_state_reset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="podman", default_project="demo")
    ca = tmp_path / "global" / "caddy-local-root.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text("cert", encoding="utf-8")
    calls: list[str] = []

    class FakeIngress:
        def recover_caddy_tls_state(self, initial_probe: dict[str, object]) -> dict[str, object]:
            calls.append("caddy-recover")
            assert "reload_error" in initial_probe
            return {"ok": True, "container": "workerbee-caddy-test"}

    daemon.ingress = FakeIngress()  # type: ignore[assignment]
    monkeypatch.setattr(
        daemon,
        "global_dashboard",
        lambda: {
            "enabled": True,
            "running": True,
            "https_port": 19443,
            "ca_bundle": str(ca),
            "caddy_container": "workerbee-caddy-test",
        },
    )
    monkeypatch.setattr(
        daemon,
        "_sync_ingress_projects_result",
        lambda: calls.append("reload") or {"scheduled": False, "synced": True},
    )

    def fake_probe(**kwargs):
        calls.append("probe")
        if calls.count("probe") < 3:
            raise WorkerBeeError(
                code="PROBE_FAILED",
                message="tls handshake failed",
                details={"loopback_error": "tls handshake failed"},
                retryable=True,
            )
        return {"ok": True, "url": kwargs["url"], "probe_method": "direct", "status": 200}

    monkeypatch.setattr("workerbee.daemon.probe_workerbee_url", fake_probe)

    result = daemon.ingress_probe(project="demo", host="app.demo.workerbee.localhost")

    assert result["ok"] is True
    assert calls == ["probe", "reload", "probe", "caddy-recover", "reload", "probe"]
    assert result["probe_recovery"]["caddy_recovery"]["ok"] is True


def test_ingress_probe_non_tls_failure_does_not_recover(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="podman", default_project="demo")
    ca = tmp_path / "global" / "caddy-local-root.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text("cert", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        daemon,
        "global_dashboard",
        lambda: {"enabled": True, "running": True, "https_port": 19443, "ca_bundle": str(ca)},
    )
    monkeypatch.setattr(
        daemon,
        "_sync_ingress_projects_result",
        lambda: calls.append("reload") or {"scheduled": False, "synced": True},
    )

    def fake_probe(**_kwargs):
        calls.append("probe")
        raise WorkerBeeError(
            code="PROBE_FAILED",
            message="connection refused",
            details={"loopback_error": "connection refused"},
            retryable=True,
        )

    monkeypatch.setattr("workerbee.daemon.probe_workerbee_url", fake_probe)

    with pytest.raises(WorkerBeeError):
        daemon.ingress_probe(project="demo", host="app.demo.workerbee.localhost")

    assert calls == ["probe"]


def test_ingress_probe_status_mismatch_does_not_recover(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="podman", default_project="demo")
    ca = tmp_path / "global" / "caddy-local-root.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text("cert", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        daemon,
        "global_dashboard",
        lambda: {"enabled": True, "running": True, "https_port": 19443, "ca_bundle": str(ca)},
    )
    monkeypatch.setattr(
        daemon,
        "_sync_ingress_projects_result",
        lambda: calls.append("reload") or {"scheduled": False, "synced": True},
    )
    monkeypatch.setattr(
        "workerbee.daemon.probe_workerbee_url",
        lambda **kwargs: calls.append("probe")
        or {"ok": False, "url": kwargs["url"], "probe_method": "direct", "status": 404},
    )

    result = daemon.ingress_probe(
        project="demo",
        host="app.demo.workerbee.localhost",
        expected_status=200,
    )

    assert result["ok"] is False
    assert calls == ["probe"]
    assert "probe_recovery" not in result


def test_ingress_probe_final_tls_failure_includes_route_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="podman", default_project="demo")
    ca = tmp_path / "global" / "caddy-local-root.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text("cert", encoding="utf-8")
    sites = tmp_path / "projects" / "demo" / "caddy"
    sites.mkdir(parents=True)
    (sites / "frontend.caddy").write_text(
        """https://app.demo.workerbee.localhost {
    tls internal
    reverse_proxy host.docker.internal:8080
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        daemon,
        "global_dashboard",
        lambda: {
            "enabled": True,
            "running": True,
            "https_port": 19443,
            "ca_bundle": str(ca),
            "caddy_container": "workerbee-caddy-test",
        },
    )
    monkeypatch.setattr(
        daemon,
        "_sync_ingress_projects_result",
        lambda: {"scheduled": False, "synced": True},
    )

    class FakeIngress:
        def recover_caddy_tls_state(self, _initial_probe: dict[str, object]) -> dict[str, object]:
            return {"ok": True, "container": "workerbee-caddy-test"}

    daemon.ingress = FakeIngress()  # type: ignore[assignment]

    def fake_probe(**_kwargs):
        raise WorkerBeeError(
            code="PROBE_FAILED",
            message="tlsv1 alert internal error",
            details={
                "primary_error": "certificate verify failed",
                "loopback_error": "tlsv1 alert internal error",
            },
            retryable=True,
        )

    monkeypatch.setattr("workerbee.daemon.probe_workerbee_url", fake_probe)

    with pytest.raises(WorkerBeeError) as exc_info:
        daemon.ingress_probe(project="demo", host="app.demo.workerbee.localhost")

    details = exc_info.value.details
    assert details["probe_recovery"]["final_error"]["message"] == "tlsv1 alert internal error"
    diagnostics = details["route_diagnostics"]
    assert diagnostics["sites_dir"] == str(sites)
    assert diagnostics["files"][0]["path"].endswith("frontend.caddy")
    assert diagnostics["routes"][0]["hosts"] == ["app.demo.workerbee.localhost"]


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


def test_delete_all_projects_unregisters_without_restoring_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    daemon._register_project("alpha", cwd_hint="/var/lib/workerbee/alpha")  # noqa: SLF001
    daemon._register_project("default", cwd_hint="/var/lib/workerbee/default")  # noqa: SLF001

    class FakeSupervisor:
        def stop(self, *, purge: bool = False) -> dict[str, object]:
            return {"ok": True, "purged": purge}

    monkeypatch.setattr(
        daemon,
        "_build_supervisor",
        lambda _name, **_kwargs: FakeSupervisor(),
    )
    monkeypatch.setattr(daemon, "_sync_ingress_projects", lambda: None)

    result = daemon.delete_all_projects()
    records = daemon._read_registry()  # noqa: SLF001

    assert result["ok"] is True
    assert sorted(result["unregistered"]) == ["alpha", "default"]
    assert "default_restored" not in result
    assert records == {}


def test_delete_all_projects_empty_does_not_restore_default(tmp_path: Path) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")

    result = daemon.delete_all_projects()

    assert result["ok"] is True
    assert result["unregistered"] == []
    assert "default_restored" not in result
    assert daemon._read_registry() == {}  # noqa: SLF001


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

    assert status == 202
    assert payload["ok"] is True
    assert payload["job"]["status"] == "queued"
    assert starts == []
    assert len(scheduled) == 1

    scheduled[0]()

    assert starts == ["alpha"]
    assert daemon.project_mode("alpha") == "start"
    job = daemon.dashboard_action_job(payload["job_id"])
    assert job is not None
    assert job["status"] == "succeeded"
    assert job["result"]["ingress_sync"] == {"scheduled": True, "synced": False}
    assert len(scheduled) == 2
    assert synced == []

    scheduled[1]()

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

    assert status == 202
    assert payload["ok"] is True
    assert len(scheduled) == 1
    assert synced == []

    scheduled[0]()

    job = daemon.dashboard_action_job(payload["job_id"])
    assert job is not None
    assert job["status"] == "succeeded"
    assert job["result"]["unregistered"] == ["alpha"]
    assert job["result"]["ingress_sync"] == {"scheduled": True, "synced": False}
    assert len(scheduled) == 2

    scheduled[1]()

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

    assert shutdown_status == 202
    assert reboot_status == 202
    assert shutdown["job"]["status"] == "queued"
    assert reboot["job"]["status"] == "queued"
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


def test_global_dashboard_root_serves_shell_without_project_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    monkeypatch.setattr(
        daemon,
        "global_dashboard",
        lambda: {"enabled": True, "running": True, "https_port": 19443},
    )
    monkeypatch.setattr(
        daemon,
        "projects",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected project scan")),
    )
    port = daemon._start_dashboard_server()  # noqa: SLF001
    try:
        result = request(f"http://127.0.0.1:{port}/", timeout=2.0)
    finally:
        assert daemon._dashboard is not None  # noqa: SLF001
        daemon._dashboard.shutdown()  # noqa: SLF001
        daemon._dashboard.server_close()  # noqa: SLF001

    assert result.status == 200
    assert "WorkerBee Projects" in result.text
    assert "/api/projects" in result.text


def test_global_dashboard_status_alias_returns_project_json(tmp_path: Path) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    port = daemon._start_dashboard_server()  # noqa: SLF001
    try:
        result = request(f"http://127.0.0.1:{port}/api/status", timeout=2.0)
    finally:
        assert daemon._dashboard is not None  # noqa: SLF001
        daemon._dashboard.shutdown()  # noqa: SLF001
        daemon._dashboard.server_close()  # noqa: SLF001

    assert result.status == 200
    payload = result.json()
    assert payload["state_root"] == str(tmp_path.resolve())
    assert payload["projects"] == []
    assert "updated_at" in payload


def test_global_dashboard_rejects_unexpected_host(tmp_path: Path) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    port = daemon._start_dashboard_server()  # noqa: SLF001
    try:
        result = request(
            f"http://127.0.0.1:{port}/api/status",
            headers={"Host": "evil.example"},
            timeout=2.0,
        )
    finally:
        assert daemon._dashboard is not None  # noqa: SLF001
        daemon._dashboard.shutdown()  # noqa: SLF001
        daemon._dashboard.server_close()  # noqa: SLF001

    assert result.status == 403


def test_global_dashboard_rejects_ca_download_from_unexpected_host(tmp_path: Path) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )
    ingress.global_dir.mkdir(parents=True)
    ingress.ca_bundle.write_text("-----BEGIN CERTIFICATE-----\ncert\n", encoding="utf-8")
    daemon.ingress = ingress
    port = daemon._start_dashboard_server()  # noqa: SLF001
    try:
        result = request(
            f"http://127.0.0.1:{port}/workerbee-ca.crt",
            headers={"Host": "evil.example"},
            timeout=2.0,
        )
    finally:
        assert daemon._dashboard is not None  # noqa: SLF001
        daemon._dashboard.shutdown()  # noqa: SLF001
        daemon._dashboard.server_close()  # noqa: SLF001

    assert result.status == 404


def test_global_dashboard_action_job_endpoint(tmp_path: Path) -> None:
    daemon = WorkerBeeDaemon(state_root=tmp_path, runtime="docker")
    scheduled: list[object] = []
    daemon.configure_dashboard_lifecycle(scheduler=scheduled.append)
    job = daemon.enqueue_dashboard_action("stop_all_projects", [])
    port = daemon._start_dashboard_server()  # noqa: SLF001
    try:
        result = request(
            f"http://127.0.0.1:{port}/api/action-jobs/{job['job_id']}",
            timeout=2.0,
        )
    finally:
        assert daemon._dashboard is not None  # noqa: SLF001
        daemon._dashboard.shutdown()  # noqa: SLF001
        daemon._dashboard.server_close()  # noqa: SLF001

    assert result.status == 200
    payload = result.json()
    assert payload["ok"] is True
    assert payload["job"]["job_id"] == job["job_id"]


def test_global_dashboard_static_background_asset_is_packaged() -> None:
    asset = _dashboard_static_asset(DASHBOARD_BACKGROUND_PATH)

    assert asset is not None
    body, content_type = asset
    assert content_type == "image/webp"
    assert body.startswith(b"RIFF")
    assert body[8:12] == b"WEBP"


def test_global_dashboard_static_background_aliases_k1s_asset_paths() -> None:
    canonical = _dashboard_static_asset(DASHBOARD_BACKGROUND_PATH)
    png = _dashboard_static_asset("/static/dash-assets/page-background-1920x1080.png")
    large = _dashboard_static_asset("/static/dash-assets/page-background-3840x2160.png")
    graph = _dashboard_static_asset("/static/dash-assets/system-graph-background-1920x1080.png")

    assert canonical is not None
    assert png == canonical
    assert large == canonical
    assert graph == canonical


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


def test_global_ingress_loopback_public_info_includes_dashboard_ca_urls(tmp_path: Path) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    )

    info = ingress.info().public_dict()

    assert info["ca_download_url"] is None
    assert info["dashboard_ca_download_url"] == (
        "https://dashboard.workerbee.localhost:19443/workerbee-ca.crt"
    )
    assert info["dashboard_ca_sha256_url"] == (
        "https://dashboard.workerbee.localhost:19443/workerbee-ca.sha256"
    )


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


def test_global_ingress_lan_writes_ca_bootstrap_route(tmp_path: Path) -> None:
    settings = IngressSettings(
        exposure="lan",
        base_domain="192-168-1-23.sslip.io",
        bind_host=LAN_BIND_HOST,
        ca_http_port=19080,
    )
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
        ingress_settings=settings,
    )
    ingress.global_dir.mkdir(parents=True)

    ingress._write_caddyfile(["alpha"])  # noqa: SLF001
    text = ingress.caddy_file.read_text(encoding="utf-8")
    config = ingress.project_config("alpha")

    assert f"default_bind {LAN_BIND_HOST}" in text
    assert "https://dashboard.192-168-1-23.sslip.io" in text
    assert "http://ca.192-168-1-23.sslip.io:19080" in text
    assert "/workerbee-ca.crt /workerbee-ca.sha256" in text
    assert config.domain == "alpha.192-168-1-23.sslip.io"
    assert config.host("api") == "api.alpha.192-168-1-23.sslip.io"
    assert config.ca_download_url == "http://ca.192-168-1-23.sslip.io:19080/workerbee-ca.crt"
    info = ingress.info().public_dict()
    assert info["dashboard_ca_download_url"] == (
        "https://dashboard.192-168-1-23.sslip.io:19443/workerbee-ca.crt"
    )
    assert info["ca_download_url"] == "http://ca.192-168-1-23.sslip.io:19080/workerbee-ca.crt"


def test_global_ingress_lan_publishes_https_and_ca_ports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = IngressSettings(
        exposure="lan",
        base_domain="workerbee.home.arpa",
        bind_host=LAN_BIND_HOST,
        ca_http_port=19080,
    )
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
        ingress_settings=settings,
    )
    ingress.global_dir.mkdir(parents=True)
    ingress.projects_dir.mkdir(parents=True)
    ingress.caddy_data.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("workerbee.ingress._podman_is_rootless", lambda: False)
    monkeypatch.setattr("workerbee.ingress.subprocess.run", fake_run)

    ingress._ensure_caddy_container()  # noqa: SLF001

    run_cmd = next(cmd for cmd in calls if "run" in cmd)
    assert f"{LAN_BIND_HOST}:19443:443" in run_cmd
    assert f"{LAN_BIND_HOST}:19080:19080" in run_cmd


def test_global_ingress_public_info_includes_dns_status(tmp_path: Path) -> None:
    settings = IngressSettings(
        exposure="lan",
        base_domain="workerbee.home.arpa",
        bind_host=LAN_BIND_HOST,
        ca_http_port=19080,
    )
    dns_status = {
        "enabled": True,
        "running": True,
        "mode": "forwarding",
        "bind_host": "127.0.0.1",
        "port": 1053,
        "answer": "192.168.1.23",
        "base_domain": "workerbee.home.arpa",
        "upstreams": ["127.0.0.1:5300"],
    }
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
        ingress_settings=settings,
        dns_status=dns_status,
    )

    public = ingress.info().public_dict()

    assert public["dns"] == dns_status


def test_global_ingress_rootless_podman_allows_host_loopback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("workerbee.ingress._caddy_container_running", lambda *_args: False)
    monkeypatch.setattr("workerbee.ingress._podman_is_rootless", lambda: True)
    monkeypatch.setattr("workerbee.ingress.subprocess.run", fake_run)

    ingress._ensure_caddy_container()  # noqa: SLF001

    run_cmd = next(cmd for cmd in calls if "run" in cmd)
    assert "--network" in run_cmd
    assert "slirp4netns:allow_host_loopback=true" in run_cmd


def test_global_ingress_readiness_uses_local_backend_and_tcp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="containerd",
        https_port=19443,
        dashboard_port=18090,
    )
    http_calls: list[tuple[str, dict[str, object]]] = []
    tcp_calls: list[tuple[str, int, dict[str, object]]] = []

    def fake_wait_for_http(url: str, **kwargs: object) -> None:
        http_calls.append((url, kwargs))

    def fake_wait_for_tcp(host: str, port: int, **kwargs: object) -> None:
        tcp_calls.append((host, port, kwargs))

    monkeypatch.setattr("workerbee.ingress.wait_for_http", fake_wait_for_http)
    monkeypatch.setattr("workerbee.ingress._wait_for_tcp", fake_wait_for_tcp)

    ingress._wait_ready()  # noqa: SLF001

    assert http_calls == [
        (
            "http://127.0.0.1:18090/healthz",
            {
                "timeout_seconds": 10,
                "interval_seconds": 0.2,
                "verify_tls": False,
                "ok_statuses": {200},
            },
        )
    ]
    assert tcp_calls == [("127.0.0.1", 19443, {"timeout_seconds": 20})]


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


def test_global_ingress_status_uses_https_health_when_runtime_probe_fails(
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
                "dashboard_url": "https://dashboard.workerbee.localhost:19443/",
                "https_port": 19443,
                "dashboard_port": 18090,
                "ca_bundle": str(ca),
            }
        ),
        encoding="utf-8",
    )

    def fail_run(_cmd: list[str], **_kwargs):
        raise BlockingIOError("helper busy")

    def fake_request(url: str, **_kwargs):
        assert url == "https://dashboard.workerbee.localhost:19443/healthz"
        return SimpleNamespace(status=200)

    monkeypatch.setattr("workerbee.ingress.subprocess.run", fail_run)
    monkeypatch.setattr("workerbee.ingress.request", fake_request)

    status = global_ingress_status(tmp_path)

    assert status["running"] is True
    assert status["stale"] is False
    assert status["runtime_running"] is False
    assert status["https_running"] is True
    assert status["probe_error"] == "helper busy"


def test_global_ingress_status_falls_back_to_loopback_health_probe(
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
                "dashboard_url": "https://dashboard.workerbee.localhost:19443/",
                "https_port": 19443,
                "dashboard_port": 18090,
                "ca_bundle": str(ca),
            }
        ),
        encoding="utf-8",
    )
    requests: list[tuple[str, dict[str, object]]] = []
    loopback_requests: list[tuple[str, dict[str, object]]] = []

    def fake_run(_cmd: list[str], **_kwargs):
        return SimpleNamespace(returncode=0, stdout="")

    def fake_request(url: str, **kwargs):
        requests.append((url, kwargs))
        raise OSError("DNS lookup failed")

    def fake_loopback_request(url: str, **kwargs):
        loopback_requests.append((url, kwargs))
        return SimpleNamespace(status=200)

    monkeypatch.setattr("workerbee.ingress.subprocess.run", fake_run)
    monkeypatch.setattr("workerbee.ingress.request", fake_request)
    monkeypatch.setattr(
        "workerbee.ingress.request_https_via_loopback",
        fake_loopback_request,
    )

    status = global_ingress_status(tmp_path)

    assert status["running"] is True
    assert status["https_running"] is True
    assert status["health_probe"]["method"] == "loopback-host-header"
    assert status["health_probe"]["primary_error"] == "DNS lookup failed"
    assert status["health_probe"]["tls_verified"] is True
    assert requests[0][0] == "https://dashboard.workerbee.localhost:19443/healthz"
    assert loopback_requests[0][0] == "https://127.0.0.1:19443/healthz"
    assert loopback_requests[0][1]["server_hostname"] == "dashboard.workerbee.localhost"
    assert loopback_requests[0][1]["host_header"] == "dashboard.workerbee.localhost:19443"
    assert loopback_requests[0][1]["verify_tls"] is True
    assert loopback_requests[0][1]["ca_bundle"] == ca


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
    assert status["ca_commands"]["export"] == "workerbee ingress ca --output workerbee-ca.crt"


def test_global_ingress_status_includes_lan_ca_download_command(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    ca = global_dir / "caddy-local-root.crt"
    ca.write_text("cert", encoding="utf-8")
    (global_dir / "ingress.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "runtime": "podman",
                "base_domain": "workerbee.home.arpa",
                "dashboard_url": "https://dashboard.workerbee.home.arpa:19443/",
                "ca_bundle": str(ca),
                "ca_download_url": "http://ca.workerbee.home.arpa:19080/workerbee-ca.crt",
            }
        ),
        encoding="utf-8",
    )

    status = global_ingress_status(tmp_path)

    assert status["ca_ready"] is True
    assert status["ca_sha256"]
    assert status["dashboard_ca_download_url"] == (
        "https://dashboard.workerbee.home.arpa:19443/workerbee-ca.crt"
    )
    assert status["dashboard_ca_sha256_url"] == (
        "https://dashboard.workerbee.home.arpa:19443/workerbee-ca.sha256"
    )
    assert status["ca_commands"]["download_curl"] == (
        "curl -fsSL http://ca.workerbee.home.arpa:19080/workerbee-ca.crt "
        "-o workerbee-ca.crt"
    )


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


def test_global_ingress_retries_ca_export_until_caddy_ca_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="root.crt: no such file")
        return SimpleNamespace(
            returncode=0,
            stdout="-----BEGIN CERTIFICATE-----\ncert\n",
            stderr="",
        )

    monkeypatch.setattr("workerbee.ingress.subprocess.run", fake_run)
    monkeypatch.setattr("workerbee.ingress.time.sleep", lambda _seconds: None)

    ingress._export_ca_bundle()  # noqa: SLF001

    assert len(calls) == 2
    assert ingress.ca_bundle.read_text(encoding="utf-8").startswith("-----BEGIN CERTIFICATE-----")


def test_global_ingress_start_verifies_exported_ca(tmp_path: Path, monkeypatch) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )
    calls: list[object] = []

    def fake_export() -> None:
        calls.append("export")
        ingress.ca_bundle.write_text("-----BEGIN CERTIFICATE-----\ncert\n", encoding="utf-8")

    def fake_probe(info: dict[str, object]) -> dict[str, object]:
        calls.append(("probe", info["ca_bundle"]))
        return {
            "ok": True,
            "url": "https://dashboard.workerbee.localhost:19443/healthz",
            "tls_verified": True,
        }

    monkeypatch.setattr(ingress, "_ensure_caddy_container", lambda: calls.append("ensure"))
    monkeypatch.setattr(ingress, "_wait_ready", lambda: calls.append("wait"))
    monkeypatch.setattr(ingress, "_export_ca_bundle", fake_export)
    monkeypatch.setattr("workerbee.ingress._global_dashboard_health_probe", fake_probe)

    info = ingress.start(projects=["alpha"])

    assert calls == [
        "ensure",
        "wait",
        "export",
        ("probe", str(ingress.ca_bundle)),
    ]
    assert info.ca_bundle == str(ingress.ca_bundle)
    assert ingress.info_file.is_file()


def test_global_ingress_start_recovers_caddy_ca_mismatch_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )
    stale = ingress.caddy_data / "stale-ca-cache"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    calls: list[str] = []
    probes = iter(
        [
            {"ok": False, "error": "certificate verify failed"},
            {
                "ok": True,
                "url": "https://dashboard.workerbee.localhost:19443/healthz",
                "tls_verified": True,
            },
        ]
    )

    def fake_export() -> None:
        calls.append("export")
        ingress.ca_bundle.write_text("-----BEGIN CERTIFICATE-----\ncert\n", encoding="utf-8")

    monkeypatch.setattr(ingress, "_ensure_caddy_container", lambda: calls.append("ensure"))
    monkeypatch.setattr(ingress, "_wait_ready", lambda: calls.append("wait"))
    monkeypatch.setattr(ingress, "_export_ca_bundle", fake_export)
    monkeypatch.setattr(ingress, "stop", lambda: calls.append("stop") or {"ok": True})
    monkeypatch.setattr(
        "workerbee.ingress._global_dashboard_health_probe",
        lambda _info: next(probes),
    )

    ingress.start()

    assert calls == ["ensure", "wait", "export", "stop", "ensure", "wait", "export"]
    assert not stale.exists()
    assert ingress.caddy_data.is_dir()
    assert ingress.ca_bundle.is_file()


def test_global_ingress_public_tls_recovery_rewrites_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )
    ingress.global_dir.mkdir(parents=True)
    ingress.ca_bundle.write_text("-----BEGIN CERTIFICATE-----\ncert\n", encoding="utf-8")
    monkeypatch.setattr(
        ingress,
        "_recover_caddy_ca_mismatch",
        lambda _initial_probe: {"ok": True, "verification": {"ok": True}},
    )

    result = ingress.recover_caddy_tls_state({"ok": False})

    assert result["ok"] is True
    metadata = json.loads(ingress.info_file.read_text(encoding="utf-8"))
    assert metadata["caddy_container"] == ingress.container
    assert metadata["ca_bundle"] == str(ingress.ca_bundle)


def test_global_ingress_start_reports_ca_recovery_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ingress = GlobalIngress(
        state_root=tmp_path,
        runtime="podman",
        https_port=19443,
        dashboard_port=18090,
    )

    def fake_export() -> None:
        ingress.ca_bundle.write_text("-----BEGIN CERTIFICATE-----\ncert\n", encoding="utf-8")

    monkeypatch.setattr(ingress, "_ensure_caddy_container", lambda: None)
    monkeypatch.setattr(ingress, "_wait_ready", lambda: None)
    monkeypatch.setattr(ingress, "_export_ca_bundle", fake_export)
    monkeypatch.setattr(ingress, "stop", lambda: {"ok": True})
    monkeypatch.setattr(
        "workerbee.ingress._global_dashboard_health_probe",
        lambda _info: {"ok": False, "error": "certificate verify failed"},
    )

    with pytest.raises(RuntimeError) as exc_info:
        ingress.start()

    message = str(exc_info.value)
    assert "certificate trusted by its exported CA" in message
    assert "certificate verify failed" in message
    assert str(ingress.ca_bundle) in message
    assert ingress.container in message
    assert "19443" in message


class _FakeDashboardHandler:
    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.ended = False
        self.wfile = _FakeBody()

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.headers[name] = value

    def end_headers(self) -> None:
        self.ended = True


class _FakeBody:
    def __init__(self) -> None:
        self.data = b""

    def write(self, body: bytes) -> None:
        self.data += body
