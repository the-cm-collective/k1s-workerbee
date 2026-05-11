import json
from pathlib import Path

from workerbee.ingress import ProjectIngressConfig
from workerbee.remote_k1s_target import (
    RemoteK1sTarget,
    RemoteK1sTargetInfo,
    _helper_bridge_script,
)


def test_remote_k1s_target_cleanup_command_uses_state_root(tmp_path: Path) -> None:
    target = RemoteK1sTarget(
        state_root=tmp_path,
        project="Remote App",
        cwd=tmp_path,
        k1s_root=tmp_path,
    )

    assert target.project == "remote-app"
    assert target.cleanup_command == (
        f"workerbee --runtime containerd --state-root {tmp_path.resolve()} "
        "cleanup --execute --purge-images"
    )


def test_remote_k1s_target_writes_dashboard_ingress_site(tmp_path: Path) -> None:
    ingress = ProjectIngressConfig(
        project="remote-app",
        domain="remote-app.workerbee.localhost",
        https_port=19443,
        sites_dir=tmp_path / "caddy",
        caddy_container="workerbee-caddy-test",
        caddy_file="/etc/caddy/Caddyfile",
        host_alias="127.0.0.1",
        ca_bundle=tmp_path / "ca.crt",
        global_dashboard_url="https://dashboard.workerbee.localhost:19443/",
        dashboard_port=18090,
    )
    target = RemoteK1sTarget(
        state_root=tmp_path,
        project="Remote App",
        cwd=tmp_path,
        k1s_root=tmp_path,
        ingress=ingress,
    )

    urls = target._write_ingress_sites(controller_port=19680, apishim_port=18680)  # noqa: SLF001

    text = (tmp_path / "caddy" / "remote-k1s.caddy").read_text(encoding="utf-8")
    assert "https://k1s-remote.remote-app.workerbee.localhost" in text
    assert "reverse_proxy 127.0.0.1:19680" in text
    assert urls["dashboard"] == "https://k1s-remote.remote-app.workerbee.localhost:19443/dashboard"
    assert urls["api_healthz"] == (
        "https://k1s-remote-api.remote-app.workerbee.localhost:19443/healthz"
    )


def test_remote_k1s_target_mounts_external_helper_socket(
    tmp_path: Path,
    monkeypatch,
) -> None:
    socket_path = tmp_path / "short-helper.sock"
    socket_path.touch()
    monkeypatch.setenv("WORKERBEE_CONTAINERD_HELPER_SOCKET", str(socket_path))
    target = RemoteK1sTarget(
        state_root=tmp_path / "state",
        project="Remote App",
        cwd=tmp_path,
        k1s_root=tmp_path,
    )

    mounts = target._mount_args()  # noqa: SLF001

    assert f"{socket_path}:{socket_path}" in mounts


def test_remote_k1s_target_info_masks_tokens(tmp_path: Path) -> None:
    info = RemoteK1sTargetInfo(
        project="remote-app",
        state_root=str(tmp_path),
        target_dir=str(tmp_path / "projects" / "remote-app" / "remote-k1s"),
        controller_url="http://127.0.0.1:19680",
        apishim_url="http://127.0.0.1:18680",
        dashboard_url="https://k1s-remote.remote-app.workerbee.localhost:19443/dashboard",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["apishim", "token"]),
        cleanup_command=(
            "workerbee --runtime containerd --state-root /tmp/state "
            "cleanup --execute --purge-images"
        ),
    )

    public = info.public_dict()
    masked = "*" * 3

    assert public["admin_token"] == masked
    assert public["read_token"] == masked
    assert public["apishim_token"] == masked


def test_helper_bridge_normalizes_nerdctl_json_lines_for_images() -> None:
    namespace = {"__name__": "workerbee_bridge_test"}
    exec(_helper_bridge_script(), namespace)  # noqa: S102
    normalize = namespace["_normalize_nerdctl_stdout"]

    output = normalize(
        [
            "--address",
            "unix:///run/containerd/containerd.sock",
            "--namespace",
            "demo",
            "images",
            "--format",
            "json",
        ],
        b'{"Repository":"localhost/app","Tag":"dev"}\n{"Repository":"python","Tag":"3.12-slim"}\n',
    )

    assert json.loads(output.decode("utf-8")) == [
        {"Repository": "localhost/app", "Tag": "dev"},
        {"Repository": "python", "Tag": "3.12-slim"},
    ]
