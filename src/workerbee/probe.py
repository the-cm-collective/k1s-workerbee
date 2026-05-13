"""WorkerBee-managed HTTPS probe helpers."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, urlparse, urlunsplit

from workerbee.contract import WorkerBeeError
from workerbee.http import request_https_via_loopback
from workerbee.supervisor import project_slug

BODY_EXCERPT_LIMIT = 4096
SELECTED_HEADERS = {
    "access-control-allow-methods",
    "allow",
    "content-security-policy",
    "content-type",
    "referrer-policy",
    "location",
    "permissions-policy",
    "server",
    "strict-transport-security",
    "x-workerbee",
    "x-content-type-options",
    "x-frame-options",
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
    return f"https://dashboard.{_base_domain(ingress_info)}:{https_port}/"


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
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    method = method.upper()
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message=(
                "ingress probe method must be GET, HEAD, POST, PUT, PATCH, DELETE, "
                "or OPTIONS"
            ),
            details={"method": method},
        )
    if json_body is not None and body is not None:
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="ingress probe accepts either json_body or body, not both",
        )
    if method in {"GET", "HEAD", "OPTIONS"} and (json_body is not None or body is not None):
        raise WorkerBeeError(
            code="VALIDATION_FAILED",
            message="ingress probe request bodies are not supported for GET, HEAD, or OPTIONS",
            details={"method": method},
        )
    parsed = _validate_workerbee_url(project=project, ingress_info=ingress_info, url=url)
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
    request_headers = _validated_request_headers(headers)
    request_headers.setdefault("Accept", "application/json, text/plain, */*")
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        _set_default_header(request_headers, "Content-Type", "application/json")
    elif body is not None:
        data = body.encode("utf-8")
        _set_default_header(request_headers, "Content-Type", "text/plain; charset=utf-8")
    started = time.monotonic()
    probe_method = "direct"
    connect_url = url
    primary_error: str | None = None
    try:
        status, response_body, response_headers = _request_direct(
            url=url,
            method=method,
            data=data,
            headers=request_headers,
            ca_bundle=ca_bundle,
            timeout=timeout,
        )
    except OSError as exc:
        primary_error = str(exc)
        probe_method = "loopback-host-header"
        connect_url = _loopback_url(parsed)
        try:
            result = request_https_via_loopback(
                connect_url,
                server_hostname=parsed.hostname or "",
                host_header=parsed.netloc,
                method=method,
                data=data,
                headers=request_headers,
                timeout=timeout,
                ca_bundle=ca_bundle,
            )
        except OSError as loopback_exc:
            message = str(loopback_exc)
            if primary_error:
                message = f"{message}; direct probe failed first: {primary_error}"
            raise WorkerBeeError(
                code="PROBE_FAILED",
                message=message,
                details={
                    "url": url,
                    "connect_url": connect_url,
                    "primary_error": primary_error,
                    "loopback_error": str(loopback_exc),
                },
                retryable=True,
                remediation="Check WorkerBee project status, ingress routes, and Caddy health.",
            ) from loopback_exc
        status = result.status
        response_body = b"" if method == "HEAD" else result.body
        response_headers = result.headers
    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = response_body.decode("utf-8", errors="replace")
    status_matches = expected_status is None or status == expected_status
    body_matches = body_contains is None or body_contains in text
    return {
        "ok": status_matches and body_matches,
        "url": url,
        "method": method,
        "probe_method": probe_method,
        "connect_url": connect_url,
        "status": status,
        "expected_status": expected_status,
        "status_matches": status_matches,
        "body_contains": body_contains,
        "body_matches": body_matches,
        "elapsed_ms": elapsed_ms,
        "tls_verified": True,
        "headers": _selected_headers(response_headers),
        "body_excerpt": text[:BODY_EXCERPT_LIMIT],
        "body_truncated": len(text) > BODY_EXCERPT_LIMIT,
        **({"primary_error": primary_error} if primary_error else {}),
    }


def _request_direct(
    *,
    url: str,
    method: str,
    data: bytes | None,
    headers: dict[str, str],
    ca_bundle: Path,
    timeout: float,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(  # noqa: S310 - restricted localhost URL
        url,
        data=data,
        headers=headers,
        method=method,
    )
    context = ssl.create_default_context(cafile=str(ca_bundle))
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            status = int(response.status)
            body = b"" if method == "HEAD" else response.read()
            headers = {str(k): str(v) for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = b"" if method == "HEAD" else exc.read()
        headers = {str(k): str(v) for k, v in exc.headers.items()}
    return status, body, headers


def _validate_workerbee_url(
    *,
    project: str,
    ingress_info: dict[str, Any],
    url: str,
) -> ParseResult:
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
    base_domain = _base_domain(ingress_info)
    project_domain = f"{project}.{base_domain}"
    project_suffix = f".{project_domain}"
    allowed = (
        host == f"dashboard.{base_domain}"
        or host == project_domain
        or host.endswith(project_suffix)
    )
    if not allowed:
        raise WorkerBeeError(
            code="UNSUPPORTED_PROBE_URL",
            message="WorkerBee ingress probe is restricted to WorkerBee-managed hosts",
            details={"url": url, "project": project},
        )
    return parsed


def _loopback_url(parsed: ParseResult) -> str:
    return urlunsplit(
        (
            parsed.scheme,
            f"127.0.0.1:{parsed.port or 443}",
            parsed.path or "/",
            parsed.query,
            "",
        )
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


def _base_domain(ingress_info: dict[str, Any]) -> str:
    return str(ingress_info.get("base_domain") or "workerbee.localhost").strip().lower()


def _selected_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in SELECTED_HEADERS or key.lower().startswith("x-workerbee-")
    }


def _validated_request_headers(headers: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = str(key).strip()
        if not name:
            raise WorkerBeeError(code="VALIDATION_FAILED", message="header name cannot be empty")
        lowered = name.lower()
        if lowered == "host":
            raise WorkerBeeError(
                code="VALIDATION_FAILED",
                message="ingress probe does not allow overriding the Host header",
            )
        if lowered == "content-length":
            raise WorkerBeeError(
                code="VALIDATION_FAILED",
                message="ingress probe does not allow overriding Content-Length",
            )
        if any(ch in name for ch in "\r\n") or any(ch in str(value) for ch in "\r\n"):
            raise WorkerBeeError(
                code="VALIDATION_FAILED",
                message="ingress probe headers cannot contain newline characters",
            )
        out[name] = str(value)
    return out


def _set_default_header(headers: dict[str, str], name: str, value: str) -> None:
    if not any(key.lower() == name.lower() for key in headers):
        headers[name] = value
