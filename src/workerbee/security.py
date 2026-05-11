"""Advisory security assessment for WorkerBee staged artifacts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from workerbee.manifests import (
    K8S_WORKLOAD_KINDS,
    KUBERNETES,
    NATIVE_K1S,
    _kind,
    _load_yaml_documents,
    validate_stage,
)
from workerbee.supervisor import WorkerBeeSupervisor, project_slug

DEFAULT_SECURITY_CHECKS = ("manifest", "runtime", "headers", "export")
SUPPORTED_SECURITY_CHECKS = frozenset(DEFAULT_SECURITY_CHECKS)

_SEVERITY_WEIGHTS = {
    "critical": 30,
    "high": 20,
    "medium": 10,
    "low": 4,
    "info": 1,
}
_SENSITIVE_NAME_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "private_key",
    "client_secret",
    "api_key",
    "apikey",
    "access_key",
)
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_RUNTIME_DOC_PATHS = ("/openapi.json", "/swagger.json", "/docs")
_REQUIRED_SECURITY_HEADERS = {
    "content-security-policy": "Content-Security-Policy",
    "x-content-type-options": "X-Content-Type-Options",
    "referrer-policy": "Referrer-Policy",
}
_REFERENCE_URLS = {
    "OWASP Web Top 10 2021 A05": "https://owasp.org/Top10/A05_2021-Security_Misconfiguration/",
    "OWASP Web Top 10 2021 A02": "https://owasp.org/Top10/A02_2021-Cryptographic_Failures/",
    "OWASP API Security Top 10 2023 API8": "https://owasp.org/API-Security/editions/2023/en/0xa8-security-misconfiguration/",
    "OWASP API Security Top 10 2023 API4": "https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/",
    "OWASP Kubernetes Top 10": "https://owasp.org/www-project-kubernetes-top-ten/",
    "OWASP CI/CD Security Risks": "https://owasp.org/www-project-top-10-ci-cd-security-risks/",
}

ProbeCallback = Callable[..., dict[str, Any]]


def assess_stage_security(
    *,
    supervisor: WorkerBeeSupervisor,
    stage_dir: Path,
    namespace: str | None = None,
    target: str = "workerbee",
    checks: list[str] | tuple[str, ...] | None = None,
    runtime_probe: ProbeCallback | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Return advisory security findings for a staged WorkerBee bundle."""
    normalized_checks = _normalize_checks(checks)
    root = stage_dir.expanduser().resolve()
    validation = validate_stage(root, cwd=supervisor.cwd)
    findings: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    if "manifest" in normalized_checks:
        _assess_manifest_stage(
            validation=validation,
            namespace=namespace,
            findings=findings,
            evidence=evidence,
        )
    if "export" in normalized_checks:
        _assess_existing_exports(
            stage_dir=root,
            namespace=namespace,
            findings=findings,
            evidence=evidence,
        )
    if "runtime" in normalized_checks or "headers" in normalized_checks:
        _assess_runtime_surface(
            supervisor=supervisor,
            validation=validation,
            checks=normalized_checks,
            runtime_probe=runtime_probe,
            timeout=timeout,
            findings=findings,
            evidence=evidence,
        )

    _assign_finding_ids(findings)
    summary = _finding_summary(findings)
    return {
        "ok": True,
        "mode": "advisory",
        "passed": summary["critical"] == 0 and summary["high"] == 0,
        "project": project_slug(supervisor.project),
        "target": target,
        "stage_dir": str(root),
        "checks": list(normalized_checks),
        "summary": summary,
        "risk_score": _risk_score(findings),
        "findings": findings,
        "evidence": evidence,
        "suggested_patches": _suggested_patches(findings),
        "suggested_policies": _suggested_policies(findings),
        "validation": {
            "ok": validation.get("ok"),
            "input_kinds": validation.get("input_kinds", []),
            "images": validation.get("images", []),
            "findings": validation.get("findings", []),
        },
    }


def _normalize_checks(checks: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    values = tuple(checks or DEFAULT_SECURITY_CHECKS)
    normalized = tuple(dict.fromkeys(str(item).strip().lower() for item in values if str(item)))
    unknown = sorted(set(normalized) - SUPPORTED_SECURITY_CHECKS)
    if unknown:
        expected = ", ".join(DEFAULT_SECURITY_CHECKS)
        raise ValueError(f"security checks must be one of: {expected}; unknown: {unknown}")
    return normalized or DEFAULT_SECURITY_CHECKS


def _assess_manifest_stage(
    *,
    validation: dict[str, Any],
    namespace: str | None,
    findings: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> None:
    for item in validation.get("findings", []):
        if not isinstance(item, dict):
            continue
        if item.get("level") == "error":
            validation_code = str(item.get("code") or "")
            if validation_code in {
                "PLAINTEXT_SECRET_REF",
                "SECRET_FILE_NOT_FOUND",
                "REMOTE_SECRET_HANDOFF_UNSAFE",
            }:
                findings.append(
                    _finding(
                        code=validation_code,
                        severity="high",
                        message=str(item.get("message") or "secret handling validation failed"),
                        path=_str_or_none(item.get("path")),
                        evidence={
                            "secret_ref": item.get("secret_ref"),
                            "secret_path": "***" if item.get("secret_path") else None,
                        },
                        refs=("OWASP CI/CD Security Risks", "OWASP Kubernetes Top 10"),
                        remediation=(
                            "Use SOPS-encrypted secretRefs or an explicitly configured "
                            "environment secret source."
                        ),
                    )
                )
                continue
            findings.append(
                _finding(
                    code="MANIFEST_VALIDATION_ERROR",
                    severity="high",
                    message=str(item.get("message") or "staged manifest validation failed"),
                    path=_str_or_none(item.get("path")),
                    evidence={"workerbee_validation_code": item.get("code")},
                    refs=("OWASP Kubernetes Top 10",),
                    remediation="Fix WorkerBee manifest validation errors before deploy/export.",
                )
            )
    for detail in validation.get("manifest_details", []):
        if not isinstance(detail, dict):
            continue
        input_kind = str(detail.get("input_kind") or "")
        if input_kind not in {NATIVE_K1S, KUBERNETES}:
            continue
        path = Path(str(detail.get("path") or ""))
        if not path.is_file():
            continue
        docs = _load_yaml_documents(path.read_text(encoding="utf-8"))
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if input_kind == NATIVE_K1S:
                if str(doc.get("apiVersion") or "") == "ae.dev/v1alpha1":
                    _assess_native_doc(doc, path=path, namespace=namespace, findings=findings)
            elif _kind(doc) in K8S_WORKLOAD_KINDS:
                _assess_k8s_workload_doc(doc, path=path, namespace=namespace, findings=findings)
            elif _kind(doc) == "Ingress":
                _assess_k8s_ingress_doc(doc, path=path, findings=findings)
    evidence.append(
        {
            "check": "manifest",
            "status": "completed",
            "manifest_count": len(validation.get("manifests", [])),
        }
    )


def _assess_native_doc(
    doc: dict[str, Any],
    *,
    path: Path,
    namespace: str | None,
    findings: list[dict[str, Any]],
) -> None:
    metadata = _dict(doc.get("metadata"))
    spec = _dict(doc.get("spec"))
    name = str(metadata.get("name") or path.stem.replace(".k1s", ""))
    ns = str(namespace or metadata.get("namespace") or "default")
    scope = f"{ns}/{name}"
    image = str(spec.get("image") or "")
    if image:
        _assess_image(image, path=path, scope=scope, findings=findings)
    pull_policy = str(spec.get("imagePullPolicy") or spec.get("image_pull_policy") or "")
    if pull_policy.lower() == "never":
        findings.append(
            _finding(
                code="IMAGE_PULL_POLICY_NEVER",
                severity="medium",
                message=(
                    "Image pull policy is Never; remote deploy/export will not pull "
                    "a registry image."
                ),
                path=str(path),
                scope=scope,
                refs=("OWASP CI/CD Security Risks", "OWASP Kubernetes Top 10"),
                remediation=(
                    "Retag and push the image, then use IfNotPresent or Always "
                    "for remote handoff."
                ),
            )
        )
    _assess_resources(
        _dict(spec.get("resources")),
        path=path,
        scope=scope,
        container=name,
        findings=findings,
    )
    _assess_security_context(
        _dict(spec.get("securityContext") or spec.get("security_context")),
        path=path,
        scope=scope,
        container=name,
        findings=findings,
    )
    _assess_workload_booleans(
        spec,
        path=path,
        scope=scope,
        findings=findings,
    )
    _assess_native_storage(spec.get("storage"), path=path, scope=scope, findings=findings)
    _assess_env(spec.get("env"), path=path, scope=scope, container=name, findings=findings)
    for container in _list(spec.get("containers")) + _list(
        spec.get("initContainers") or spec.get("init_containers")
    ):
        if not isinstance(container, dict):
            continue
        container_name = str(container.get("name") or name)
        if container.get("image"):
            _assess_image(str(container.get("image")), path=path, scope=scope, findings=findings)
        _assess_resources(
            _dict(container.get("resources")),
            path=path,
            scope=scope,
            container=container_name,
            findings=findings,
        )
        _assess_security_context(
            _dict(container.get("securityContext") or container.get("security_context")),
            path=path,
            scope=scope,
            container=container_name,
            findings=findings,
        )
        _assess_env(
            container.get("env"),
            path=path,
            scope=scope,
            container=container_name,
            findings=findings,
        )
    if _kind(doc) != "Job":
        health = _dict(spec.get("health"))
        if not _dict(health.get("readiness")):
            findings.append(_missing_probe_finding("readiness", path=path, scope=scope))
        if not _dict(health.get("liveness")):
            findings.append(_missing_probe_finding("liveness", path=path, scope=scope))
    service_account = str(spec.get("serviceAccountName") or spec.get("service_account") or "")
    if not service_account or service_account == "default":
        findings.append(
            _finding(
                code="DEFAULT_SERVICE_ACCOUNT",
                severity="low",
                message="Workload does not declare a dedicated service account.",
                path=str(path),
                scope=scope,
                refs=("OWASP Kubernetes Top 10",),
                confidence="medium",
                remediation="Use a least-privilege service account for non-trivial workloads.",
            )
        )
    ingress = _dict(spec.get("ingress"))
    if ingress.get("host"):
        findings.append(
            _finding(
                code="PUBLIC_INGRESS_REVIEW",
                severity="low",
                message=(
                    "Manifest exposes ingress; review authentication, authorization, "
                    "and rate limits."
                ),
                path=str(path),
                scope=scope,
                evidence={"host": str(ingress.get("host"))},
                refs=("OWASP Web Top 10 2021 A05", "OWASP API Security Top 10 2023 API8"),
                confidence="medium",
                remediation=(
                    "Add app-specific authz smoke tests and document expected "
                    "public endpoints."
                ),
            )
        )


def _assess_k8s_workload_doc(
    doc: dict[str, Any],
    *,
    path: Path,
    namespace: str | None,
    findings: list[dict[str, Any]],
) -> None:
    metadata = _dict(doc.get("metadata"))
    spec = _dict(doc.get("spec"))
    name = str(metadata.get("name") or path.stem)
    ns = str(namespace or metadata.get("namespace") or "default")
    scope = f"{ns}/{name}"
    template = _dict(spec.get("template"))
    pod_spec = _dict(template.get("spec"))
    _assess_workload_booleans(pod_spec, path=path, scope=scope, findings=findings)
    _assess_k8s_volumes(pod_spec.get("volumes"), path=path, scope=scope, findings=findings)
    service_account = str(pod_spec.get("serviceAccountName") or "")
    if not service_account or service_account == "default":
        findings.append(
            _finding(
                code="DEFAULT_SERVICE_ACCOUNT",
                severity="low",
                message="Pod template uses the default service account.",
                path=str(path),
                scope=scope,
                refs=("OWASP Kubernetes Top 10",),
                confidence="medium",
                remediation=(
                    "Set serviceAccountName to a workload-specific least-privilege "
                    "account."
                ),
            )
        )
    containers = _list(pod_spec.get("containers")) + _list(pod_spec.get("initContainers"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        container_name = str(container.get("name") or name)
        image = str(container.get("image") or "")
        if image:
            _assess_image(image, path=path, scope=scope, findings=findings)
        pull_policy = str(container.get("imagePullPolicy") or "")
        if pull_policy.lower() == "never":
            findings.append(
                _finding(
                    code="IMAGE_PULL_POLICY_NEVER",
                    severity="medium",
                    message=(
                        "Container imagePullPolicy is Never; exported Kubernetes "
                        "will not pull from a registry."
                    ),
                    path=str(path),
                    scope=scope,
                    evidence={"container": container_name},
                    refs=("OWASP CI/CD Security Risks", "OWASP Kubernetes Top 10"),
                    remediation=(
                        "Use a pushed registry image and IfNotPresent or Always "
                        "for remote clusters."
                    ),
                )
            )
        _assess_resources(
            _dict(container.get("resources")),
            path=path,
            scope=scope,
            container=container_name,
            findings=findings,
        )
        _assess_security_context(
            _dict(container.get("securityContext")),
            path=path,
            scope=scope,
            container=container_name,
            findings=findings,
        )
        _assess_env(
            container.get("env"),
            path=path,
            scope=scope,
            container=container_name,
            findings=findings,
        )
        if _kind(doc) != "Job":
            if not _dict(container.get("readinessProbe")):
                findings.append(
                    _missing_probe_finding(
                        "readiness",
                        path=path,
                        scope=scope,
                        container=container_name,
                    )
                )
            if not _dict(container.get("livenessProbe")):
                findings.append(
                    _missing_probe_finding(
                        "liveness",
                        path=path,
                        scope=scope,
                        container=container_name,
                    )
                )


def _assess_k8s_ingress_doc(
    doc: dict[str, Any],
    *,
    path: Path,
    findings: list[dict[str, Any]],
) -> None:
    metadata = _dict(doc.get("metadata"))
    spec = _dict(doc.get("spec"))
    name = str(metadata.get("name") or path.stem)
    ns = str(metadata.get("namespace") or "default")
    hosts = [
        str(rule.get("host"))
        for rule in _list(spec.get("rules"))
        if isinstance(rule, dict) and rule.get("host")
    ]
    if hosts and not _list(spec.get("tls")):
        findings.append(
            _finding(
                code="K8S_INGRESS_TLS_MISSING",
                severity="medium",
                message="Kubernetes Ingress declares hosts without tls entries.",
                path=str(path),
                scope=f"{ns}/{name}",
                evidence={"hosts": hosts},
                refs=("OWASP Web Top 10 2021 A02", "OWASP Kubernetes Top 10"),
                remediation="Configure TLS for exported production Ingress resources.",
            )
        )
    if hosts:
        findings.append(
            _finding(
                code="PUBLIC_INGRESS_REVIEW",
                severity="low",
                message=(
                    "Ingress exposes HTTP routes; review authn/authz, rate limits, "
                    "and public inventory."
                ),
                path=str(path),
                scope=f"{ns}/{name}",
                evidence={"hosts": hosts},
                refs=("OWASP Web Top 10 2021 A05", "OWASP API Security Top 10 2023 API8"),
                confidence="medium",
                remediation="Document expected public routes and add negative authz smoke checks.",
            )
        )


def _assess_existing_exports(
    *,
    stage_dir: Path,
    namespace: str | None,
    findings: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> None:
    roots = [
        ("k8s", stage_dir / "exports" / "k8s"),
        ("helm", stage_dir / "exports" / "helm" / "templates"),
    ]
    scanned = 0
    for fmt, root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.y*ml")):
            scanned += 1
            docs = _load_yaml_documents(path.read_text(encoding="utf-8"))
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                if _kind(doc) in K8S_WORKLOAD_KINDS:
                    _assess_k8s_workload_doc(
                        doc,
                        path=path,
                        namespace=namespace,
                        findings=findings,
                    )
                elif _kind(doc) == "Ingress":
                    _assess_k8s_ingress_doc(doc, path=path, findings=findings)
                elif _kind(doc) == "Secret":
                    findings.append(
                        _finding(
                            code="EXPORTED_SECRET_REVIEW_REQUIRED",
                            severity="medium",
                            message=(
                                "Export includes a Kubernetes Secret; verify secret handling "
                                "before handoff."
                            ),
                            path=str(path),
                            scope=_doc_scope(doc, namespace=namespace),
                            refs=("OWASP CI/CD Security Risks", "OWASP Kubernetes Top 10"),
                            confidence="medium",
                            remediation=(
                                "Use an external secret manager or environment-specific "
                                "secret injection."
                            ),
                        )
                    )
        evidence.append({"check": "export", "format": fmt, "status": "scanned", "files": scanned})
    if scanned == 0:
        evidence.append(
            {
                "check": "export",
                "status": "skipped",
                "reason": "no existing k8s or helm exports under the staged bundle",
            }
        )


def _assess_runtime_surface(
    *,
    supervisor: WorkerBeeSupervisor,
    validation: dict[str, Any],
    checks: tuple[str, ...],
    runtime_probe: ProbeCallback | None,
    timeout: float,
    findings: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> None:
    urls = _manifest_ingress_urls(supervisor=supervisor, validation=validation)
    if not urls:
        evidence.append(
            {"check": "runtime", "status": "skipped", "reason": "no ingress hosts in manifests"}
        )
        return
    if runtime_probe is None:
        evidence.append(
            {
                "check": "runtime",
                "status": "skipped",
                "reason": "WorkerBee ingress probe is unavailable",
                "urls": urls,
            }
        )
        return
    scanned = 0
    for url in urls[:8]:
        scanned += 1
        if "headers" in checks:
            _assess_runtime_headers(url, runtime_probe, timeout=timeout, findings=findings)
        if "runtime" in checks:
            _assess_runtime_options(url, runtime_probe, timeout=timeout, findings=findings)
            _assess_runtime_docs(url, runtime_probe, timeout=timeout, findings=findings)
    evidence.append(
        {"check": "runtime", "status": "completed", "urls": urls[:8], "scanned": scanned}
    )


def _assess_runtime_headers(
    url: str,
    runtime_probe: ProbeCallback,
    *,
    timeout: float,
    findings: list[dict[str, Any]],
) -> None:
    result = _safe_probe(runtime_probe, url=url, method="GET", timeout=timeout)
    if not result.get("ok") and not result.get("status"):
        findings.append(_runtime_probe_failure(url, result))
        return
    status = int(result.get("status") or 0)
    if status >= 400:
        return
    headers = _lower_headers(_dict(result.get("headers")))
    missing = [display for key, display in _REQUIRED_SECURITY_HEADERS.items() if key not in headers]
    if missing:
        findings.append(
            _finding(
                code="MISSING_SECURITY_HEADERS",
                severity="medium",
                message="HTTP response is missing common browser security headers.",
                evidence={"url": url, "missing": missing, "status": status},
                refs=("OWASP Web Top 10 2021 A05", "OWASP API Security Top 10 2023 API8"),
                remediation="Set security headers at the app or edge before production handoff.",
            )
        )
    if "strict-transport-security" not in headers:
        findings.append(
            _finding(
                code="HSTS_NOT_OBSERVED",
                severity="low",
                message="HTTPS response did not include Strict-Transport-Security.",
                evidence={"url": url, "status": status},
                refs=("OWASP Web Top 10 2021 A02",),
                confidence="medium",
                remediation="Enable HSTS on non-local production HTTPS endpoints.",
            )
        )
    has_frame_policy = "x-frame-options" in headers or "frame-ancestors" in headers.get(
        "content-security-policy", ""
    )
    if not has_frame_policy:
        findings.append(
            _finding(
                code="FRAME_PROTECTION_NOT_OBSERVED",
                severity="low",
                message="HTTP response did not include clickjacking frame protection.",
                evidence={"url": url, "status": status},
                refs=("OWASP Web Top 10 2021 A05",),
                confidence="medium",
                remediation="Add X-Frame-Options or a CSP frame-ancestors directive.",
            )
        )


def _assess_runtime_options(
    url: str,
    runtime_probe: ProbeCallback,
    *,
    timeout: float,
    findings: list[dict[str, Any]],
) -> None:
    result = _safe_probe(runtime_probe, url=url, method="OPTIONS", timeout=timeout)
    status = int(result.get("status") or 0)
    if status < 200 or status >= 300:
        return
    headers = _lower_headers(_dict(result.get("headers")))
    allowed = _allowed_methods(headers.get("allow") or headers.get("access-control-allow-methods"))
    exposed = sorted(allowed & _MUTATING_METHODS)
    if exposed:
        findings.append(
            _finding(
                code="MUTATING_METHODS_EXPOSED",
                severity="medium",
                message="Unauthenticated OPTIONS response advertises mutating HTTP methods.",
                evidence={"url": url, "methods": exposed, "status": status},
                refs=("OWASP API Security Top 10 2023 API8", "OWASP Web Top 10 2021 A05"),
                confidence="medium",
                remediation="Confirm mutating routes enforce authentication and authorization.",
            )
        )


def _assess_runtime_docs(
    url: str,
    runtime_probe: ProbeCallback,
    *,
    timeout: float,
    findings: list[dict[str, Any]],
) -> None:
    origin = _origin(url)
    for path in _RUNTIME_DOC_PATHS:
        doc_url = _url_with_path(origin, path)
        result = _safe_probe(runtime_probe, url=doc_url, method="GET", timeout=timeout)
        status = int(result.get("status") or 0)
        if status == 200:
            findings.append(
                _finding(
                    code="API_DOCS_PUBLIC",
                    severity="low",
                    message=(
                        "API documentation or schema endpoint is reachable without auth "
                        "in local smoke test."
                    ),
                    evidence={"url": doc_url, "status": status},
                    refs=("OWASP API Security Top 10 2023 API8",),
                    confidence="medium",
                    remediation=(
                        "Decide whether API docs are intentionally public and gate them "
                        "otherwise."
                    ),
                )
            )
            return


def _assess_image(
    image: str,
    *,
    path: Path,
    scope: str,
    findings: list[dict[str, Any]],
) -> None:
    if _image_uses_latest_or_no_tag(image):
        findings.append(
            _finding(
                code="IMAGE_NOT_PINNED",
                severity="medium",
                message="Container image is not pinned to an immutable tag or digest.",
                path=str(path),
                scope=scope,
                evidence={"image": image},
                refs=("OWASP CI/CD Security Risks", "OWASP Kubernetes Top 10"),
                remediation=(
                    "Use a versioned tag or digest and record image provenance "
                    "for handoff."
                ),
            )
        )
    if image.startswith("workerbee-"):
        findings.append(
            _finding(
                code="LOCAL_IMAGE_REMOTE_HANDOFF",
                severity="medium",
                message=(
                    "WorkerBee-local image must be retagged and pushed before remote "
                    "deploy/export."
                ),
                path=str(path),
                scope=scope,
                evidence={"image": image},
                refs=("OWASP CI/CD Security Risks",),
                remediation=(
                    "Publish the image to a registry and update the staged manifest "
                    "before handoff."
                ),
            )
        )


def _assess_resources(
    resources: dict[str, Any],
    *,
    path: Path,
    scope: str,
    container: str,
    findings: list[dict[str, Any]],
) -> None:
    requests = _dict(resources.get("requests"))
    limits = _dict(resources.get("limits"))
    if not requests:
        findings.append(
            _finding(
                code="MISSING_RESOURCE_REQUESTS",
                severity="medium",
                message="Container does not declare resource requests.",
                path=str(path),
                scope=scope,
                evidence={"container": container},
                refs=("OWASP API Security Top 10 2023 API4", "OWASP Kubernetes Top 10"),
                remediation="Set CPU and memory requests based on expected workload behavior.",
            )
        )
    if not limits:
        findings.append(
            _finding(
                code="MISSING_RESOURCE_LIMITS",
                severity="medium",
                message="Container does not declare resource limits.",
                path=str(path),
                scope=scope,
                evidence={"container": container},
                refs=("OWASP API Security Top 10 2023 API4", "OWASP Kubernetes Top 10"),
                remediation=(
                    "Set memory and CPU limits or document why limits are intentionally "
                    "omitted."
                ),
            )
        )


def _assess_security_context(
    security_context: dict[str, Any],
    *,
    path: Path,
    scope: str,
    container: str,
    findings: list[dict[str, Any]],
) -> None:
    if security_context.get("privileged") is True:
        findings.append(
            _finding(
                code="PRIVILEGED_CONTAINER",
                severity="critical",
                message="Container securityContext enables privileged mode.",
                path=str(path),
                scope=scope,
                evidence={"container": container},
                refs=("OWASP Kubernetes Top 10",),
                remediation=(
                    "Remove privileged mode or isolate the workload behind a documented "
                    "exception."
                ),
            )
        )
    if security_context.get("allowPrivilegeEscalation") is True:
        findings.append(
            _finding(
                code="PRIVILEGE_ESCALATION_ALLOWED",
                severity="high",
                message="Container allows privilege escalation.",
                path=str(path),
                scope=scope,
                evidence={"container": container},
                refs=("OWASP Kubernetes Top 10",),
                remediation="Set allowPrivilegeEscalation: false.",
            )
        )
    capabilities = _dict(security_context.get("capabilities"))
    added = _list(capabilities.get("add"))
    if added:
        findings.append(
            _finding(
                code="LINUX_CAPABILITIES_ADDED",
                severity="high",
                message="Container adds Linux capabilities.",
                path=str(path),
                scope=scope,
                evidence={
                    "container": container,
                    "capabilities": [str(item) for item in added],
                },
                refs=("OWASP Kubernetes Top 10",),
                remediation=(
                    "Drop all capabilities by default and add only narrowly justified "
                    "exceptions."
                ),
            )
        )
    dropped = {str(item).upper() for item in _list(capabilities.get("drop"))}
    if "ALL" not in dropped:
        findings.append(
            _finding(
                code="CAPABILITIES_NOT_DROPPED",
                severity="low",
                message="Container does not drop all Linux capabilities.",
                path=str(path),
                scope=scope,
                evidence={"container": container},
                refs=("OWASP Kubernetes Top 10",),
                confidence="medium",
                remediation='Set securityContext.capabilities.drop: ["ALL"] where compatible.',
            )
        )
    if security_context.get("runAsNonRoot") is not True:
        findings.append(
            _finding(
                code="RUN_AS_NON_ROOT_MISSING",
                severity="medium",
                message="Container does not explicitly require a non-root user.",
                path=str(path),
                scope=scope,
                evidence={"container": container},
                refs=("OWASP Kubernetes Top 10",),
                confidence="medium",
                remediation="Set runAsNonRoot: true and use a non-root image user.",
            )
        )
    if security_context.get("readOnlyRootFilesystem") is not True:
        findings.append(
            _finding(
                code="READ_ONLY_ROOT_FS_MISSING",
                severity="low",
                message="Container root filesystem is not explicitly read-only.",
                path=str(path),
                scope=scope,
                evidence={"container": container},
                refs=("OWASP Kubernetes Top 10",),
                confidence="medium",
                remediation="Set readOnlyRootFilesystem: true where the app does not need writes.",
            )
        )


def _assess_workload_booleans(
    spec: dict[str, Any],
    *,
    path: Path,
    scope: str,
    findings: list[dict[str, Any]],
) -> None:
    host_network = spec.get("hostNetwork")
    if host_network is None:
        host_network = spec.get("host_network")
    if host_network is True:
        findings.append(
            _finding(
                code="HOST_NETWORK_ENABLED",
                severity="high",
                message="Workload enables host networking.",
                path=str(path),
                scope=scope,
                refs=("OWASP Kubernetes Top 10",),
                remediation=(
                    "Use normal pod networking unless host networking is explicitly "
                    "required."
                ),
            )
        )


def _assess_native_storage(
    storage: Any,
    *,
    path: Path,
    scope: str,
    findings: list[dict[str, Any]],
) -> None:
    for item in _list(storage):
        if not isinstance(item, dict):
            continue
        if item.get("hostPath") or str(item.get("type") or "").lower() == "hostpath":
            findings.append(
                _host_path_finding(
                    path=path,
                    scope=scope,
                    volume=str(item.get("name") or ""),
                )
            )


def _assess_k8s_volumes(
    volumes: Any,
    *,
    path: Path,
    scope: str,
    findings: list[dict[str, Any]],
) -> None:
    for volume in _list(volumes):
        if isinstance(volume, dict) and "hostPath" in volume:
            findings.append(
                _host_path_finding(
                    path=path,
                    scope=scope,
                    volume=str(volume.get("name") or ""),
                )
            )


def _assess_env(
    env: Any,
    *,
    path: Path,
    scope: str,
    container: str,
    findings: list[dict[str, Any]],
) -> None:
    for item in _list(env):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name or "value" not in item or not _looks_sensitive_name(name):
            continue
        findings.append(
            _finding(
                code="SECRET_LITERAL_ENV",
                severity="high",
                message="Sensitive-looking environment variable is set as a literal value.",
                path=str(path),
                scope=scope,
                evidence={"container": container, "env": name, "value": "***"},
                refs=("OWASP Web Top 10 2021 A05", "OWASP CI/CD Security Risks"),
                remediation="Move secret values into an environment-specific secret source.",
            )
        )


def _missing_probe_finding(
    probe: str,
    *,
    path: Path,
    scope: str,
    container: str | None = None,
) -> dict[str, Any]:
    evidence = {"probe": probe}
    if container:
        evidence["container"] = container
    return _finding(
        code=f"MISSING_{probe.upper()}_PROBE",
        severity="low" if probe == "liveness" else "medium",
        message=f"Workload does not declare a {probe} probe.",
        path=str(path),
        scope=scope,
        evidence=evidence,
        refs=("OWASP Kubernetes Top 10",),
        confidence="medium",
        remediation=f"Add a {probe} probe for safer rollout and failure detection.",
    )


def _host_path_finding(*, path: Path, scope: str, volume: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    if volume:
        evidence["volume"] = volume
    return _finding(
        code="HOST_PATH_VOLUME",
        severity="high",
        message="Workload mounts a hostPath volume.",
        path=str(path),
        scope=scope,
        evidence=evidence,
        refs=("OWASP Kubernetes Top 10",),
        remediation=(
            "Replace hostPath with a scoped volume or document a tightly bounded "
            "exception."
        ),
    )


def _manifest_ingress_urls(
    *,
    supervisor: WorkerBeeSupervisor,
    validation: dict[str, Any],
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for detail in validation.get("manifest_details", []):
        if not isinstance(detail, dict):
            continue
        path = Path(str(detail.get("path") or ""))
        input_kind = str(detail.get("input_kind") or "")
        if not path.is_file() or input_kind not in {NATIVE_K1S, KUBERNETES}:
            continue
        docs = _load_yaml_documents(path.read_text(encoding="utf-8"))
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            for host, route_path in _doc_ingress_routes(doc, input_kind=input_kind):
                if not host:
                    continue
                if supervisor.ingress is not None:
                    url = supervisor.ingress.url(host, route_path)
                else:
                    normalized_path = route_path if route_path.startswith("/") else f"/{route_path}"
                    url = f"https://{host}:19443{normalized_path}"
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
    return urls


def _doc_ingress_routes(doc: dict[str, Any], *, input_kind: str) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    if input_kind == NATIVE_K1S:
        ingress = _dict(_dict(doc.get("spec")).get("ingress"))
        host = str(ingress.get("host") or "")
        path = str(ingress.get("path") or "/")
        if host:
            routes.append((host, path))
        return routes
    if _kind(doc) != "Ingress":
        return routes
    spec = _dict(doc.get("spec"))
    for rule in _list(spec.get("rules")):
        if not isinstance(rule, dict):
            continue
        host = str(rule.get("host") or "")
        http = _dict(rule.get("http"))
        paths = _list(http.get("paths")) or [{"path": "/"}]
        for item in paths:
            if isinstance(item, dict):
                routes.append((host, str(item.get("path") or "/")))
    return routes


def _safe_probe(
    runtime_probe: ProbeCallback,
    *,
    url: str,
    method: str,
    timeout: float,
) -> dict[str, Any]:
    try:
        return runtime_probe(url=url, method=method, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - advisory probes should degrade into findings
        return {"ok": False, "url": url, "method": method, "error": str(exc)}


def _runtime_probe_failure(url: str, result: dict[str, Any]) -> dict[str, Any]:
    return _finding(
        code="RUNTIME_PROBE_FAILED",
        severity="low",
        message="Runtime security probe failed.",
        evidence={"url": url, "error": str(result.get("error") or result)},
        refs=("OWASP Web Top 10 2021 A05",),
        confidence="low",
        remediation=(
            "Deploy the staged workload locally before runtime checks, or inspect "
            "ingress health."
        ),
    )


def _finding(
    *,
    code: str,
    severity: str,
    message: str,
    refs: tuple[str, ...],
    path: str | None = None,
    scope: str | None = None,
    evidence: dict[str, Any] | None = None,
    confidence: str = "high",
    remediation: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
        "confidence": confidence,
        "standard_refs": [{"name": ref, "url": _REFERENCE_URLS[ref]} for ref in refs],
    }
    if path:
        payload["path"] = path
    if scope:
        payload["scope"] = scope
    if evidence:
        payload["evidence"] = _redact_sensitive(evidence)
    if remediation:
        payload["remediation"] = remediation
    return payload


def _assign_finding_ids(findings: list[dict[str, Any]]) -> None:
    for index, finding in enumerate(findings, start=1):
        finding["id"] = f"WBSEC-{index:03d}"


def _finding_summary(findings: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "info": 0,
        "total": len(findings),
    }
    for finding in findings:
        severity = str(finding.get("severity") or "info")
        if severity in summary:
            summary[severity] += 1
    return summary


def _risk_score(findings: list[dict[str, Any]]) -> int:
    return min(
        100,
        sum(
            _SEVERITY_WEIGHTS.get(str(finding.get("severity") or "info"), 1) for finding in findings
        ),
    )


def _suggested_patches(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    patches = []
    emitted: set[tuple[str, str | None]] = set()
    for finding in findings:
        code = str(finding.get("code") or "")
        key = (code, _str_or_none(finding.get("path")))
        if key in emitted:
            continue
        emitted.add(key)
        change = _patch_hint_for_code(code)
        if not change:
            continue
        patches.append(
            {
                "finding_code": code,
                "path": finding.get("path"),
                "summary": finding.get("message"),
                "change": change,
            }
        )
    return patches


def _suggested_policies(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    codes = {str(finding.get("code") or "") for finding in findings}
    policies = []
    if codes & {
        "PRIVILEGED_CONTAINER",
        "PRIVILEGE_ESCALATION_ALLOWED",
        "RUN_AS_NON_ROOT_MISSING",
        "CAPABILITIES_NOT_DROPPED",
        "HOST_PATH_VOLUME",
    }:
        policies.append(
            {
                "type": "pod-security",
                "name": "restricted-workload-baseline",
                "summary": (
                    "Require restricted pod security defaults for exported Kubernetes "
                    "workloads."
                ),
                "example": (
                    "Label production namespaces with "
                    "pod-security.kubernetes.io/enforce=restricted and audit exceptions explicitly."
                ),
            }
        )
    if "PUBLIC_INGRESS_REVIEW" in codes or "K8S_INGRESS_TLS_MISSING" in codes:
        policies.append(
            {
                "type": "network-policy",
                "name": "default-deny-plus-ingress-allowlist",
                "summary": (
                    "Use a default-deny NetworkPolicy and allow only expected ingress "
                    "paths/upstreams."
                ),
                "example": (
                    "Start with namespace default-deny ingress/egress, then add "
                    "app-specific allow rules."
                ),
            }
        )
    if "SECRET_LITERAL_ENV" in codes or "EXPORTED_SECRET_REVIEW_REQUIRED" in codes:
        policies.append(
            {
                "type": "secret-handling",
                "name": "no-literal-secrets",
                "summary": "Reject literal secret values in manifests and exports.",
                "example": (
                    "Use external secret injection or sealed environment-specific "
                    "secret material."
                ),
            }
        )
    return policies


def _patch_hint_for_code(code: str) -> str | None:
    hints = {
        "IMAGE_NOT_PINNED": "Replace floating image tags with versioned tags or digests.",
        "LOCAL_IMAGE_REMOTE_HANDOFF": (
            "Retag WorkerBee-local images to a registry reference before remote handoff."
        ),
        "IMAGE_PULL_POLICY_NEVER": (
            "Use IfNotPresent or Always after publishing the image to a registry."
        ),
        "MISSING_RESOURCE_REQUESTS": (
            "Add CPU and memory requests to the container resources block."
        ),
        "MISSING_RESOURCE_LIMITS": (
            "Add CPU and memory limits, or document why limits are intentionally omitted."
        ),
        "RUN_AS_NON_ROOT_MISSING": (
            "Set runAsNonRoot: true and use an image with a non-root user."
        ),
        "READ_ONLY_ROOT_FS_MISSING": (
            "Set readOnlyRootFilesystem: true where runtime writes are not required."
        ),
        "CAPABILITIES_NOT_DROPPED": (
            'Set capabilities.drop to ["ALL"] and add only justified capabilities.'
        ),
        "MISSING_READINESS_PROBE": "Add a readiness probe that checks real app readiness.",
        "MISSING_LIVENESS_PROBE": "Add a liveness probe that detects unrecoverable app hangs.",
        "SECRET_LITERAL_ENV": "Move the value to an environment-specific secret source.",
        "K8S_INGRESS_TLS_MISSING": "Add spec.tls for production Kubernetes Ingress hosts.",
    }
    return hints.get(code)


def _image_uses_latest_or_no_tag(image: str) -> bool:
    if not image or "@" in image:
        return False
    last = image.rsplit("/", 1)[-1]
    if ":" not in last:
        return True
    return last.rsplit(":", 1)[-1].lower() == "latest"


def _looks_sensitive_name(name: str) -> bool:
    lowered = name.lower().replace("-", "_")
    return any(marker in lowered for marker in _SENSITIVE_NAME_MARKERS)


def _allowed_methods(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def _lower_headers(headers: dict[str, Any]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _url_with_path(origin: str, path: str) -> str:
    parsed = urlsplit(origin)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _doc_scope(doc: dict[str, Any], *, namespace: str | None) -> str:
    metadata = _dict(doc.get("metadata"))
    ns = str(namespace or metadata.get("namespace") or "default")
    name = str(metadata.get("name") or _kind(doc) or "resource")
    return f"{ns}/{name}"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            out[key_str] = "***" if _looks_sensitive_name(key_str) else _redact_sensitive(item)
        return out
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value
