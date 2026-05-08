"""WorkerBee-managed HTTPS probe helpers."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from workerbee.contract import WorkerBeeError
from workerbee.supervisor import project_slug

BODY_EXCERPT_LIMIT = 4096
SELECTED_HEADERS = {
    "content-type",
    "location",
    "server",
    "x-workerbee",
    "x-request-id",
}


def build_probe_url(
    *,
    ingress_info: dict[str, Any],
    url: str | None = None,
    host: str | None = None,
    path: str = "/",
) -> str:
    if url:
        return url
    https_port = _https_port(ingress_info)
    if host:
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"https://{host}:{https_port}{normalized_path}"
    dashboard = str(ingress_info.get("dashboard_url") or "")
    if dashboard:
        return dashboard
    return f"https://dashboard.workerbee.localhost:{https_port}/"


def probe_workerbee_url(
    *,
    project: str,
    ingress_info: dict[str, Any],
    url: str,
    method: str = "GET",
    expected_status: int | None = None,
    body_contains: str | None = None,
    json_body: dict[str, Any] | None = None,
    body: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    method = method.upper()
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="ingress probe method must be GET, HEAD, POST, PUT, PATCH, or DELETE",
            details={"method": method},
        )
    if json_body is not None and body is not None:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="ingress probe accepts either json_body or body, not both",
        )
    if method in {"GET", "HEAD"} and (json_body is not None or body is not None):
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="ingress probe request bodies are not supported for GET or HEAD",
            details={"method": method},
        )
    _validate_workerbee_url(project=project, ingress_info=ingress_info, url=url)
    ca_bundle = Path(str(ingress_info.get("ca_bundle") or "")).expanduser()
    if not ca_bundle.is_file():
        raise WorkerBeeError(
            code="CA_NOT_READY",
            message="WorkerBee Caddy CA bundle is not ready",
            details={"ca_bundle": str(ca_bundle)},
            remediation="Start WorkerBee MCP and open/probe the dashboard once Caddy is ready.",
            retryable=True,
        )

    data = None
    headers = {"Accept": "application/json, text/plain, */*"}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif body is not None:
        data = body.encode("utf-8")
        headers["Content-Type"] = "text/plain; charset=utf-8"
    request = urllib.request.Request(  # noqa: S310 - restricted localhost URL
        url,
        data=data,
        headers=headers,
        method=method,
    )
    context = ssl.create_default_context(cafile=str(ca_bundle))
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            status = int(response.status)
            response_body = b"" if method == "HEAD" else response.read()
            headers = {str(k): str(v) for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        response_body = b"" if method == "HEAD" else exc.read()
        headers = {str(k): str(v) for k, v in exc.headers.items()}
    except OSError as exc:
        raise WorkerBeeError(
            code="PROBE_FAILED",
            message=str(exc),
            details={"url": url},
            retryable=True,
            remediation="Check WorkerBee project status, ingress routes, and Caddy health.",
        ) from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = response_body.decode("utf-8", errors="replace")
    status_matches = expected_status is None or status == expected_status
    body_matches = body_contains is None or body_contains in text
    return {
        "ok": status_matches and body_matches,
        "url": url,
        "method": method,
        "status": status,
        "expected_status": expected_status,
        "status_matches": status_matches,
        "body_contains": body_contains,
        "body_matches": body_matches,
        "elapsed_ms": elapsed_ms,
        "tls_verified": True,
        "headers": _selected_headers(headers),
        "body_excerpt": text[:BODY_EXCERPT_LIMIT],
        "body_truncated": len(text) > BODY_EXCERPT_LIMIT,
    }


def _validate_workerbee_url(*, project: str, ingress_info: dict[str, Any], url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise WorkerBeeError(
            code="UNSUPPORTED_PROBE_URL",
            message="WorkerBee ingress probe only supports https URLs",
            details={"url": url},
        )
    if parsed.port != _https_port(ingress_info):
        raise WorkerBeeError(
            code="UNSUPPORTED_PROBE_URL",
            message="WorkerBee ingress probe URL must use the WorkerBee HTTPS port",
            details={"url": url, "https_port": _https_port(ingress_info)},
        )
    project = project_slug(project)
    project_suffix = f".{project}.workerbee.localhost"
    allowed = (
        host == "dashboard.workerbee.localhost"
        or host == f"{project}.workerbee.localhost"
        or host.endswith(project_suffix)
    )
    if not allowed:
        raise WorkerBeeError(
            code="UNSUPPORTED_PROBE_URL",
            message="WorkerBee ingress probe is restricted to WorkerBee-managed localhost hosts",
            details={"url": url, "project": project},
        )


def _https_port(ingress_info: dict[str, Any]) -> int:
    try:
        port = int(ingress_info.get("https_port") or 0)
    except (TypeError, ValueError):
        port = 0
    if port <= 0:
        raise WorkerBeeError(
            code="INGRESS_NOT_READY",
            message="WorkerBee global ingress is not ready",
            details={"ingress": ingress_info},
            remediation="Start `workerbee mcp start` before probing ingress.",
            retryable=True,
        )
    return port


def _selected_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in SELECTED_HEADERS or key.lower().startswith("x-workerbee-")
    }
