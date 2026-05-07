from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

DATA = {os.getenv("SEED_KEY", "boot"): os.getenv("SEED_VALUE", "workerbee")}


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
            self._send(200, {"ok": True, "keys": sorted(DATA)})
            return
        if path == "/dump":
            self._send(200, {"data": DATA})
            return
        if path.startswith("/kv/"):
            key = unquote(path[len("/kv/") :])
            self._send(200 if key in DATA else 404, {"key": key, "value": DATA.get(key)})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/kv/"):
            self._send(404, {"error": "not_found"})
            return
        key = unquote(path[len("/kv/") :])
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8") if length else ""
        DATA[key] = body or "set"
        self._send(200, {"key": key, "value": DATA[key]})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

