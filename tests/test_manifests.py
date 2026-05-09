from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

from workerbee.contract import WorkerBeeError
from workerbee.k1s_runtime import K1sRuntime
from workerbee.manifests import (
    deploy_local_stage,
    deploy_profile_stage,
    deploy_remote_k1s_stage,
    export_bundle,
    prepare_stage,
    resolve_stage_dir,
    validate_stage,
)
from workerbee.supervisor import WorkerBeeSupervisor


def _runtime() -> K1sRuntime:
    return K1sRuntime(
        source="installed",
        python_executable="/usr/bin/python",
        k1s_root=None,
        pythonpath=None,
        ae_origin="/site-packages/ae/__init__.py",
    )


def _supervisor(tmp_path: Path, monkeypatch) -> WorkerBeeSupervisor:
    monkeypatch.setattr("workerbee.supervisor.resolve_k1s_runtime", lambda **_: _runtime())
    return WorkerBeeSupervisor(project="Demo App", state_dir=tmp_path / "state", runtime="docker")


def test_prepare_and_validate_native_stage(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, monkeypatch)

    prepared = prepare_stage(supervisor=sup, name="Demo Bundle", template="frontend-api-store")
    validation = validate_stage(Path(prepared["stage_dir"]))

    assert prepared["project"] == "demo-app"
    assert validation["ok"] is True
    assert validation["required_controller_scopes"] == [
        "demo-app/api",
        "demo-app/frontend",
        "demo-app/store",
    ]
    assert "workerbee-demo-app-api:dev" in validation["images"]
    assert (Path(prepared["stage_dir"]) / "bundle.json").is_file()
    images = Path(prepared["stage_dir"]) / "images.json"
    assert "workerbee-demo-app-api:dev" in images.read_text(encoding="utf-8")


def test_named_stage_resolves_under_project_artifacts(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    prepared = prepare_stage(supervisor=sup, name="RawForm Rerun", template="stateless-web")

    resolved = resolve_stage_dir(sup, "rawform-rerun")

    assert resolved == Path(prepared["stage_dir"]).resolve()
    assert validate_stage(resolved)["ok"] is True


def test_remote_deploy_uses_controller_apply_and_masks_token(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    prepared = prepare_stage(supervisor=sup, name="Demo Bundle", template="stateless-web")
    calls: list[list[str]] = []

    def fake_run(
        _self,
        args: list[str],
        *,
        timeout: int = 60,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert timeout == 180
        assert env_overrides is None
        calls.append(args)
        return {"cmd": ["python", "-m", "ae.cli", "--token", "***"], "returncode": 0}

    sup.run_ae_cli = MethodType(fake_run, sup)  # type: ignore[method-assign]
    remote_token = "-".join(["secret", "token"])
    result = deploy_remote_k1s_stage(
        supervisor=sup,
        stage_dir=Path(prepared["stage_dir"]),
        server="https://k1s.example",
        token=remote_token,
    )

    assert result["ok"] is True
    assert calls[0][:5] == ["--server", "https://k1s.example", "--token", remote_token, "apply"]
    assert result["apply"][0]["apply"]["cmd"][-1] == "***"


def test_export_k1s_bundle_copies_staged_manifests(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    prepared = prepare_stage(supervisor=sup, name="Demo Bundle", template="stateless-web")

    result = export_bundle(
        supervisor=sup,
        stage_dir=Path(prepared["stage_dir"]),
        fmt="k1s",
    )

    assert result["format"] == "k1s"
    assert any(path.endswith("manifests/web.k1s.yaml") for path in result["files"])
    assert any(path.endswith("images.json") for path in result["files"])


def test_validate_kubernetes_stage_accepts_one_workload_bundle(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "web.yaml").write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: demo
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: web
          image: workerbee-demo-web:dev
---
apiVersion: v1
kind: Service
metadata:
  name: web
  namespace: demo
spec:
  selector:
    app: web
  ports:
    - port: 80
      targetPort: 8080
""",
        encoding="utf-8",
    )

    result = validate_stage(stage)

    assert result["ok"] is True
    assert result["input_kinds"] == ["kubernetes"]
    assert result["required_controller_scopes"] == ["demo/web"]
    assert result["manifest_details"][0]["supported_export_formats"] == ["k8s", "helm"]


def test_local_deploy_uses_k8s_apply_flag(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    manifest = manifests / "web.yaml"
    manifest.write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  template:
    spec:
      containers:
        - name: web
          image: nginx:latest
""",
        encoding="utf-8",
    )
    calls: list[tuple[Path, str | None, int]] = []

    def fake_deploy(
        _self,
        path: Path,
        *,
        namespace: str | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        calls.append((path, namespace, timeout))
        return {"ok": True, "input_kind": "kubernetes", "manifest": str(path)}

    sup.deploy_k8s_manifest = MethodType(fake_deploy, sup)  # type: ignore[method-assign]

    result = deploy_local_stage(supervisor=sup, stage_dir=stage, namespace="demo", timeout=55)

    assert result["ok"] is True
    assert calls == [(manifest.resolve(), "demo", 55)]
    assert result["alias_refresh"]["enabled"] is False


def test_local_containerd_native_deploy_reapplies_after_service_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.supervisor.resolve_k1s_runtime", lambda **_: _runtime())
    sup = WorkerBeeSupervisor(project="RawForm", state_dir=tmp_path / "state", runtime="containerd")
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    api = manifests / "api.k1s.yaml"
    api.write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: api
  namespace: rawform
spec:
  image: workerbee-api:dev
  env:
    - name: S3_ENDPOINT
      value: http://minio:9000
  service:
    port: 8000
""",
        encoding="utf-8",
    )
    minio = manifests / "minio.k1s.yaml"
    minio.write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: minio
  namespace: rawform
spec:
  image: minio/minio:latest
  service:
    port: 9000
""",
        encoding="utf-8",
    )
    stack = SimpleNamespace(
        runtime="containerd",
        controller_url="http://127.0.0.1:19108",
        read_token="-".join(["token", "for", "test"]),
    )
    calls: list[str] = []

    def fake_load_stack(_self) -> object:
        return stack

    def fake_deploy(
        _self,
        path: Path,
        *,
        namespace: str | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        _ = (namespace, timeout)
        calls.append(path.name)
        return {"ok": True, "manifest": str(path)}

    def fake_run_ae(
        _self,
        args: list[str],
        *,
        info: object,
        timeout: int = 60,
    ) -> dict[str, Any]:
        _ = (args, info, timeout)
        return {
            "stdout": (
                '{"app_name":"rawform--minio","desired_replicas":1,'
                '"ready_replicas":1,"live_replicas":1}'
            )
        }

    sup.load_stack = MethodType(fake_load_stack, sup)  # type: ignore[method-assign]
    sup.deploy_manifest = MethodType(fake_deploy, sup)  # type: ignore[method-assign]
    sup.run_ae = MethodType(fake_run_ae, sup)  # type: ignore[method-assign]

    result = deploy_local_stage(supervisor=sup, stage_dir=stage, namespace=None, timeout=60)

    assert result["ok"] is True
    assert calls == ["api.k1s.yaml", "minio.k1s.yaml", "api.k1s.yaml", "minio.k1s.yaml"]
    assert result["alias_refresh"]["enabled"] is True
    assert result["alias_refresh"]["ready"] is True
    assert result["alias_refresh"]["service_workloads"] == [
        {"namespace": "rawform", "name": "minio"}
    ]
    assert result["alias_refresh"]["reapplied"] == 2


def test_local_containerd_native_deploy_skips_alias_refresh_without_references(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.supervisor.resolve_k1s_runtime", lambda **_: _runtime())
    sup = WorkerBeeSupervisor(project="RawForm", state_dir=tmp_path / "state", runtime="containerd")
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "api.k1s.yaml").write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: api
  namespace: rawform
spec:
  image: workerbee-api:dev
  service:
    port: 8000
""",
        encoding="utf-8",
    )
    (manifests / "minio.k1s.yaml").write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: minio
  namespace: rawform
spec:
  image: minio/minio:latest
  service:
    port: 9000
""",
        encoding="utf-8",
    )
    stack = SimpleNamespace(
        runtime="containerd",
        controller_url="http://127.0.0.1:19108",
        read_token="-".join(["token", "for", "test"]),
    )
    calls: list[str] = []

    def fake_load_stack(_self) -> object:
        return stack

    def fake_deploy(
        _self,
        path: Path,
        *,
        namespace: str | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        _ = (namespace, timeout)
        calls.append(path.name)
        return {"ok": True, "manifest": str(path)}

    sup.load_stack = MethodType(fake_load_stack, sup)  # type: ignore[method-assign]
    sup.deploy_manifest = MethodType(fake_deploy, sup)  # type: ignore[method-assign]

    result = deploy_local_stage(supervisor=sup, stage_dir=stage, namespace=None, timeout=60)

    assert result["ok"] is True
    assert calls == ["api.k1s.yaml", "minio.k1s.yaml"]
    assert result["alias_refresh"]["enabled"] is False
    assert result["alias_refresh"]["reason"] == "no referenced native k1s service workloads"


def test_local_containerd_native_deploy_does_not_reapply_when_service_not_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.supervisor.resolve_k1s_runtime", lambda **_: _runtime())
    sup = WorkerBeeSupervisor(project="RawForm", state_dir=tmp_path / "state", runtime="containerd")
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    api = manifests / "api.k1s.yaml"
    api.write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: api
  namespace: rawform
spec:
  image: workerbee-api:dev
  env:
    - name: S3_ENDPOINT
      value: http://minio:9000
  service:
    port: 8000
""",
        encoding="utf-8",
    )
    minio = manifests / "minio.k1s.yaml"
    minio.write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: minio
  namespace: rawform
spec:
  image: minio/minio:latest
  service:
    port: 9000
""",
        encoding="utf-8",
    )
    stack = SimpleNamespace(
        runtime="containerd",
        controller_url="http://127.0.0.1:19108",
        read_token="-".join(["token", "for", "test"]),
    )
    calls: list[str] = []

    def fake_load_stack(_self) -> object:
        return stack

    def fake_deploy(
        _self,
        path: Path,
        *,
        namespace: str | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        _ = (namespace, timeout)
        calls.append(path.name)
        return {"ok": True, "manifest": str(path)}

    def fake_wait(**_kwargs) -> dict[str, Any]:
        return {
            "ready": False,
            "waited_seconds": 20.0,
            "statuses": [
                {
                    "namespace": "rawform",
                    "name": "minio",
                    "ready": False,
                    "desired": 1,
                    "ready_replicas": 0,
                }
            ],
        }

    sup.load_stack = MethodType(fake_load_stack, sup)  # type: ignore[method-assign]
    sup.deploy_manifest = MethodType(fake_deploy, sup)  # type: ignore[method-assign]
    monkeypatch.setattr("workerbee.manifests._wait_for_service_workloads", fake_wait)

    try:
        deploy_local_stage(supervisor=sup, stage_dir=stage, namespace=None, timeout=60)
    except WorkerBeeError as exc:
        assert exc.code == "CONTAINERD_SERVICE_ALIAS_NOT_READY"
        alias_refresh = exc.details["alias_refresh"]
        assert alias_refresh["ready"] is False
        assert alias_refresh["reapplied"] == 0
    else:
        raise AssertionError("expected WorkerBeeError")
    assert calls == ["api.k1s.yaml", "minio.k1s.yaml"]


def test_profile_deploy_uses_internal_profile_connection(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    prepared = prepare_stage(supervisor=sup, name="Realtime", template="realtime-web-db")
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(
        _self,
        args: list[str],
        *,
        timeout: int = 60,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert timeout == 77
        calls.append((args, env_overrides))
        return {"cmd": ["python", "-m", "ae.cli", "--token", "***"], "returncode": 0}

    class FakeProfileRunner:
        def connection(
            self,
            *,
            profile: str | None = None,
            timeout: float = 180.0,
        ) -> dict[str, Any]:
            assert profile == "k1s-dev-min-sqlite"
            assert timeout == 77.0
            ca_bundle = str(tmp_path / "workerbee-ca.pem")
            return {
                "profile": profile,
                "server": "https://k1s.demo-app.workerbee.localhost:19443/",
                "api_server": "https://k1s-api.demo-app.workerbee.localhost:19443/",
                "ca_bundle": ca_bundle,
                "admin_token": "-".join(["admin", "token"]),
                "urls": {"dashboard": "https://k1s.demo-app.workerbee.localhost:19443/dashboard"},
            }

    sup.run_ae_cli = MethodType(fake_run, sup)  # type: ignore[method-assign]
    result = deploy_profile_stage(
        supervisor=sup,
        profile_runner=FakeProfileRunner(),
        stage_dir=Path(prepared["stage_dir"]),
        profile="k1s-dev-min-sqlite",
        namespace="demo",
        timeout=77,
    )

    assert result["ok"] is True
    assert result["target"] == "profile"
    assert result["server"] == "https://k1s.demo-app.workerbee.localhost:19443/"
    assert len(calls) == 3
    assert calls[0][0][:5] == [
        "--server",
        "https://k1s.demo-app.workerbee.localhost:19443/",
        "--token",
        "-".join(["admin", "token"]),
        "apply",
    ]
    ca_bundle = str(tmp_path / "workerbee-ca.pem")
    assert calls[0][1] == {
        "AE_APISHIM_CA_BUNDLE": ca_bundle,
        "SSL_CERT_FILE": ca_bundle,
        "REQUESTS_CA_BUNDLE": ca_bundle,
    }


def test_realtime_template_contains_websocket_ingress(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("workerbee.manifests.port_is_free", lambda _port, **_kwargs: True)
    sup = _supervisor(tmp_path, monkeypatch)
    prepared = prepare_stage(supervisor=sup, name="Realtime", template="realtime-web-db")

    validation = validate_stage(Path(prepared["stage_dir"]))
    backend = Path(prepared["stage_dir"]) / "manifests" / "backend.k1s.yaml"
    frontend = Path(prepared["stage_dir"]) / "manifests" / "frontend.k1s.yaml"
    db = Path(prepared["stage_dir"]) / "manifests" / "db.k1s.yaml"
    backend_text = backend.read_text(encoding="utf-8")
    frontend_text = frontend.read_text(encoding="utf-8")
    db_text = db.read_text(encoding="utf-8")

    assert validation["ok"] is True
    assert validation["required_controller_scopes"] == [
        "demo-app/backend",
        "demo-app/db",
        "demo-app/frontend",
    ]
    assert "workerbee-demo-app-realtime-backend:dev" in validation["images"]
    assert "host: api.demo-app.workerbee.localhost" in backend_text
    assert "path: /" in backend_text
    assert "- /ws" not in backend_text
    assert "host.containers.internal" in backend_text
    assert "host.containers.internal" in frontend_text
    assert "port: 8080\n    targetPort: 8080" not in db_text
    assert "wss://api.demo-app.workerbee.localhost:19443/ws" in frontend_text


def test_k1s_export_rejects_kubernetes_stage(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "web.yaml").write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  template:
    spec:
      containers:
        - name: web
          image: nginx:latest
""",
        encoding="utf-8",
    )

    try:
        export_bundle(supervisor=sup, stage_dir=stage, fmt="k1s")
    except Exception as exc:  # noqa: BLE001
        assert "native k1s bundle export requires native k1s staged manifests" in str(exc)
    else:
        raise AssertionError("expected k1s export to reject Kubernetes inputs")
