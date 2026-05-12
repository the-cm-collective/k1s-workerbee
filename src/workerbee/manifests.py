"""Staged manifest and artifact bundle helpers."""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import blake2s
from ipaddress import ip_network
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - exercised when PyYAML is not installed
    yaml = None  # type: ignore[assignment]

from workerbee import __version__
from workerbee.contract import WorkerBeeError
from workerbee.ports import port_is_free
from workerbee.runtime_support import CONTAINERD_RUNTIME, containerd_network_subnet
from workerbee.secrets import file_is_sops_encrypted, plaintext_secrets_allowed
from workerbee.supervisor import WorkerBeeSupervisor, project_slug

SUPPORTED_TEMPLATES = {
    "stateless-web",
    "frontend-api",
    "frontend-api-store",
    "realtime-web-db",
}
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
        ingress_port = supervisor.ingress.https_port if supervisor.ingress else 19443
        service_ports = _realtime_service_ports(project) if template == "realtime-web-db" else None
        peer_hosts = _realtime_peer_hosts(supervisor) if template == "realtime-web-db" else None
        copied = _write_template(
            stage.manifest_dir,
            template=template,
            project=project,
            ingress_port=ingress_port,
            ingress_domain=supervisor.ingress.domain if supervisor.ingress else None,
            service_ports=service_ports,
            peer_hosts=peer_hosts,
        )
    bundle = _bundle_metadata(stage, manifests=copied, template=template, source=source)
    _write_json(stage.stage_dir / "bundle.json", bundle)
    _write_json(stage.stage_dir / "images.json", {"images": []})
    _write_stage_readme(stage)
    validation = validate_stage(stage.stage_dir, cwd=supervisor.cwd)
    _write_json(stage.stage_dir / "images.json", {"images": validation.get("images", [])})
    return {
        "project": project,
        "stage_dir": str(stage.stage_dir),
        "manifests": [str(path) for path in copied],
        "bundle": str(stage.stage_dir / "bundle.json"),
        "validation": validation,
    }


def validate_stage(stage_dir: Path, *, cwd: Path | None = None) -> dict[str, Any]:
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
            if cwd is not None:
                findings.extend(_validate_native_secret_refs(path, docs, cwd=cwd))
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


def resolve_stage_dir(supervisor: WorkerBeeSupervisor, stage: Path | str) -> Path:
    raw = Path(stage).expanduser()
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(raw.resolve())
        if len(raw.parts) == 1:
            stage_ref = _stage_ref(
                supervisor.state_dir,
                project=supervisor.project,
                name=raw.name,
            )
            candidates.append(stage_ref.stage_dir)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    available = []
    staged_root = supervisor.state_dir / "artifacts" / "staged"
    if staged_root.is_dir():
        available = sorted(path.name for path in staged_root.iterdir() if path.is_dir())
    raise WorkerBeeError(
        code="STAGE_NOT_FOUND",
        message=f"staged manifest bundle not found: {stage}",
        details={
            "stage": str(stage),
            "checked": [str(item) for item in candidates],
            "available_stages": available,
        },
        remediation=(
            "Pass an absolute stage_dir from manifest_prepare or a named stage under "
            "the project artifacts/staged directory."
        ),
    )


def deploy_local_stage(
    *,
    supervisor: WorkerBeeSupervisor,
    stage_dir: Path,
    namespace: str | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    validation = validate_stage(stage_dir, cwd=supervisor.cwd)
    if not validation["ok"]:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="staged manifests are not valid",
            details=validation,
            remediation="Fix staged files and run validation again.",
        )
    enforce_stage_secret_policy(stage_dir, validation=validation, cwd=supervisor.cwd)
    results = []
    native_manifests: list[Path] = []
    for detail in validation["manifest_details"]:
        manifest = Path(str(detail["path"]))
        if detail["input_kind"] == KUBERNETES:
            results.append(
                supervisor.deploy_k8s_manifest(manifest, namespace=namespace, timeout=timeout)
            )
        else:
            native_manifests.append(manifest)
            results.append(
                supervisor.deploy_manifest(manifest, namespace=namespace, timeout=timeout)
            )
    alias_refresh = _refresh_containerd_service_aliases(
        supervisor=supervisor,
        validation=validation,
        native_manifests=native_manifests,
        namespace=namespace,
        timeout=timeout,
    )
    if alias_refresh.get("enabled") and alias_refresh.get("ok") is False:
        raise WorkerBeeError(
            code="CONTAINERD_SERVICE_ALIAS_NOT_READY",
            message=(
                "direct-containerd service aliases were not ready; "
                "staged manifests were not reapplied"
            ),
            details={
                "validation": validation,
                "apply": results,
                "alias_refresh": alias_refresh,
            },
            remediation=(
                "Inspect service workload status/logs, fix readiness failures, then rerun deploy. "
                "WorkerBee did not perform the alias-refresh reapply because one or more service "
                "workloads did not become ready."
            ),
            retryable=True,
        )
    return {"ok": True, "validation": validation, "apply": results, "alias_refresh": alias_refresh}


def _refresh_containerd_service_aliases(
    *,
    supervisor: WorkerBeeSupervisor,
    validation: dict[str, Any],
    native_manifests: list[Path],
    namespace: str | None,
    timeout: int,
) -> dict[str, Any]:
    info = supervisor.load_stack()
    runtime = getattr(info, "runtime", None) if info is not None else None
    if runtime != CONTAINERD_RUNTIME:
        return {
            "ok": True,
            "enabled": False,
            "reason": "runtime is not direct containerd",
            "runtime": runtime,
        }
    if not native_manifests:
        return {
            "ok": True,
            "enabled": False,
            "reason": "no native k1s manifests",
            "runtime": runtime,
        }
    service_workloads = _native_referenced_service_workloads(validation, namespace=namespace)
    if not service_workloads:
        return {
            "ok": True,
            "enabled": False,
            "reason": "no referenced native k1s service workloads",
            "runtime": runtime,
        }
    if info is None:
        info = supervisor.start()
    wait = _wait_for_service_workloads(
        supervisor=supervisor,
        info=info,
        workloads=service_workloads,
        timeout_seconds=max(3.0, min(20.0, float(timeout) * 0.25)),
    )
    if not wait["ready"]:
        return {
            "ok": False,
            "enabled": True,
            "runtime": runtime,
            "service_workloads": service_workloads,
            "ready": False,
            "waited_seconds": wait["waited_seconds"],
            "statuses": wait.get("statuses", []),
            "reapplied": 0,
            "apply": [],
        }
    reapplies = [
        supervisor.deploy_manifest(manifest, namespace=namespace, timeout=timeout)
        for manifest in native_manifests
    ]
    return {
        "ok": True,
        "enabled": True,
        "runtime": runtime,
        "service_workloads": service_workloads,
        "ready": wait["ready"],
        "waited_seconds": wait["waited_seconds"],
        "reapplied": len(reapplies),
        "apply": reapplies,
    }


def _native_referenced_service_workloads(
    validation: dict[str, Any],
    *,
    namespace: str | None,
) -> list[dict[str, str]]:
    providers: list[dict[str, str]] = []
    provider_refs: dict[tuple[str, str], set[str]] = {}
    referenced_hosts_by_ns: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for detail in validation.get("manifest_details", []):
        if not isinstance(detail, dict) or detail.get("input_kind") != NATIVE_K1S:
            continue
        path = Path(str(detail.get("path") or ""))
        if not path.is_file():
            continue
        docs = _load_yaml_documents(path.read_text(encoding="utf-8"))
        for doc in docs:
            if not isinstance(doc, dict) or _api_version(doc) != "ae.dev/v1alpha1":
                continue
            metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
            ns = str(namespace or metadata.get("namespace") or "default")
            name = str(metadata.get("name") or path.stem)
            manifest_refs = _native_manifest_reference_hosts(doc)
            referenced_hosts_by_ns.setdefault(ns, set()).update(manifest_refs)
            if not spec.get("service"):
                continue
            key = (ns, name)
            provider_refs[key] = manifest_refs
            if key in seen:
                continue
            seen.add(key)
            providers.append({"namespace": ns, "name": name})
    referenced_providers = [
        provider
        for provider in providers
        if _service_reference_matches(
            referenced_hosts_by_ns.get(provider["namespace"], set()),
            provider["namespace"],
            provider["name"],
        )
    ]
    provider_keys = {(provider["namespace"], provider["name"]) for provider in providers}
    root_providers = [
        provider
        for provider in referenced_providers
        if not _provider_depends_on_provider(
            provider_refs.get((provider["namespace"], provider["name"]), set()),
            provider_keys=provider_keys,
            current=(provider["namespace"], provider["name"]),
        )
    ]
    return root_providers or referenced_providers


def _provider_depends_on_provider(
    referenced_hosts: set[str],
    *,
    provider_keys: set[tuple[str, str]],
    current: tuple[str, str],
) -> bool:
    for service_ns, service_name in provider_keys:
        if (service_ns, service_name) == current:
            continue
        if _service_reference_matches(referenced_hosts, service_ns, service_name):
            return True
    return False


def _native_service_workloads(
    validation: dict[str, Any],
    *,
    namespace: str | None,
) -> list[dict[str, str]]:
    workloads: list[dict[str, str]] = []
    for detail in validation.get("manifest_details", []):
        if not isinstance(detail, dict) or detail.get("input_kind") != NATIVE_K1S:
            continue
        path = Path(str(detail.get("path") or ""))
        if not path.is_file():
            continue
        docs = _load_yaml_documents(path.read_text(encoding="utf-8"))
        for doc in docs:
            if not isinstance(doc, dict) or _api_version(doc) != "ae.dev/v1alpha1":
                continue
            spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
            if not spec.get("service"):
                continue
            metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            name = str(metadata.get("name") or path.stem)
            ns = str(namespace or metadata.get("namespace") or "default")
            workloads.append({"namespace": ns, "name": name})
    return workloads


def _native_manifest_reference_hosts(doc: dict[str, Any]) -> set[str]:
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    hosts: set[str] = set()
    for text in _native_manifest_reference_texts(spec):
        hosts.update(_reference_hosts_from_text(text))
    return hosts


def _native_manifest_reference_texts(spec: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    texts.extend(_as_texts(spec.get("command")))
    texts.extend(_as_texts(spec.get("args")))
    texts.extend(_env_reference_texts(spec.get("env")))
    containers = []
    for key in ("containers", "initContainers", "init_containers"):
        value = spec.get(key)
        if isinstance(value, list):
            containers.extend(value)
    for container in containers:
        if not isinstance(container, dict):
            continue
        texts.extend(_as_texts(container.get("command")))
        texts.extend(_as_texts(container.get("args")))
        texts.extend(_env_reference_texts(container.get("env")))
    return [text for text in texts if text]


def _env_reference_texts(env: Any) -> list[str]:
    if not isinstance(env, list):
        return []
    texts: list[str] = []
    for item in env:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if value is not None:
            texts.append(str(value))
    return texts


def _as_texts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _service_reference_matches(
    referenced_hosts: set[str],
    service_ns: str,
    service_name: str,
) -> bool:
    service_ns = str(service_ns or "").strip().lower()
    service_name = str(service_name or "").strip().lower()
    if not service_ns or not service_name:
        return False
    candidates = {
        service_name,
        f"{service_name}.{service_ns}",
        f"{service_name}.{service_ns}.svc",
        f"{service_name}.{service_ns}.svc.cluster.local",
    }
    return bool(referenced_hosts & candidates)


def _reference_hosts_from_text(text: str) -> set[str]:
    raw = str(text or "").strip()
    if not raw:
        return set()
    hosts: set[str] = set()
    scrubbed_parts: list[str] = []
    cursor = 0
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+", raw):
        scrubbed_parts.append(raw[cursor : match.start()])
        scrubbed_parts.append(" ")
        cursor = match.end()
        try:
            from urllib.parse import urlparse

            host = str(urlparse(match.group(0)).hostname or "").strip().lower().rstrip(".")
        except Exception:
            host = ""
        if host:
            hosts.add(host)
    scrubbed_parts.append(raw[cursor:])
    scrubbed = "".join(scrubbed_parts)
    host_port = re.compile(
        r"(?<![A-Za-z0-9_.-])"
        r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)"
        r":\d{1,5}"
        r"(?![A-Za-z0-9_.-])"
    )
    for match in host_port.finditer(scrubbed):
        host = str(match.group(1) or "").strip().lower().rstrip(".")
        if host:
            hosts.add(host)
    return hosts


def _wait_for_service_workloads(
    *,
    supervisor: WorkerBeeSupervisor,
    info: Any,
    workloads: list[dict[str, str]],
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: list[dict[str, Any]] = []
    while True:
        last = [
            _service_workload_status(supervisor=supervisor, info=info, workload=workload)
            for workload in workloads
        ]
        if last and all(item.get("ready") for item in last):
            waited = max(0.0, timeout_seconds - (deadline - time.monotonic()))
            return {
                "ready": True,
                "waited_seconds": round(waited, 3),
                "statuses": last,
            }
        if time.monotonic() >= deadline:
            return {"ready": False, "waited_seconds": round(timeout_seconds, 3), "statuses": last}
        time.sleep(0.5)


def _service_workload_status(
    *,
    supervisor: WorkerBeeSupervisor,
    info: Any,
    workload: dict[str, str],
) -> dict[str, Any]:
    name = workload["name"]
    namespace = workload["namespace"]
    try:
        result = supervisor.run_ae(
            [
                "--server",
                info.controller_url,
                "--token",
                info.read_token,
                "status",
                name,
                "-n",
                namespace,
                "--json",
            ],
            info=info,
            timeout=10,
        )
        payload = json.loads(str(result.get("stdout") or "{}"))
        desired = max(1, int(payload.get("desired_replicas") or 1))
        ready = int(payload.get("ready_replicas") or 0)
        return {
            "namespace": namespace,
            "name": name,
            "ready": ready >= desired,
            "desired": desired,
            "ready_replicas": ready,
        }
    except Exception as exc:  # noqa: BLE001
        return {"namespace": namespace, "name": name, "ready": False, "error": str(exc)}


def deploy_profile_stage(
    *,
    supervisor: WorkerBeeSupervisor,
    profile_runner: Any,
    stage_dir: Path,
    profile: str | None = None,
    namespace: str | None = None,
    timeout: int = 180,
    sync_ingress: Callable[[], dict[str, Any]] | None = None,
    reset_existing: bool = False,
) -> dict[str, Any]:
    validation = validate_stage(stage_dir, cwd=supervisor.cwd)
    if not validation["ok"]:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="staged manifests are not valid",
            details=validation,
            remediation="Fix staged files and run validation again.",
        )
    enforce_stage_secret_policy(stage_dir, validation=validation, cwd=supervisor.cwd)
    connection = profile_runner.connection(profile=profile, timeout=float(timeout))
    env_overrides = {
        "AE_APISHIM_CA_BUNDLE": str(connection["ca_bundle"]),
        "SSL_CERT_FILE": str(connection["ca_bundle"]),
        "REQUESTS_CA_BUNDLE": str(connection["ca_bundle"]),
    }
    ingress_sync = sync_ingress() if sync_ingress else None
    reset = (
        _delete_profile_stage_apps(
            supervisor=supervisor,
            connection=connection,
            validation=validation,
            namespace=namespace,
            timeout=timeout,
            env_overrides=env_overrides,
        )
        if reset_existing
        else []
    )
    results = []
    for detail in validation["manifest_details"]:
        manifest = Path(str(detail["path"]))
        if detail["input_kind"] == KUBERNETES:
            results.append(
                supervisor.deploy_remote_k8s_manifest(
                    manifest,
                    server=str(connection["server"]),
                    token=str(connection["admin_token"]),
                    namespace=namespace,
                    timeout=timeout,
                    env_overrides=env_overrides,
                )
            )
        else:
            results.append(
                supervisor.deploy_remote_manifest(
                    manifest,
                    server=str(connection["server"]),
                    token=str(connection["admin_token"]),
                    namespace=namespace,
                    timeout=timeout,
                    env_overrides=env_overrides,
                )
            )
    return {
        "ok": True,
        "target": "profile",
        "project": supervisor.project,
        "profile": connection["profile"],
        "server": connection["server"],
        "api_server": connection["api_server"],
        "public_server": connection.get("public_server"),
        "public_api_server": connection.get("public_api_server"),
        "urls": connection["urls"],
        "ca_bundle": connection["ca_bundle"],
        "ingress_sync": ingress_sync,
        "reset": reset,
        "validation": validation,
        "apply": results,
    }


def _delete_profile_stage_apps(
    *,
    supervisor: WorkerBeeSupervisor,
    connection: dict[str, Any],
    validation: dict[str, Any],
    namespace: str | None,
    timeout: int,
    env_overrides: dict[str, str],
) -> list[dict[str, Any]]:
    deleted: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str]] = set()
    for detail in validation.get("manifest_details") or []:
        if not isinstance(detail, dict):
            continue
        for workload in detail.get("workloads") or []:
            if not isinstance(workload, dict):
                continue
            name = str(workload.get("name") or "").strip()
            if not name:
                continue
            target_namespace = namespace or str(workload.get("namespace") or "").strip() or None
            key = (target_namespace, name)
            if key in seen:
                continue
            seen.add(key)
            args = [
                "--server",
                str(connection["server"]),
                "--token",
                str(connection["admin_token"]),
                "delete",
                name,
                "--purge",
            ]
            if target_namespace:
                args.extend(["-n", target_namespace])
            try:
                result = supervisor.run_ae_cli(
                    args,
                    timeout=timeout,
                    env_overrides=env_overrides,
                )
                deleted.append(
                    {
                        "ok": True,
                        "name": name,
                        "namespace": target_namespace,
                        "delete": result,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - reset is best-effort
                deleted.append(
                    {
                        "ok": False,
                        "name": name,
                        "namespace": target_namespace,
                        "error": str(exc),
                    }
                )
    return deleted


def deploy_remote_k1s_stage(
    *,
    supervisor: WorkerBeeSupervisor,
    stage_dir: Path,
    server: str,
    token: str,
    namespace: str | None = None,
    timeout: int = 180,
    allow_remote_secretrefs: bool = False,
) -> dict[str, Any]:
    if not server:
        raise ValueError("remote k1s server URL is required")
    if not token:
        raise WorkerBeeError(
            code="AUTH_REQUIRED",
            message="remote k1s admin token is required",
            remediation="Provide a controller admin token scoped to every manifest namespace/name.",
        )
    validation = validate_stage(stage_dir, cwd=supervisor.cwd)
    if not validation["ok"]:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="staged manifests are not valid",
            details=validation,
        )
    enforce_stage_secret_policy(
        stage_dir,
        validation=validation,
        cwd=supervisor.cwd,
        remote=True,
        allow_remote_secretrefs=allow_remote_secretrefs,
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
    validation = validate_stage(stage_dir, cwd=supervisor.cwd)
    if not validation["manifests"]:
        raise RuntimeError("no staged manifests to export")
    if not validation["ok"]:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="staged manifests are not valid",
            details=validation,
            remediation="Fix staged files and run validation again.",
        )
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
        _export_k1s_native(
            validation["manifest_details"],
            out_dir,
            cwd=supervisor.cwd,
        )
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


def _write_template(
    target: Path,
    *,
    template: str,
    project: str,
    ingress_port: int = 19443,
    ingress_domain: str | None = None,
    service_ports: dict[str, int] | None = None,
    peer_hosts: list[str] | None = None,
) -> list[Path]:
    if template not in SUPPORTED_TEMPLATES:
        expected = sorted(SUPPORTED_TEMPLATES)
        raise ValueError(f"unknown template {template!r}; expected one of {expected}")
    if template == "realtime-web-db":
        paths = []
        for app in ("db", "backend", "frontend"):
            path = target / f"{app}.k1s.yaml"
            path.write_text(
                _realtime_template_manifest(
                    app=app,
                    project=project,
                    ingress_port=ingress_port,
                    ingress_domain=ingress_domain,
                    service_ports=service_ports or _realtime_service_ports(project),
                    peer_hosts=peer_hosts or ["host.containers.internal", "host.docker.internal"],
                ),
                encoding="utf-8",
            )
            paths.append(path)
        return paths
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


def _realtime_template_manifest(
    *,
    app: str,
    project: str,
    ingress_port: int,
    ingress_domain: str | None,
    service_ports: dict[str, int],
    peer_hosts: list[str],
) -> str:
    project_name = project_slug(project)
    domain = ingress_domain or f"{project_name}.workerbee.localhost"
    db_urls = ",".join(_peer_urls("db", project_name, service_ports, peer_hosts))
    backend_urls = ",".join(_peer_urls("backend", project_name, service_ports, peer_hosts))
    if app == "db":
        return f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: db
  namespace: {project_name}
  labels:
    workerbee.k1s.dev/project: {project_name}
spec:
  image: workerbee-{project_name}-realtime-db:dev
  imagePullPolicy: Never
  replicas: 1
  env:
    - name: SEED_KEY
      value: boot
    - name: SEED_VALUE
      value: workerbee
  ports:
    - name: http
      containerPort: 8080
  service:
    port: {service_ports["db"]}
    targetPort: 8080
  health:
    readiness:
      httpGet: {{ path: /healthz, port: 8080 }}
      initialDelaySeconds: 1
      periodSeconds: 2
  storage:
    - name: data
      mountPath: /data
      retention: Delete
  resources:
    requests:
      cpu: 0.05
      memory: 64Mi
"""
    if app == "backend":
        return f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: backend
  namespace: {project_name}
  labels:
    workerbee.k1s.dev/project: {project_name}
spec:
  image: workerbee-{project_name}-realtime-backend:dev
  imagePullPolicy: Never
  replicas: 1
  env:
    - name: DB_URLS
      value: "{db_urls}"
  ports:
    - name: http
      containerPort: 8080
  service:
    port: {service_ports["backend"]}
    targetPort: 8080
  health:
    readiness:
      httpGet: {{ path: /healthz, port: 8080 }}
      initialDelaySeconds: 1
      periodSeconds: 2
  ingress:
    host: api.{domain}
    path: /
  resources:
    requests:
      cpu: 0.05
      memory: 96Mi
"""
    if app == "frontend":
        return f"""apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: frontend
  namespace: {project_name}
  labels:
    workerbee.k1s.dev/project: {project_name}
spec:
  image: workerbee-{project_name}-realtime-frontend:dev
  imagePullPolicy: Never
  replicas: 1
  env:
    - name: BACKEND_URLS
      value: "{backend_urls}"
    - name: PUBLIC_API_BASE
      value: "https://api.{domain}:{ingress_port}"
    - name: PUBLIC_WS_URL
      value: "wss://api.{domain}:{ingress_port}/ws"
  ports:
    - name: http
      containerPort: 8080
  service:
    port: {service_ports["frontend"]}
    targetPort: 8080
  health:
    readiness:
      httpGet: {{ path: /healthz, port: 8080 }}
      initialDelaySeconds: 1
      periodSeconds: 2
  ingress:
    host: app.{domain}
    path: /
  resources:
    requests:
      cpu: 0.05
      memory: 64Mi
"""
    raise ValueError(f"unknown realtime app {app!r}")


def _realtime_service_ports(project: str) -> dict[str, int]:
    start = 21000
    end = 22999
    span = 3
    digest = blake2s(project_slug(project).encode("utf-8"), digest_size=2).digest()
    slots = max(1, (end - start + 1) // span)
    preferred = start + (int.from_bytes(digest, "big") % slots) * span
    candidates = list(range(preferred, end - span + 2, span))
    candidates.extend(range(start, preferred, span))
    for base in candidates:
        ports = [base, base + 1, base + 2]
        if all(port_is_free(port, host="0.0.0.0") for port in ports):  # noqa: S104
            return {"db": ports[0], "backend": ports[1], "frontend": ports[2]}
    raise RuntimeError(f"no free realtime service port range found in {start}-{end}")


def _realtime_peer_hosts(supervisor: WorkerBeeSupervisor) -> list[str]:
    hosts: list[str] = []
    if str(supervisor.runtime_requested).lower() == CONTAINERD_RUNTIME:
        state_root = supervisor.state_dir.parent.parent
        hosts.append(_subnet_gateway(containerd_network_subnet(state_root, supervisor.project)))
    hosts.extend(["host.containers.internal", "host.docker.internal", "127.0.0.1"])
    deduped: list[str] = []
    for host in hosts:
        if host and host not in deduped:
            deduped.append(host)
    return deduped


def _subnet_gateway(subnet: str) -> str:
    network = ip_network(subnet, strict=False)
    return str(next(network.hosts()))


def _peer_urls(
    app: str,
    project_name: str,
    service_ports: dict[str, int],
    peer_hosts: list[str],
) -> list[str]:
    port = service_ports[app]
    urls = [f"http://{host}:{port}" for host in peer_hosts]
    urls.extend(
        [
            f"http://ae-{project_name}--{app}:8080",
            f"http://app-{project_name}--{app}:8080",
            f"http://{app}:8080",
        ]
    )
    return urls


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


def _export_k1s_native(
    manifests: list[dict[str, Any]],
    out_dir: Path,
    *,
    cwd: Path,
) -> None:
    manifest_out = out_dir / "manifests"
    secret_out = out_dir / "secrets"
    manifest_out.mkdir(parents=True, exist_ok=True)
    copied_secrets: dict[Path, str] = {}
    for detail in manifests:
        source = Path(str(detail["path"]))
        docs = _load_yaml_documents(source.read_text(encoding="utf-8"))
        changed = False
        for doc in docs:
            if not isinstance(doc, dict) or _api_version(doc) != "ae.dev/v1alpha1":
                continue
            for ref in _native_secret_refs(doc):
                raw_path = str(ref.get("path") or "").strip()
                if not raw_path:
                    continue
                resolved = _resolve_secret_ref_path(raw_path, manifest=source, cwd=cwd)
                if not resolved.is_file():
                    continue
                rel = copied_secrets.get(resolved)
                if rel is None:
                    secret_out.mkdir(parents=True, exist_ok=True)
                    dest = _unique_secret_export_path(secret_out, resolved)
                    shutil.copy2(resolved, dest)
                    rel = f"secrets/{dest.name}"
                    copied_secrets[resolved] = rel
                ref["path"] = rel
                changed = True
        target = manifest_out / source.name
        if changed and docs:
            body = "\n---\n".join(_dump_yaml(doc).rstrip() for doc in docs) + "\n"
            target.write_text(body, encoding="utf-8")
        else:
            shutil.copy2(source, target)


def _unique_secret_export_path(secret_out: Path, source: Path) -> Path:
    target = secret_out / source.name
    if not target.exists():
        return target
    digest = blake2s(str(source).encode("utf-8"), digest_size=4).hexdigest()
    return secret_out / f"{source.stem}-{digest}{source.suffix}"


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
        args = ["export-k8s", "-f", manifest, "--emit-configs", "--validate"]
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
                ["export-k8s", "-f", manifest, "--emit-configs", "--validate"],
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


def enforce_stage_secret_policy(
    stage_dir: Path,
    *,
    validation: dict[str, Any] | None = None,
    cwd: Path | None = None,
    remote: bool = False,
    allow_remote_secretrefs: bool = False,
) -> None:
    root = stage_dir.expanduser().resolve()
    findings = _stage_secret_policy_findings(
        validation=validation or validate_stage(root, cwd=cwd),
        cwd=cwd or root,
        remote=remote,
        allow_remote_secretrefs=allow_remote_secretrefs,
    )
    errors = [item for item in findings if item.get("level") == "error"]
    if not errors:
        return
    code = str(errors[0].get("code") or "SECRET_POLICY_FAILED")
    if len({str(item.get("code") or "") for item in errors}) > 1:
        code = "SECRET_POLICY_FAILED"
    raise WorkerBeeError(
        code=code,
        message="staged secret handling does not satisfy WorkerBee secure defaults",
        details={"stage_dir": str(root), "findings": errors},
        remediation=(
            "Use SOPS-encrypted secretRefs or set WORKERBEE_ALLOW_PLAINTEXT_SECRETS=1 "
            "for an insecure local-only run."
        ),
    )


def _stage_secret_policy_findings(
    *,
    validation: dict[str, Any],
    cwd: Path,
    remote: bool,
    allow_remote_secretrefs: bool,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for detail in validation.get("manifest_details", []):
        if not isinstance(detail, dict) or detail.get("input_kind") != NATIVE_K1S:
            continue
        path = Path(str(detail.get("path") or ""))
        if not path.is_file():
            continue
        docs = _load_yaml_documents(path.read_text(encoding="utf-8"))
        findings.extend(_validate_native_secret_refs(path, docs, cwd=cwd))
        if remote and not allow_remote_secretrefs:
            for doc in docs:
                if not isinstance(doc, dict) or _api_version(doc) != "ae.dev/v1alpha1":
                    continue
                for ref in _native_secret_refs(doc):
                    findings.append(
                        {
                            "level": "error",
                            "code": "REMOTE_SECRET_HANDOFF_UNSAFE",
                            "path": str(path),
                            "secret_ref": str(ref.get("name") or ""),
                            "secret_path": str(ref.get("path") or ""),
                            "message": (
                                "remote k1s deploy refuses secretRefs by default because "
                                "secret paths are resolved on the remote controller."
                            ),
                        }
                    )
    return findings


def _validate_native_secret_refs(
    path: Path,
    docs: list[dict[str, Any]],
    *,
    cwd: Path,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for doc in docs:
        if not isinstance(doc, dict) or _api_version(doc) != "ae.dev/v1alpha1":
            continue
        for ref in _native_secret_refs(doc):
            name = str(ref.get("name") or "")
            raw_path = str(ref.get("path") or "").strip()
            if not raw_path:
                findings.append(
                    {
                        "level": "error",
                        "code": "SECRET_FILE_NOT_FOUND",
                        "path": str(path),
                        "secret_ref": name,
                        "message": "secretRef is missing a path.",
                    }
                )
                continue
            secret_path = _resolve_secret_ref_path(raw_path, manifest=path, cwd=cwd)
            if not secret_path.is_file():
                findings.append(
                    {
                        "level": "error",
                        "code": "SECRET_FILE_NOT_FOUND",
                        "path": str(path),
                        "secret_ref": name,
                        "secret_path": raw_path,
                        "resolved_secret_path": str(secret_path),
                        "message": "secretRef path does not exist.",
                    }
                )
                continue
            if plaintext_secrets_allowed() or file_is_sops_encrypted(secret_path):
                continue
            findings.append(
                {
                    "level": "error",
                    "code": "PLAINTEXT_SECRET_REF",
                    "path": str(path),
                    "secret_ref": name,
                    "secret_path": str(secret_path),
                    "message": (
                        "secretRef points to a plaintext file while secure defaults "
                        "are enabled."
                    ),
                }
            )
    return findings


def _native_secret_refs(doc: dict[str, Any]) -> list[dict[str, Any]]:
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    refs = spec.get("secretRefs") or spec.get("secret_refs") or []
    return [item for item in refs if isinstance(item, dict)] if isinstance(refs, list) else []


def _resolve_secret_ref_path(value: str, *, manifest: Path, cwd: Path) -> Path:
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    candidates = [
        (cwd / raw).resolve(),
        (manifest.parent / raw).resolve(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


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
WorkerBee keeps generated secret material SOPS-encrypted by default. Kubernetes
and Helm exports reference Secret names but do not emit Secret values; create
environment-specific Secret objects before applying those exports.

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
