"""Small stdlib HTTP helpers used by WorkerBee."""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


@dataclass(slots=True)
class HTTPResult:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    json_body: Any | None = None,
    timeout: float = 5.0,
    verify_tls: bool = True,
    ca_bundle: str | Path | None = None,
) -> HTTPResult:
    request_headers: dict[str, str] = dict(headers or {})
    payload = data
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(  # noqa: S310
        url,
        data=payload,
        headers=request_headers,
        method=method,
    )
    context = None
    if url.startswith("https://") and ca_bundle:
        context = ssl.create_default_context(cafile=str(ca_bundle))
    elif url.startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()  # noqa: S323 - local dev certs only
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:  # noqa: S310
            return HTTPResult(
                status=int(resp.status),
                body=resp.read(),
                headers={str(k): str(v) for k, v in resp.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return HTTPResult(
            status=int(exc.code),
            body=exc.read(),
            headers={str(k): str(v) for k, v in exc.headers.items()},
        )


def request_https_via_loopback(
    url: str,
    *,
    server_hostname: str,
    host_header: str,
    method: str = "GET",
    token: str | None = None,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 5.0,
    verify_tls: bool = True,
    ca_bundle: str | Path | None = None,
) -> HTTPResult:
    method = method.upper()
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError(
            "loopback HTTPS probe method must be GET, HEAD, POST, PUT, PATCH, or DELETE"
        )
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("loopback HTTPS probe requires an https:// URL")
    if "\r" in host_header or "\n" in host_header:
        raise ValueError("invalid Host header")
    request_headers: dict[str, str] = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    for key, value in request_headers.items():
        if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
            raise ValueError("invalid HTTP header")
    if data is not None and not any(key.lower() == "content-length" for key in request_headers):
        request_headers["Content-Length"] = str(len(data))
    port = int(parsed.port or 443)
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    context = (
        ssl.create_default_context(cafile=str(ca_bundle))
        if verify_tls and ca_bundle
        else ssl.create_default_context()
        if verify_tls
        else ssl._create_unverified_context()  # noqa: S323 - local dev certs only
    )
    with (
        socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw_sock,
        context.wrap_socket(raw_sock, server_hostname=server_hostname) as sock,
    ):
        sock.settimeout(timeout)
        header_lines = "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
        payload = (
            f"{method} {target} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            f"{header_lines}"
            "User-Agent: workerbee-local-probe\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        sock.sendall(payload)
        if data:
            sock.sendall(data)
        raw = _read_all(sock)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines:
        raise OSError("empty HTTPS response")
    status_parts = lines[0].decode("iso-8859-1", errors="replace").split()
    if len(status_parts) < 2:
        raise OSError(f"invalid HTTPS response status: {lines[0]!r}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, separator, value = line.partition(b":")
        if separator:
            headers[key.decode("iso-8859-1").strip()] = value.decode(
                "iso-8859-1",
                errors="replace",
            ).strip()
    return HTTPResult(status=int(status_parts[1]), body=body, headers=headers)


def _read_all(sock: ssl.SSLSocket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def wait_for_http(
    url: str,
    *,
    token: str | None = None,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 0.5,
    verify_tls: bool = True,
    ok_statuses: set[int] | None = None,
) -> HTTPResult:
    ok_statuses = ok_statuses or {200}
    deadline = time.monotonic() + timeout_seconds
    last: HTTPResult | None = None
    while time.monotonic() < deadline:
        try:
            last = request(url, token=token, timeout=2.0, verify_tls=verify_tls)
            if last.status in ok_statuses:
                return last
        except OSError:
            pass
        time.sleep(interval_seconds)
    if last is not None:
        raise TimeoutError(f"{url} did not become healthy: last status {last.status}")
    raise TimeoutError(f"{url} did not become healthy")
