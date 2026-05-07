"""Staged manifest and artifact bundle helpers."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - exercised when PyYAML is not installed
    yaml = None  # type: ignore[assignment]

from workerbee import __version__
from workerbee.contract import WorkerBeeError
from workerbee.supervisor import WorkerBeeSupervisor, project_slug

SUPPORTED_TEMPLATES = {"stateless-web", "frontend-api", "frontend-api-store"}
NATIVE_K1S = "native-k1s"
KUBERNETES = "kubernetes"
K8S_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
K8S_NETWORK_KINDS = {"Service", "Ingress"}
K8S_SUPPORTED_KINDS = K8S_WORKLOAD_KINDS | K8S_NETWORK_KINDS


@dataclass(frozen=True, slots=True)
class StageRef:
    project: str
    name: str
    stage_dir: Path
    manifest_dir: Path


def prepare_stage(
    *,
    supervisor: WorkerBeeSupervisor,
    name: str,
    template: str = "frontend-api-store",
    source: Path | None = None,
) -> dict[str, Any]:
    project = supervisor.project
    stage = _stage_ref(supervisor.state_dir, project=project, name=name)
    if stage.stage_dir.exists():
        shutil.rmtree(stage.stage_dir)
    stage.manifest_dir.mkdir(parents=True, exist_ok=True)
    (stage.stage_dir / "configs").mkdir(parents=True, exist_ok=True)
    (stage.stage_dir / "secrets").mkdir(parents=True, exist_ok=True)
    if source is not None:
        copied = _copy_source(source.expanduser().resolve(), stage.manifest_dir)
    else:
        copied = _write_template(stage.manifest_dir, template=template, project=project)
    bundle = _bundle_metadata(stage, manifests=copied, template=template, source=source)
    _write_json(stage.stage_dir / "bundle.json", bundle)
    _write_json(stage.stage_dir / "images.json", {"images": []})
    _write_stage_readme(stage)
    validation = validate_stage(stage.stage_dir)
    _write_json(stage.stage_dir / "images.json", {"images": validation.get("images", [])})
    return {
        "project": project,
        "stage_dir": str(stage.stage_dir),
        "manifests": [str(path) for path in copied],
        "bundle": str(stage.stage_dir / "bundle.json"),
        "validation": validation,
    }


def validate_stage(stage_dir: Path) -> dict[str, Any]:
    root = stage_dir.expanduser().resolve()
    manifest_dir = root / "manifests"
    paths = sorted(manifest_dir.glob("*.yaml")) + sorted(manifest_dir.glob("*.yml"))
    findings: list[dict[str, Any]] = []
    scopes: list[str] = []
    details: list[dict[str, Any]] = []
    images_seen: list[str] = []
    if not paths:
        findings.append({"level": "error", "code": "NO_MANIFESTS", "message": "no YAML manifests"})
    for path in paths:
        text = path.read_text(encoding="utf-8")
        docs = _load_yaml_documents(text)
        detail = _manifest_detail(path, docs)
        details.append(detail)
        if detail["input_kind"] == "unknown":
            findings.append(
                {
                    "level": "error",
                    "code": "UNKNOWN_MANIFEST_INPUT",
                    "path": str(path),
                    "message": (
                        "manifest must be native ae.dev/v1alpha1 or practical Kubernetes "
                        "Deployment/StatefulSet/DaemonSet/Job plus optional Service/Ingress"
                    ),
                }
            )
            continue
        if detail["input_kind"] == "mixed":
            findings.append(
                {
                    "level": "error",
                    "code": "MIXED_MANIFEST_INPUT",
                    "path": str(path),
                    "message": "do not mix native k1s and Kubernetes documents in one file",
                }
            )
            continue
        if detail["input_kind"] == NATIVE_K1S:
            findings.extend(_validate_native_documents(path, docs))
        if detail["input_kind"] == KUBERNETES:
            findings.extend(_validate_k8s_documents(path, docs))
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if detail["input_kind"] == KUBERNETES and _kind(doc) not in K8S_WORKLOAD_KINDS:
                continue
            metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
            namespace = str(metadata.get("namespace") or "default")
            name = str(metadata.get("name") or path.stem)
            scopes.append(f"{namespace}/{name}")
            images = (
                _native_images(spec) if detail["input_kind"] == NATIVE_K1S else _k8s_images(doc)
            )
            for image in images:
                images_seen.append(image)
                if not image.startswith("workerbee-"):
                    continue
                findings.append(
                    {
                        "level": "warning",
                        "code": "LOCAL_IMAGE",
                        "path": str(path),
                        "message": f"{image} is local; retag/push before remote k1s deploy",
                    }
                )
    errors = [item for item in findings if item.get("level") == "error"]
    return {
        "ok": not errors,
        "stage_dir": str(root),
        "manifests": [str(path) for path in paths],
        "manifest_details": details,
        "input_kinds": sorted({item["input_kind"] for item in details if item["input_kind"]}),
        "images": sorted(set(images_seen)),
        "findings": findings,
        "required_controller_scopes": sorted(set(scopes)),
    }


def deploy_local_stage(
    *,
    supervisor: WorkerBeeSupervisor,
    stage_dir: Path,
    namespace: str | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    validation = validate_stage(stage_dir)
    if not validation["ok"]:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="staged manifests are not valid",
            details=validation,
            remediation="Fix staged files and run validation again.",
        )
    results = []
    for detail in validation["manifest_details"]:
        manifest = Path(str(detail["path"]))
        if detail["input_kind"] == KUBERNETES:
            results.append(
                supervisor.deploy_k8s_manifest(manifest, namespace=namespace, timeout=timeout)
            )
        else:
            results.append(
                supervisor.deploy_manifest(manifest, namespace=namespace, timeout=timeout)
            )
    return {"ok": True, "validation": validation, "apply": results}


def deploy_remote_k1s_stage(
    *,
    supervisor: WorkerBeeSupervisor,
    stage_dir: Path,
    server: str,
    token: str,
    namespace: str | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    if not server:
        raise ValueError("remote k1s server URL is required")
    if not token:
        raise WorkerBeeError(
            code="AUTH_REQUIRED",
            message="remote k1s admin token is required",
            remediation="Provide a controller admin token scoped to every manifest namespace/name.",
        )
    validation = validate_stage(stage_dir)
    if not validation["ok"]:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="staged manifests are not valid",
            details=validation,
        )
    results = []
    for detail in validation["manifest_details"]:
        manifest = Path(str(detail["path"]))
        if detail["input_kind"] == KUBERNETES:
            results.append(
                supervisor.deploy_remote_k8s_manifest(
                    manifest,
                    server=server,
                    token=token,
                    namespace=namespace,
                    timeout=timeout,
                )
            )
        else:
            results.append(
                supervisor.deploy_remote_manifest(
                    manifest,
                    server=server,
                    token=token,
                    namespace=namespace,
                    timeout=timeout,
                )
            )
    return {
        "ok": True,
        "server": server,
        "validation": validation,
        "apply": results,
    }


def export_bundle(
    *,
    supervisor: WorkerBeeSupervisor,
    stage_dir: Path,
    fmt: str = "k1s",
    namespace: str | None = None,
) -> dict[str, Any]:
    fmt = fmt.lower()
    validation = validate_stage(stage_dir)
    if not validation["manifests"]:
        raise RuntimeError("no staged manifests to export")
    out_dir = stage_dir.expanduser().resolve() / "exports" / fmt
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "k1s":
        unsupported = [
            item["path"]
            for item in validation["manifest_details"]
            if item["input_kind"] != NATIVE_K1S
        ]
        if unsupported:
            raise WorkerBeeError(
                code="UNSUPPORTED_EXPORT_FORMAT",
                message="native k1s bundle export requires native k1s staged manifests",
                details={"unsupported_manifests": unsupported},
                remediation=(
                    "Use native k1s manifests for k1s bundle export, or export Kubernetes/Helm "
                    "artifacts and hand convert before deploying to k1s."
                ),
            )
        manifest_out = out_dir / "manifests"
        shutil.copytree(stage_dir / "manifests", manifest_out)
        _write_json(out_dir / "bundle.json", _export_metadata(fmt, validation))
        _write_export_readme(out_dir, fmt=fmt)
    elif fmt == "k8s":
        _export_k8s(supervisor, validation["manifest_details"], out_dir, namespace=namespace)
        _write_json(out_dir / "bundle.json", _export_metadata(fmt, validation))
        _write_export_readme(out_dir, fmt=fmt)
    elif fmt == "helm":
        _export_helm(supervisor, validation["manifest_details"], out_dir, namespace=namespace)
    else:
        raise ValueError("format must be one of: k1s, k8s, helm")
    _write_json(out_dir / "images.json", {"images": validation.get("images", [])})
    files = [str(path) for path in sorted(out_dir.rglob("*")) if path.is_file()]
    return {"ok": True, "format": fmt, "output_dir": str(out_dir), "files": files}


def template_names() -> list[str]:
    return sorted(SUPPORTED_TEMPLATES)


def _stage_ref(state_dir: Path, *, project: str, name: str) -> StageRef:
    slug = project_slug(name)
    stage_dir = state_dir / "artifacts" / "staged" / slug
    return StageRef(
        project=project,
        name=slug,
        stage_dir=stage_dir,
        manifest_dir=stage_dir / "manifests",
    )


def _copy_source(source: Path, target: Path) -> list[Path]:
    if source.is_file():
        dest = target / source.name
        shutil.copy2(source, dest)
        return [dest]
    if not source.is_dir():
        raise FileNotFoundError(f"source not found: {source}")
    copied = []
    for path in sorted(source.glob("*.y*ml")):
        dest = target / path.name
        shutil.copy2(path, dest)
        copied.append(dest)
    return copied


def _write_template(target: Path, *, template: str, project: str) -> list[Path]:
    if template not in SUPPORTED_TEMPLATES:
        expected = sorted(SUPPORTED_TEMPLATES)
        raise ValueError(f"unknown template {template!r}; expected one of {expected}")
    apps = ["web"]
    if template in {"frontend-api", "frontend-api-store"}:
        apps = ["api", "frontend"]
    if template == "frontend-api-store":
        apps = ["store", "api", "frontend"]
    paths = []
    for app in apps:
        path = target / f"{app}.k1s.yaml"
        path.write_text(_template_manifest(app=app, project=project), encoding="utf-8")
        paths.append(path)
    return paths


def _template_manifest(*, app: str, project: str) -> str:
    port = 8080
    return f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: {app}
  namespace: {project_slug(project)}
  labels:
    workerbee.k1s.dev/project: {project_slug(project)}
spec:
  # TODO: replace image with a pushed registry image before remote k1s deploy.
  image: workerbee-{project_slug(project)}-{app}:dev
  imagePullPolicy: Never
  replicas: 1
  ports:
    - name: http
      containerPort: {port}
  service:
    port: {port}
    targetPort: {port}
  health:
    readiness:
      httpGet: {{ path: /healthz, port: {port} }}
      initialDelaySeconds: 1
      periodSeconds: 2
  resources:
    requests:
      cpu: 0.05
      memory: 64Mi
"""


def _bundle_metadata(
    stage: StageRef,
    *,
    manifests: list[Path],
    template: str,
    source: Path | None,
) -> dict[str, Any]:
    return {
        "api_version": "workerbee.bundle/v1",
        "kind": "WorkerBeeBundle",
        "workerbee_version": __version__,
        "project": stage.project,
        "name": stage.name,
        "template": template,
        "source": str(source) if source else None,
        "generated_at": time.time(),
        "manifests": [str(path.relative_to(stage.stage_dir)) for path in manifests],
    }


def _export_metadata(fmt: str, validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "api_version": "workerbee.bundle/v1",
        "kind": "WorkerBeeExport",
        "format": fmt,
        "workerbee_version": __version__,
        "generated_at": time.time(),
        "validation": validation,
    }


def _export_k8s(
    supervisor: WorkerBeeSupervisor,
    manifests: list[dict[str, Any]],
    out_dir: Path,
    *,
    namespace: str | None,
) -> None:
    for detail in manifests:
        manifest = str(detail["path"])
        stem = Path(manifest).stem.replace(".k1s", "")
        target = out_dir / f"{stem}.k8s.yaml"
        if detail["input_kind"] == KUBERNETES:
            shutil.copy2(manifest, target)
            continue
        args = ["export-k8s", "-f", manifest, "--emit-configs", "--emit-secrets", "--validate"]
        if namespace:
            args.extend(["--namespace", namespace])
        result = supervisor.run_ae_cli(args, timeout=90)
        target.write_text(result["stdout"], encoding="utf-8")


def _export_helm(
    supervisor: WorkerBeeSupervisor,
    manifests: list[dict[str, Any]],
    out_dir: Path,
    *,
    namespace: str | None,
) -> None:
    chart_name = out_dir.parent.parent.name
    templates_dir = out_dir / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "Chart.yaml").write_text(
        f"""apiVersion: v2
name: {chart_name}
description: Generated WorkerBee chart skeleton
type: application
version: 0.1.0
appVersion: "0.1.0"
""",
        encoding="utf-8",
    )
    values: dict[str, Any] = {
        "namespace": namespace or "default",
        "images": {},
        "replicas": {},
        "ingress": {"className": "", "hosts": {}},
    }
    for detail in manifests:
        manifest = str(detail["path"])
        docs = _load_yaml_documents(Path(manifest).read_text(encoding="utf-8"))
        app = _primary_app_name(detail) or Path(manifest).stem.replace(".k1s", "")
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if detail["input_kind"] == KUBERNETES and _kind(doc) not in K8S_WORKLOAD_KINDS:
                continue
            spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
            images = (
                _native_images(spec) if detail["input_kind"] == NATIVE_K1S else _k8s_images(doc)
            )
            if images:
                values["images"][app] = images[0]
            if detail["input_kind"] == NATIVE_K1S:
                values["replicas"][app] = int(spec.get("replicas") or 1)
            else:
                values["replicas"][app] = int(spec.get("replicas") or 1)
        if detail["input_kind"] == KUBERNETES:
            body = Path(manifest).read_text(encoding="utf-8")
        else:
            result = supervisor.run_ae_cli(
                ["export-k8s", "-f", manifest, "--emit-configs", "--emit-secrets", "--validate"],
                timeout=90,
            )
            body = result["stdout"]
        (templates_dir / f"{app}.yaml").write_text(
            "# TODO: parameterize this generated YAML before production use.\n" + body,
            encoding="utf-8",
        )
    (out_dir / "values.yaml").write_text(_dump_yaml(values), encoding="utf-8")
    _write_export_readme(out_dir, fmt="helm")


def _manifest_detail(path: Path, docs: list[dict[str, Any]]) -> dict[str, Any]:
    doc_inputs = []
    workloads = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        api = _api_version(doc)
        kind = _kind(doc)
        if api == "ae.dev/v1alpha1":
            doc_inputs.append(NATIVE_K1S)
            workloads.append(_workload_summary(doc, input_kind=NATIVE_K1S))
        elif api and kind:
            doc_inputs.append(KUBERNETES)
            if kind in K8S_WORKLOAD_KINDS:
                workloads.append(_workload_summary(doc, input_kind=KUBERNETES))
        else:
            doc_inputs.append("unknown")
    if not doc_inputs or "unknown" in doc_inputs:
        input_kind = "unknown"
    elif NATIVE_K1S in doc_inputs and KUBERNETES in doc_inputs:
        input_kind = "mixed"
    elif all(item == NATIVE_K1S for item in doc_inputs):
        input_kind = NATIVE_K1S
    elif all(item == KUBERNETES for item in doc_inputs):
        input_kind = KUBERNETES
    else:
        input_kind = "unknown"
    formats = [fmt for fmt in ("k1s", "k8s", "helm") if input_kind == NATIVE_K1S or fmt != "k1s"]
    if input_kind not in {NATIVE_K1S, KUBERNETES}:
        formats = []
    return {
        "path": str(path),
        "input_kind": input_kind,
        "document_count": len(docs),
        "workloads": workloads,
        "supported_export_formats": formats,
    }


def _validate_native_documents(path: Path, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(docs) == 1:
        return []
    return [
        {
            "level": "error",
            "code": "NATIVE_SINGLE_DOCUMENT_REQUIRED",
            "path": str(path),
            "message": "native k1s apply expects one ae.dev/v1alpha1 document per file",
        }
    ]


def _validate_k8s_documents(path: Path, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    unsupported = sorted({_kind(doc) for doc in docs if _kind(doc) not in K8S_SUPPORTED_KINDS})
    if unsupported:
        findings.append(
            {
                "level": "error",
                "code": "K8S_UNSUPPORTED_KIND",
                "path": str(path),
                "message": (
                    "WorkerBee v0.1 Kubernetes apply supports exactly one "
                    "Deployment/StatefulSet/DaemonSet/Job plus optional Service/Ingress"
                ),
                "kinds": unsupported,
            }
        )
    workloads = [doc for doc in docs if _kind(doc) in K8S_WORKLOAD_KINDS]
    if len(workloads) != 1:
        findings.append(
            {
                "level": "error",
                "code": "K8S_ONE_WORKLOAD_REQUIRED",
                "path": str(path),
                "message": (
                    "Kubernetes input must keep one workload and its matching Service/Ingress "
                    "documents in the same file"
                ),
                "workload_count": len(workloads),
            }
        )
    return findings


def _primary_app_name(detail: dict[str, Any]) -> str | None:
    workloads = detail.get("workloads")
    if not isinstance(workloads, list) or not workloads:
        return None
    first = workloads[0]
    if not isinstance(first, dict):
        return None
    name = first.get("name")
    return str(name) if name else None


def _workload_summary(doc: dict[str, Any], *, input_kind: str) -> dict[str, Any]:
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    namespace = str(metadata.get("namespace") or "default")
    name = str(metadata.get("name") or "app")
    return {
        "name": name,
        "namespace": namespace,
        "scope": f"{namespace}/{name}",
        "kind": _kind(doc),
        "input_kind": input_kind,
    }


def _api_version(doc: dict[str, Any]) -> str:
    return str(doc.get("apiVersion") or "")


def _kind(doc: dict[str, Any]) -> str:
    return str(doc.get("kind") or "")


def _native_images(spec: dict[str, Any]) -> list[str]:
    image = str(spec.get("image") or "")
    return [image] if image else []


def _k8s_images(doc: dict[str, Any]) -> list[str]:
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    pod_spec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
    images = []
    for container in pod_spec.get("containers") or []:
        if not isinstance(container, dict):
            continue
        image = str(container.get("image") or "")
        if image:
            images.append(image)
    return images


def _write_stage_readme(stage: StageRef) -> None:
    (stage.stage_dir / "README.md").write_text(
        f"""# WorkerBee Staged Bundle: {stage.name}

Edit files in `manifests/`, then run:

```bash
workerbee --project {stage.project} manifest validate --stage {stage.stage_dir}
workerbee --project {stage.project} manifest deploy-local --stage {stage.stage_dir}
workerbee --project {stage.project} bundle export --stage {stage.stage_dir} --format k1s
```
""",
        encoding="utf-8",
    )


def _write_export_readme(out_dir: Path, *, fmt: str) -> None:
    commands = {
        "k1s": (
            "ae --server <k1s-controller-url> --token <admin-token> "
            "apply -f manifests/<app>.k1s.yaml"
        ),
        "k8s": "kubectl apply -f .",
        "helm": "helm upgrade --install <release> . -n <namespace> --create-namespace",
    }
    (out_dir / "README.md").write_text(
        f"""# WorkerBee {fmt} Export

Review and hand edit these artifacts before production deployment.
Image references discovered during validation are recorded in `images.json`; retag
and push local `workerbee-*` images before remote deployment.

Suggested command:

```bash
{commands.get(fmt, '')}
```
""",
        encoding="utf-8",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _load_yaml_documents(text: str) -> list[dict[str, Any]]:
    if yaml is not None:
        return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]
    docs = []
    for raw in text.split("\n---"):
        docs.append(_minimal_manifest_parse(raw))
    return [doc for doc in docs if doc]


def _minimal_manifest_parse(text: str) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    section: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0 and stripped.endswith(":"):
            section = stripped[:-1]
            doc.setdefault(section, {})
            continue
        if indent == 0 and ":" in stripped:
            key, value = stripped.split(":", 1)
            doc[key] = value.strip()
            section = key if isinstance(doc.get(key), dict) else None
            continue
        if section in {"metadata", "spec"} and indent >= 2 and ":" in stripped:
            key, value = stripped.split(":", 1)
            doc.setdefault(section, {})[key] = value.strip().strip('"')
    return doc


def _dump_yaml(payload: dict[str, Any]) -> str:
    if yaml is not None:
        return yaml.safe_dump(payload, sort_keys=False)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
