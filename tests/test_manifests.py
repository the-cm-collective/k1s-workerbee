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


def _ready_wait(**kwargs: Any) -> dict[str, Any]:
    workloads = kwargs.get("workloads") or []
    return {
        "ready": True,
        "waited_seconds": 0.0,
        "statuses": [
            {
                "namespace": item["namespace"],
                "name": item["name"],
                "ready": True,
                "desired": 1,
                "ready_replicas": 1,
            }
            for item in workloads
        ],
    }


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


def test_validate_stage_rejects_plaintext_secretrefs_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    secret = tmp_path / "secret.yaml"
    secret.write_text("token: dont-store-plaintext\n", encoding="utf-8")
    (manifests / "web.k1s.yaml").write_text(
        f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: web
spec:
  image: workerbee-web:dev
  secretRefs:
    - name: app-secret
      path: {secret}
      env:
        - name: API_TOKEN
          key: token
""",
        encoding="utf-8",
    )

    result = validate_stage(stage, cwd=sup.cwd)

    assert result["ok"] is False
    assert {item["code"] for item in result["findings"]} >= {"PLAINTEXT_SECRET_REF"}


def test_validate_stage_allows_plaintext_secretrefs_with_explicit_opt_in(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKERBEE_ALLOW_PLAINTEXT_SECRETS", "1")
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    secret = tmp_path / "secret.yaml"
    secret.write_text("token: local-dev-only\n", encoding="utf-8")
    (manifests / "web.k1s.yaml").write_text(
        f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: web
spec:
  image: workerbee-web:dev
  secretRefs:
    - name: app-secret
      path: {secret}
      env:
        - name: API_TOKEN
          key: token
""",
        encoding="utf-8",
    )

    result = validate_stage(stage, cwd=sup.cwd)

    assert result["ok"] is True


def test_remote_deploy_fails_closed_for_secretrefs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    secret = tmp_path / "secret.sops.yaml"
    secret.write_text("token: ENC[test]\nsops: {}\n", encoding="utf-8")
    (manifests / "web.k1s.yaml").write_text(
        f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: web
spec:
  image: workerbee-web:dev
  secretRefs:
    - name: app-secret
      path: {secret}
""",
        encoding="utf-8",
    )

    try:
        deploy_remote_k1s_stage(
            supervisor=sup,
            stage_dir=stage,
            server="https://k1s.example",
            token="-".join(["admin", "token"]),
        )
    except WorkerBeeError as exc:
        assert exc.code == "REMOTE_SECRET_HANDOFF_UNSAFE"
    else:
        raise AssertionError("remote deploy should fail closed for secretRefs")


def test_k1s_export_copies_sops_secretrefs_and_rewrites_bundle_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    secret = tmp_path / "secret.sops.yaml"
    secret.write_text("token: ENC[test]\nsops: {}\n", encoding="utf-8")
    (manifests / "web.k1s.yaml").write_text(
        f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: web
spec:
  image: workerbee-web:dev
  secretRefs:
    - name: app-secret
      path: {secret}
""",
        encoding="utf-8",
    )

    result = export_bundle(supervisor=sup, stage_dir=stage, fmt="k1s")
    out_dir = Path(result["output_dir"])
    manifest = out_dir / "manifests" / "web.k1s.yaml"

    assert (out_dir / "secrets" / "secret.sops.yaml").is_file()
    assert "path: secrets/secret.sops.yaml" in manifest.read_text(encoding="utf-8")


def test_k8s_export_does_not_emit_secret_resources_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    secret = tmp_path / "secret.sops.yaml"
    secret.write_text("token: ENC[test]\nsops: {}\n", encoding="utf-8")
    manifest = manifests / "web.k1s.yaml"
    manifest.write_text(
        f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: web
spec:
  image: workerbee-web:dev
  secretRefs:
    - name: app-secret
      path: {secret}
""",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(
        _self,
        args: list[str],
        *,
        timeout: int = 60,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        _ = (timeout, env_overrides)
        calls.append(args)
        return {"stdout": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"}

    sup.run_ae_cli = MethodType(fake_run, sup)  # type: ignore[method-assign]

    export_bundle(supervisor=sup, stage_dir=stage, fmt="k8s")

    assert calls == [["export-k8s", "-f", str(manifest.resolve()), "--emit-configs", "--validate"]]


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


def test_validate_kubernetes_stage_rejects_multi_container_and_init_container(
    tmp_path: Path,
) -> None:
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
      initContainers:
        - name: init
          image: busybox:latest
      containers:
        - name: web
          image: nginx:latest
        - name: worker
          image: busybox:latest
""",
        encoding="utf-8",
    )

    result = validate_stage(stage)

    codes = {finding["code"] for finding in result["findings"]}
    assert result["ok"] is False
    assert "K8S_MULTI_CONTAINER_UNSUPPORTED" in codes
    assert "K8S_INIT_CONTAINERS_UNSUPPORTED" in codes


def test_validate_kubernetes_stage_warns_for_command_entrypoint_semantics(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "job.yaml").write_text(
        """apiVersion: batch/v1
kind: Job
metadata:
  name: minio-init
spec:
  template:
    spec:
      containers:
        - name: mc
          image: minio/mc:latest
          command: ["/bin/sh", "-c"]
          args: ["echo ok"]
""",
        encoding="utf-8",
    )

    result = validate_stage(stage)

    command_findings = [
        finding
        for finding in result["findings"]
        if finding["code"] == "K8S_COMMAND_ENTRYPOINT_SEMANTICS"
    ]
    assert result["ok"] is True
    assert any(
        finding["code"] == "K8S_COMMAND_ENTRYPOINT_SEMANTICS"
        for finding in result["findings"]
    )
    assert command_findings[0]["risk"] == "known_entrypoint_image"
    assert "known to use an entrypoint" in command_findings[0]["message"]


def test_validate_kubernetes_stage_skips_low_risk_local_python_command(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "worker.yaml").write_text(
        """apiVersion: batch/v1
kind: Job
metadata:
  name: bucket-init
spec:
  template:
    spec:
      containers:
        - name: init
          image: localhost/rawform-first-run:workerbee
          command: ["python", "-m", "rawform.bootstrap"]
""",
        encoding="utf-8",
    )

    result = validate_stage(stage)

    assert result["ok"] is True
    assert not any(
        finding["code"] == "K8S_COMMAND_ENTRYPOINT_SEMANTICS"
        for finding in result["findings"]
    )


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
    sup.load_stack = MethodType(  # type: ignore[method-assign]
        lambda _self: SimpleNamespace(runtime="docker", controller_url="", read_token=""),
        sup,
    )
    monkeypatch.setattr("workerbee.manifests._wait_for_service_workloads", _ready_wait)

    result = deploy_local_stage(supervisor=sup, stage_dir=stage, namespace="demo", timeout=55)

    assert result["ok"] is True
    assert calls == [(manifest.resolve(), "demo", 55)]
    assert result["alias_refresh"]["enabled"] is False
    assert result["app_status"]["state"] == "ready"


def test_local_deploy_reports_degraded_app_status(tmp_path: Path, monkeypatch) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    manifest = manifests / "api.k1s.yaml"
    manifest.write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: api
  namespace: demo
spec:
  image: workerbee-demo-api:dev
""",
        encoding="utf-8",
    )

    def fake_deploy(
        _self,
        path: Path,
        *,
        namespace: str | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        _ = (path, namespace, timeout)
        return {"ok": True, "manifest": str(manifest)}

    def degraded_wait(**kwargs: Any) -> dict[str, Any]:
        workload = kwargs["workloads"][0]
        return {
            "ready": False,
            "waited_seconds": 1.0,
            "statuses": [
                {
                    "namespace": workload["namespace"],
                    "name": workload["name"],
                    "ready": False,
                    "desired": 1,
                    "ready_replicas": 0,
                }
            ],
        }

    sup.deploy_manifest = MethodType(fake_deploy, sup)  # type: ignore[method-assign]
    sup.load_stack = MethodType(  # type: ignore[method-assign]
        lambda _self: SimpleNamespace(runtime="docker", controller_url="", read_token=""),
        sup,
    )
    monkeypatch.setattr("workerbee.manifests._wait_for_service_workloads", degraded_wait)

    result = deploy_local_stage(supervisor=sup, stage_dir=stage, timeout=30)

    assert result["apply_ok"] is True
    assert result["ok"] is False
    assert result["app_status"]["state"] == "degraded"
    assert result["app_status"]["degraded_workload_count"] == 1


def test_local_deploy_reports_and_prunes_orphaned_previous_workload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    manifest = manifests / "api.k1s.yaml"
    manifest.write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: api
  namespace: demo
spec:
  image: workerbee-demo-api:dev
""",
        encoding="utf-8",
    )
    previous = {
        "validation": {
            "workloads": [
                {"name": "api", "namespace": "demo", "kind": "Deployment"},
                {"name": "old-worker", "namespace": "demo", "kind": "Deployment"},
            ]
        }
    }
    deletes: list[list[str]] = []

    def fake_deploy(
        _self,
        path: Path,
        *,
        namespace: str | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        _ = (path, namespace, timeout)
        return {"ok": True, "manifest": str(manifest)}

    def fake_run_ae(
        _self,
        args: list[str],
        *,
        info: object,
        timeout: int = 60,
    ) -> dict[str, Any]:
        _ = (info, timeout)
        deletes.append(args)
        return {"stdout": "deleted old-worker"}

    sup.deploy_manifest = MethodType(fake_deploy, sup)  # type: ignore[method-assign]
    sup.run_ae = MethodType(fake_run_ae, sup)  # type: ignore[method-assign]
    sup.load_stack = MethodType(  # type: ignore[method-assign]
        lambda _self: SimpleNamespace(
            runtime="docker",
            controller_url="http://127.0.0.1:19108",
            read_token="-".join(["read", "token"]),
            admin_token="-".join(["admin", "token"]),
        ),
        sup,
    )
    monkeypatch.setattr("workerbee.manifests._wait_for_service_workloads", _ready_wait)

    report = deploy_local_stage(
        supervisor=sup,
        stage_dir=stage,
        previous_deployment=previous,
        prune=False,
    )
    pruned = deploy_local_stage(
        supervisor=sup,
        stage_dir=stage,
        previous_deployment=previous,
        prune=True,
    )

    assert report["ok"] is True
    assert report["app_status"]["state"] == "orphaned"
    assert report["app_status"]["orphaned_workload_count"] == 1
    assert "ae delete old-worker -n demo --purge" in report["app_status"]["orphaned_workloads"][0][
        "cleanup_command"
    ]
    assert pruned["ok"] is True
    assert pruned["app_status"]["state"] == "ready"
    assert pruned["app_status"]["deleted_orphans"][0]["name"] == "old-worker"
    assert any("delete" in args and "old-worker" in args for args in deletes)


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
    assert result["alias_refresh"]["published_service_workloads"] == [
        {"namespace": "rawform", "name": "api"},
        {"namespace": "rawform", "name": "minio"},
    ]
    assert result["alias_refresh"]["reapplied"] == 2


def test_local_containerd_native_deploy_refreshes_published_services_without_references(
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

    def fake_run_ae(
        _self,
        args: list[str],
        *,
        info: object,
        timeout: int = 60,
    ) -> dict[str, Any]:
        _ = (args, info, timeout)
        return {"stdout": '{"desired_replicas":1,"ready_replicas":1}'}

    sup.load_stack = MethodType(fake_load_stack, sup)  # type: ignore[method-assign]
    sup.deploy_manifest = MethodType(fake_deploy, sup)  # type: ignore[method-assign]
    sup.run_ae = MethodType(fake_run_ae, sup)  # type: ignore[method-assign]

    result = deploy_local_stage(supervisor=sup, stage_dir=stage, namespace=None, timeout=60)

    assert result["ok"] is True
    assert calls == ["api.k1s.yaml", "minio.k1s.yaml", "api.k1s.yaml", "minio.k1s.yaml"]
    assert result["alias_refresh"]["enabled"] is True
    assert result["alias_refresh"]["service_workloads"] == [
        {"namespace": "rawform", "name": "api"},
        {"namespace": "rawform", "name": "minio"},
    ]
    assert result["alias_refresh"]["published_service_workloads"] == [
        {"namespace": "rawform", "name": "api"},
        {"namespace": "rawform", "name": "minio"},
    ]
    assert result["alias_refresh"]["reapplied"] == 2


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
                "server": "http://127.0.0.1:19608",
                "api_server": "http://127.0.0.1:18645",
                "public_server": "https://k1s.demo-app.workerbee.localhost:19443/",
                "public_api_server": "https://k1s-api.demo-app.workerbee.localhost:19443/",
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
    assert result["server"] == "http://127.0.0.1:19608"
    assert result["public_server"] == "https://k1s.demo-app.workerbee.localhost:19443/"
    assert len(calls) == 3
    assert calls[0][0][:5] == [
        "--server",
        "http://127.0.0.1:19608",
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


def test_profile_deploy_can_reset_existing_profile_apps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    prepared = prepare_stage(supervisor=sup, name="Realtime", template="realtime-web-db")
    calls: list[list[str]] = []

    def fake_run(
        _self,
        args: list[str],
        *,
        timeout: int = 60,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert timeout == 77
        assert env_overrides is not None
        calls.append(args)
        return {"cmd": ["python", "-m", "ae.cli", "--token=***"], "returncode": 0}

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
                "server": "http://127.0.0.1:19608",
                "api_server": "http://127.0.0.1:18645",
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
        reset_existing=True,
    )

    assert result["ok"] is True
    assert [call[4] for call in calls[:3]] == ["delete", "delete", "delete"]
    assert [call[5] for call in calls[:3]] == ["backend", "db", "frontend"]
    assert all(call[-3:] == ["--purge", "-n", "demo"] for call in calls[:3])
    assert [call[4] for call in calls[3:]] == ["apply", "apply", "apply"]
    assert [item["name"] for item in result["reset"]] == ["backend", "db", "frontend"]


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
