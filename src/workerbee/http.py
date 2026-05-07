"""Small stdlib HTTP helpers used by WorkerBee."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


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
    data: bytes | None = None,
    json_body: Any | None = None,
    timeout: float = 5.0,
    verify_tls: bool = True,
) -> HTTPResult:
    headers: dict[str, str] = {}
    payload = data
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    context = None
    if url.startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()  # noqa: S323 - local dev certs only
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
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

