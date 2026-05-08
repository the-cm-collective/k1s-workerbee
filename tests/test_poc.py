import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workerbee import poc
from workerbee.k1s_runtime import K1sRuntime
from workerbee.poc import POC_NAMESPACE, validate_poc_urls, write_stack_files
from workerbee.runtime_support import CONTAINERD_RUNTIME, containerd_network_name
from workerbee.supervisor import StackInfo, WorkerBeeSupervisor


def test_write_stack_files_contains_representative_features(tmp_path: Path) -> None:
    artifacts = write_stack_files(
        state_dir=tmp_path,
        project="demo",
        image_tags={
            "store": "store:test",
            "api": "api:test",
            "frontend": "frontend:test",
        },
        service_ports={"store": 19080, "api": 19081, "frontend": 19082},
    )

    text = "\n".join(path.read_text(encoding="utf-8") for path in artifacts.manifests)
    assert f"namespace: {POC_NAMESPACE}" in text
    assert "configRefs:" in text
    assert "secretRefs:" in text
    assert "host.containers.internal" in text
    assert "AE_CONFIG_ROOT" in text
    assert "file: mode.txt" in text
    assert "file: token" in text
    assert "emptyDirs:" in text
    assert "storage:" in text
    assert "readiness:" in text
    assert "ingress:" in text
    assert artifacts.urls["frontend"] == "http://127.0.0.1:19082"


def test_write_stack_files_uses_runtime_peer_urls_for_podman(tmp_path: Path) -> None:
    artifacts = write_stack_files(
        state_dir=tmp_path,
        project="demo",
        image_tags={
            "store": "store:test",
            "api": "api:test",
            "frontend": "frontend:test",
        },
        service_ports={"store": 22080, "api": 22081, "frontend": 22082},
        runtime="podman",
    )

    text = "\n".join(path.read_text(encoding="utf-8") for path in artifacts.manifests)

    assert "http://ae-workerbee-poc--store-rev1-0:8080" in text
    assert "http://ae-workerbee-poc--store:8080" in text
    assert "http://127.0.0.1:22080" not in text


def test_write_stack_files_uses_runtime_peer_urls_for_docker(tmp_path: Path) -> None:
    artifacts = write_stack_files(
        state_dir=tmp_path,
        project="demo",
        image_tags={
            "store": "store:test",
            "api": "api:test",
            "frontend": "frontend:test",
        },
        service_ports={"store": 22080, "api": 22081, "frontend": 22082},
        runtime="docker",
    )

    text = "\n".join(path.read_text(encoding="utf-8") for path in artifacts.manifests)

    assert "http://ae-workerbee-poc--store-rev1-0:8080" in text
    assert "http://app-workerbee-poc--store:8080" in text
    assert "http://127.0.0.1:22080" not in text


def test_write_stack_files_can_scope_ingress_hosts(tmp_path: Path) -> None:
    artifacts = write_stack_files(
        state_dir=tmp_path,
        project="alpha",
        image_tags={
            "store": "store:test",
            "api": "api:test",
            "frontend": "frontend:test",
        },
        service_ports={"store": 19080, "api": 19081, "frontend": 19082},
        ingress_domain="alpha.workerbee.localhost",
    )

    text = "\n".join(path.read_text(encoding="utf-8") for path in artifacts.manifests)
    assert "host: api.alpha.workerbee.localhost" in text
    assert "host: app.alpha.workerbee.localhost" in text


def test_containerd_poc_images_use_localhost_registry() -> None:
    assert poc._poc_image_tag(runtime="containerd", name="api", project="demo") == (  # noqa: SLF001
        "localhost/workerbee-poc-api:demo"
    )
    assert poc._poc_image_tag(runtime="podman", name="api", project="demo") == (  # noqa: SLF001
        "workerbee-poc-api:demo"
    )


def test_containerd_stack_files_include_nerdctl_peer_aliases(tmp_path: Path) -> None:
    artifacts = write_stack_files(
        state_dir=tmp_path,
        project="demo",
        image_tags={
            "store": "localhost/store:test",
            "api": "localhost/api:test",
            "frontend": "localhost/frontend:test",
        },
        service_ports={"store": 19080, "api": 19081, "frontend": 19082},
        runtime="containerd",
    )

    text = "\n".join(path.read_text(encoding="utf-8") for path in artifacts.manifests)

    assert "http://ae-workerbee-poc-store-rev1-0-6a6b035d64:8080" in text
    assert "http://ae-workerbee-poc-api-rev1-0-32840b931b:8080" in text


def test_validate_poc_urls_returns_top_level_ok(monkeypatch) -> None:
    class Response:
        def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
            self.status = status
            self.payload = payload or {"healthy": True}
            self.text = "<h1>WorkerBee POC</h1>"

        def json(self) -> dict[str, Any]:
            return self.payload

    def fake_request(url: str, **_: Any) -> Response:
        if url.endswith("/api/check"):
            return Response(200, {"ok": True})
        return Response(200)

    monkeypatch.setattr("workerbee.poc.request", fake_request)

    result = validate_poc_urls(
        {"store": "http://store", "api": "http://api", "frontend": "http://frontend"},
        timeout_seconds=1,
    )

    assert result["ok"] is True
    assert result["api_check"] == {"ok": True}


def test_containerd_poc_validation_execs_inside_containers(
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
    sup = WorkerBeeSupervisor(
        project="demo",
        state_dir=state_dir,
        runtime=CONTAINERD_RUNTIME,
        cwd=tmp_path,
    )
    info = StackInfo(
        project="demo",
        state_dir=str(state_dir),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin="/site-packages/ae/__init__.py",
        runtime=CONTAINERD_RUNTIME,
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
    monkeypatch.setattr(
        sup,
        "_runtime_container_ids",
        lambda _info, *, app, namespace: [f"{namespace}-{app}-cid"],
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        body = "<h1>WorkerBee POC</h1>" if cmd[-1].endswith("/") else '{"ok": true}'
        payload: dict[str, Any] = {"status": 200, "body": body}
        if body.startswith("{"):
            payload["json"] = {"ok": True}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr("workerbee.supervisor.subprocess.run", fake_run)

    result = sup._validate_poc_containerd(info, timeout_seconds=1)  # noqa: SLF001

    assert result["ok"] is True
    assert result["mode"] == "containerd-exec"
    assert result["api_check"] == {"ok": True}
    assert result["frontend"] == "<h1>WorkerBee POC</h1>"
    exec_targets = [cmd[cmd.index("exec") + 1] for cmd in calls]
    assert exec_targets == [
        "workerbee-poc-store-cid",
        "workerbee-poc-api-cid",
        "workerbee-poc-frontend-cid",
        "workerbee-poc-api-cid",
        "workerbee-poc-frontend-cid",
    ]
