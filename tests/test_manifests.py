from pathlib import Path
from types import MethodType
from typing import Any

from workerbee.k1s_runtime import K1sRuntime
from workerbee.manifests import (
    deploy_local_stage,
    deploy_remote_k1s_stage,
    export_bundle,
    prepare_stage,
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


def test_remote_deploy_uses_controller_apply_and_masks_token(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    prepared = prepare_stage(supervisor=sup, name="Demo Bundle", template="stateless-web")
    calls: list[list[str]] = []

    def fake_run(_self, args: list[str], *, timeout: int = 60) -> dict[str, Any]:
        assert timeout == 180
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
