from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

DATA = {"boot": "workerbee"}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"ok": True, "keys": sorted(DATA)})
            return
        if path == "/openapi.json":
            self._send_json(
                200,
                {
                    "openapi": "3.0.0",
                    "info": {"title": "WorkerBee Security Fixture", "version": "0.1.0"},
                    "paths": {
                        "/kv/{key}": {
                            "get": {"responses": {"200": {"description": "read"}}},
                            "post": {"responses": {"200": {"description": "write"}}},
                            "delete": {"responses": {"200": {"description": "delete"}}},
                        }
                    },
                },
            )
            return
        if path.startswith("/kv/"):
            key = unquote(path[len("/kv/") :])
            self._send_json(200 if key in DATA else 404, {"key": key, "value": DATA.get(key)})
            return
        if path == "/":
            body = b"<!doctype html><title>Security Fixture</title><h1>Security Fixture</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        self._write_key()

    def do_PUT(self) -> None:  # noqa: N802
        self._write_key()

    def do_PATCH(self) -> None:  # noqa: N802
        self._write_key()

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/kv/"):
            self._send_json(404, {"error": "not_found"})
            return
        key = unquote(path[len("/kv/") :])
        DATA.pop(key, None)
        self._send_json(200, {"deleted": key})

    def _write_key(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/kv/"):
            self._send_json(404, {"error": "not_found"})
            return
        key = unquote(path[len("/kv/") :])
        length = int(self.headers.get("Content-Length", "0") or "0")
        DATA[key] = self.rfile.read(length).decode("utf-8") if length else "set"
        self._send_json(200, {"key": key, "value": DATA[key]})

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
