from pathlib import Path

from workerbee.daemon import WorkerBeeDaemon
from workerbee.ingress import GlobalIngress
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
