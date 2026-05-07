from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def _urls() -> list[str]:
    raw = os.getenv("STORE_URLS", "")
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()]


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _config_root() -> Path:
    return Path(os.getenv("AE_CONFIG_ROOT", "/var/run/ae/config/workerbee-poc--api"))


def _store_request(path: str, *, data: bytes | None = None) -> tuple[int, str, str]:
    last = ""
    method = "POST" if data is not None else "GET"
    for base in _urls():
        url = f"{base}{path}"
        try:
            req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
            with urllib.request.urlopen(req, timeout=2.5) as resp:  # noqa: S310
                return int(resp.status), resp.read().decode("utf-8"), base
        except Exception as exc:  # noqa: BLE001
            last = f"{base}: {exc}"
    return 503, json.dumps({"error": "store_unreachable", "last": last}), ""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            status, body, store = _store_request("/healthz")
            self._send(
                200 if status == 200 else 503,
                {
                    "ok": status == 200,
                    "store": store,
                    "mode_file": _read(str(_config_root() / "config" / "mode.txt")),
                    "color_file": _read(str(_config_root() / "config" / "color.txt")),
                    "token_present": bool(_read(str(_config_root() / "secret" / "token"))),
                    "env_mode": os.getenv("MODE")
                    or os.getenv("mode")  # noqa: SIM112 - validates k1s env key handling.
                    or os.getenv("APP_MODE"),
                    "store_body": body[:300],
                },
            )
            return
        if path == "/api/check":
            _store_request("/kv/api", data=b"checked")
            status, body, store = _store_request("/kv/api")
            self._send(
                200 if status == 200 else 503,
                {
                    "ok": status == 200,
                    "store": store,
                    "store_response": body,
                    "project": "workerbee",
                },
            )
            return
        self._send(404, {"error": "not_found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
