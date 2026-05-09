import json
from pathlib import Path
from types import SimpleNamespace

from workerbee.contract import WorkerBeeError
from workerbee.daemon import WorkerBeeDaemon
from workerbee.k1s_runtime import K1sRuntime
from workerbee.paths import daemon_project_state_dir
from workerbee.security import assess_stage_security
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


def _codes(result: dict) -> set[str]:
    return {str(item.get("code")) for item in result["findings"]}


def test_security_assess_flags_k8s_manifest_risks_and_redacts_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
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
  template:
    spec:
      containers:
        - name: web
          image: nginx:latest
          env:
            - name: SECRET_TOKEN
              value: dont-leak
          securityContext:
            privileged: true
          resources:
            requests:
              cpu: "100m"
              memory: 128Mi
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  namespace: demo
spec:
  rules:
    - host: web.demo.workerbee.localhost
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: web
                port:
                  number: 80
""",
        encoding="utf-8",
    )

    result = assess_stage_security(supervisor=sup, stage_dir=stage, checks=["manifest"])

    codes = _codes(result)
    assert result["ok"] is True
    assert result["mode"] == "advisory"
    assert result["passed"] is False
    assert "IMAGE_NOT_PINNED" in codes
    assert "SECRET_LITERAL_ENV" in codes
    assert "PRIVILEGED_CONTAINER" in codes
    assert "K8S_INGRESS_TLS_MISSING" in codes
    assert "dont-leak" not in str(result)
    assert result["risk_score"] > 0
    assert result["suggested_patches"]


def test_security_assess_runs_runtime_header_and_api_doc_probes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "api.k1s.yaml").write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: api
  namespace: demo
spec:
  image: registry.example/api:v1
  ingress:
    host: api.demo.workerbee.localhost
    path: /
""",
        encoding="utf-8",
    )

    def fake_probe(*, url: str, method: str, timeout: float) -> dict:
        _ = timeout
        if method == "OPTIONS":
            return {"ok": True, "status": 204, "headers": {"Allow": "GET, POST, DELETE"}}
        if url.endswith("/openapi.json"):
            return {"ok": True, "status": 200, "headers": {"Content-Type": "application/json"}}
        return {"ok": True, "status": 200, "headers": {"Content-Type": "application/json"}}

    result = assess_stage_security(
        supervisor=sup,
        stage_dir=stage,
        checks=["runtime", "headers"],
        runtime_probe=fake_probe,
    )

    codes = _codes(result)
    assert "MISSING_SECURITY_HEADERS" in codes
    assert "MUTATING_METHODS_EXPOSED" in codes
    assert "API_DOCS_PUBLIC" in codes
    assert any(
        item["check"] == "runtime" and item["status"] == "completed" for item in result["evidence"]
    )


def test_security_assess_reviews_existing_exports_without_generating_them(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = _supervisor(tmp_path, monkeypatch)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "web.k1s.yaml").write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: web
spec:
  image: registry.example/web:v1
""",
        encoding="utf-8",
    )
    exports = stage / "exports" / "k8s"
    exports.mkdir(parents=True)
    (exports / "secret.yaml").write_text(
        """apiVersion: v1
kind: Secret
metadata:
  name: app-secret
stringData:
  password: dont-leak
""",
        encoding="utf-8",
    )

    result = assess_stage_security(supervisor=sup, stage_dir=stage, checks=["export"])

    assert "EXPORTED_SECRET_REVIEW_REQUIRED" in _codes(result)
    assert "dont-leak" not in str(result)
    assert any(
        item["check"] == "export" and item["status"] == "scanned" for item in result["evidence"]
    )


def test_security_review_project_resolves_latest_deployment_and_writes_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.supervisor.resolve_k1s_runtime", lambda **_: _runtime())
    monkeypatch.setattr(
        WorkerBeeSupervisor,
        "status",
        lambda self: {  # noqa: ARG005
            "running": True,
            "apishim_running": True,
            "stack": {"dashboard_url": "http://127.0.0.1:19108/dashboard"},
        },
    )
    monkeypatch.setattr(
        WorkerBeeSupervisor,
        "start",
        lambda self: SimpleNamespace(  # noqa: ARG005
            dashboard_url="http://127.0.0.1:19108/dashboard",
            controller_url="http://127.0.0.1:19108",
            apishim_url="https://127.0.0.1:19109",
        ),
    )
    monkeypatch.setattr(
        WorkerBeeSupervisor,
        "load_stack",
        lambda self: SimpleNamespace(runtime="docker"),  # noqa: ARG005
    )

    def fake_deploy_manifest(
        self: WorkerBeeSupervisor,
        manifest: Path,
        *,
        namespace: str | None = None,
        timeout: int = 180,
    ) -> dict:
        _ = timeout
        return {
            "ok": True,
            "project": self.project,
            "manifest": str(manifest),
            "namespace": namespace,
            "ingress_urls": ["https://app.demo.workerbee.localhost:19443/"],
        }

    monkeypatch.setattr(WorkerBeeSupervisor, "deploy_manifest", fake_deploy_manifest)
    stage = tmp_path / "stage"
    manifests = stage / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "web.k1s.yaml").write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: web
  namespace: demo
spec:
  image: registry.example/web:v1
  ingress:
    host: app.demo.workerbee.localhost
    path: /
""",
        encoding="utf-8",
    )
    daemon = WorkerBeeDaemon(
        state_root=tmp_path / "state",
        runtime="docker",
        default_project="demo",
        cwd=tmp_path,
    )

    deploy = daemon.manifest_deploy_local(stage=stage, project="demo", namespace="demo")

    assert deploy["deployment"]["stage_dir"] == str(stage.resolve())
    assert deploy["deployment"]["ingress_urls"] == ["https://app.demo.workerbee.localhost:19443/"]
    status = daemon.project_status("demo")
    assert status["latest_deployment"]["stage_dir"] == str(stage.resolve())

    review = daemon.security_review_project(project="demo", checks=["manifest"])

    assert review["deployment"]["id"] == deploy["deployment"]["id"]
    assert review["assessment"]["project"] == "demo"
    report_path = Path(review["report"]["path"])
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["api_version"] == "workerbee.security_report/v1"
    assert report["review"]["assessment"]["stage_dir"] == str(stage.resolve())


def test_security_review_project_requires_deployment_or_explicit_stage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.supervisor.resolve_k1s_runtime", lambda **_: _runtime())
    state_root = tmp_path / "state"
    state_dir = daemon_project_state_dir("demo", state_root=state_root)
    (state_dir / "artifacts" / "staged" / "candidate").mkdir(parents=True)
    daemon = WorkerBeeDaemon(
        state_root=state_root,
        runtime="docker",
        default_project="demo",
        cwd=tmp_path,
    )

    try:
        daemon.security_review_project(project="demo")
    except WorkerBeeError as exc:
        assert exc.code == "SECURITY_REVIEW_DEPLOYMENT_REQUIRED"
        assert exc.details["available_stages"] == ["candidate"]
    else:
        raise AssertionError("security review should require a deployment or explicit stage")
