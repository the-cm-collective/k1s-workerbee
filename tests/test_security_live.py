from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from workerbee.daemon import WorkerBeeDaemon
from workerbee.manifests import export_bundle, validate_stage
from workerbee.paths import daemon_project_state_dir
from workerbee.ports import choose_port
from workerbee.runtime_support import runtime_command_args
from workerbee.supervisor import project_slug

pytestmark = pytest.mark.skipif(
    os.getenv("WORKERBEE_LIVE_SECURITY_REPORT") != "1",
    reason="set WORKERBEE_LIVE_SECURITY_REPORT=1 to run the live security report scenario",
)


def test_live_security_web_app_generates_assessment_report(tmp_path: Path) -> None:
    runtime = os.getenv("WORKERBEE_SECURITY_LIVE_RUNTIME", "auto")
    project = project_slug(os.getenv("WORKERBEE_SECURITY_LIVE_PROJECT", "security-live-app"))
    state_root = tmp_path / "workerbee-state"
    report_path = _report_path(state_root)
    fixture_dir = Path(__file__).parent / "fixtures" / "security_web_app"
    daemon = WorkerBeeDaemon(
        state_root=state_root,
        runtime=runtime,
        default_project=project,
        cwd=fixture_dir,
    )
    assessment: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    report: dict[str, Any] | None = None

    try:
        ingress = daemon.start(mcp_bind_url="pytest-live-security")
        supervisor = daemon.supervisor(project)
        image = f"workerbee-{project}-security-web:dev"
        build = supervisor.build_image(fixture_dir, tag=image)
        service_port = choose_port(23080, start=23080, end=23180)
        stage_dir = _write_live_stage(
            state_root=state_root,
            project=project,
            image=image,
            ingress_host=f"app.{project}.workerbee.localhost",
            service_port=service_port,
        )
        validation = validate_stage(stage_dir)
        deploy = daemon.manifest_deploy_local(
            stage=stage_dir,
            project=project,
            namespace=project,
            timeout=300,
        )
        ingress_override = _install_host_port_ingress_override(
            supervisor=supervisor,
            project=project,
            service_port=service_port,
        )
        ingress_url = f"https://app.{project}.workerbee.localhost:{ingress.https_port}/"
        health = _wait_for_ingress_health(daemon, project=project, timeout_seconds=90)
        exports = {
            fmt: export_bundle(
                supervisor=supervisor,
                stage_dir=stage_dir,
                fmt=fmt,
                namespace=project,
            )
            for fmt in ("k1s", "k8s", "helm")
        }
        review = daemon.security_review_project(
            project=project,
            namespace=project,
            checks=["manifest", "export", "runtime", "headers"],
            timeout=5.0,
        )
        assessment = review["assessment"]
        report = {
            "api_version": "workerbee.security_report/v1",
            "scenario": "live-security-web-app",
            "project": project,
            "runtime": runtime,
            "ingress_url": ingress_url,
            "stage_dir": str(stage_dir),
            "build": _compact_build(build),
            "validation": _compact_validation(validation),
            "deploy": _compact_deploy(deploy),
            "ingress_override": ingress_override,
            "health": _compact_probe(health),
            "exports": {fmt: _compact_export(result) for fmt, result in exports.items()},
            "project_review": review,
            "assessment": assessment,
            "report_path": str(report_path),
            "generated_at": time.time(),
        }
        _write_report(report_path, report)

        codes = {str(item.get("code")) for item in assessment["findings"]}
        assert build["ok"] is True
        assert deploy["ok"] is True
        assert deploy["deployment"]["stage_dir"] == str(stage_dir)
        assert all(result["ok"] is True for result in exports.values())
        assert Path(review["report"]["path"]).is_file()
        assert assessment["ok"] is True
        assert assessment["checks"] == ["manifest", "export", "runtime", "headers"]
        assert {
            "MISSING_SECURITY_HEADERS",
            "API_DOCS_PUBLIC",
            "MUTATING_METHODS_EXPOSED",
            "LOCAL_IMAGE_REMOTE_HANDOFF",
            "IMAGE_PULL_POLICY_NEVER",
            "SECRET_LITERAL_ENV",
            "MISSING_RESOURCE_REQUESTS",
        }.issubset(codes)
        assert "super-secret-live-token" not in json.dumps(report, sort_keys=True)
        assert report_path.is_file()
    finally:
        if report is None and assessment is not None:
            _write_report(
                report_path,
                {
                    "api_version": "workerbee.security_report/v1",
                    "scenario": "live-security-web-app",
                    "project": project,
                    "runtime": runtime,
                    "assessment": assessment,
                    "report_path": str(report_path),
                    "generated_at": time.time(),
                },
            )
        _cleanup_daemon(daemon, project=project)


def _write_live_stage(
    *,
    state_root: Path,
    project: str,
    image: str,
    ingress_host: str,
    service_port: int,
) -> Path:
    stage_dir = daemon_project_state_dir(project, state_root=state_root) / "artifacts" / "staged"
    stage_dir = stage_dir / "security-live-web"
    manifest_dir = stage_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "web.k1s.yaml").write_text(
        f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: security-web
  namespace: {project}
  labels:
    workerbee.k1s.dev/project: {project}
spec:
  image: {image}
  imagePullPolicy: Never
  replicas: 1
  env:
    - name: SECRET_TOKEN
      value: super-secret-live-token
  ports:
    - name: http
      containerPort: 8080
  service:
    port: {service_port}
    targetPort: 8080
  health:
    readiness:
      httpGet: {{ path: /healthz, port: 8080 }}
      initialDelaySeconds: 1
      periodSeconds: 2
  ingress:
    host: {ingress_host}
    path: /
""",
        encoding="utf-8",
    )
    return stage_dir


def _wait_for_ingress_health(
    daemon: WorkerBeeDaemon,
    *,
    project: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            result = daemon.ingress_probe(
                project=project,
                host=f"app.{project}.workerbee.localhost",
                path="/healthz",
                expected_status=200,
                timeout=5.0,
            )
        except Exception as exc:  # noqa: BLE001 - live readiness retry
            last = {"ok": False, "error": str(exc)}
        else:
            last = result
            if result.get("ok"):
                return result
        time.sleep(1.0)
    raise TimeoutError(f"live security app ingress did not become ready: {last}")


def _install_host_port_ingress_override(
    *,
    supervisor: Any,
    project: str,
    service_port: int,
) -> dict[str, Any]:
    if supervisor.ingress is None:
        return {"ok": False, "enabled": False, "reason": "WorkerBee ingress is not configured"}
    site = supervisor.ingress.sites_dir / f"{project}--security-web.caddy"
    host = f"app.{project}.workerbee.localhost"
    content = f"""# Generated by WorkerBee live security report test.
https://{host} {{
    log {{
        output stdout
        format console
    }}
    tls internal
    reverse_proxy {supervisor.ingress.host_alias}:{service_port}
}}
"""
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(content, encoding="utf-8")
    supervisor._reload_ingress()  # noqa: SLF001 - live test validates WorkerBee Caddy edge
    return {
        "ok": True,
        "enabled": True,
        "host": host,
        "service_port": service_port,
        "site": str(site),
        "upstream": f"{supervisor.ingress.host_alias}:{service_port}",
    }


def _report_path(state_root: Path) -> Path:
    configured = os.getenv("WORKERBEE_SECURITY_REPORT_OUT")
    if configured:
        return Path(configured).expanduser().resolve()
    return state_root / "reports" / "security-live-report.json"


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def _compact_build(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "runtime": result.get("runtime"),
        "build_backend": result.get("build_backend"),
        "tag": result.get("tag"),
        "context": result.get("context"),
        "dockerfile": result.get("dockerfile"),
    }


def _compact_validation(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "input_kinds": result.get("input_kinds", []),
        "images": result.get("images", []),
        "finding_count": len(result.get("findings", [])),
    }


def _compact_deploy(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "project": result.get("project"),
        "validation_ok": result.get("validation", {}).get("ok"),
        "apply_count": len(result.get("apply", [])),
        "alias_refresh": result.get("alias_refresh"),
    }


def _compact_probe(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "url": result.get("url"),
        "status": result.get("status"),
        "elapsed_ms": result.get("elapsed_ms"),
        "headers": result.get("headers", {}),
    }


def _compact_export(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "format": result.get("format"),
        "output_dir": result.get("output_dir"),
        "file_count": len(result.get("files", [])),
    }


def _cleanup_daemon(daemon: WorkerBeeDaemon, *, project: str) -> None:
    with suppress(Exception):
        daemon.with_project(project, lambda supervisor: supervisor.stop(purge=True))
    _clear_caddy_data(daemon)
    with suppress(Exception):
        daemon.stop_global_ingress()
    dashboard = getattr(daemon, "_dashboard", None)
    if dashboard is not None:
        dashboard.shutdown()
        dashboard.server_close()
    state_lock = getattr(daemon, "_state_lock", None)
    if state_lock is not None:
        state_lock.release()


def _clear_caddy_data(daemon: WorkerBeeDaemon) -> None:
    ingress = getattr(daemon, "ingress", None)
    if ingress is None:
        return
    with suppress(Exception):
        subprocess.run(
            runtime_command_args(
                ingress.runtime,
                state_root=daemon.state_root,
                project=None,
                system=True,
                args=["exec", ingress.container, "sh", "-c", "rm -rf /data/*"],
            ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
