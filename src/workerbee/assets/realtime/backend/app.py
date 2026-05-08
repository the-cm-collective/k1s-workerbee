from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _db_urls() -> list[str]:
    raw = os.getenv("DB_URLS", "")
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()]


def _db_request(path: str, *, data: bytes | None = None) -> tuple[int, str, str]:
    method = "POST" if data is not None else "GET"
    last = ""
    for base in _db_urls():
        try:
            req = urllib.request.Request(f"{base}{path}", data=data, method=method)  # noqa: S310
            with urllib.request.urlopen(req, timeout=2.5) as resp:  # noqa: S310
                return int(resp.status), resp.read().decode("utf-8"), base
        except Exception as exc:  # noqa: BLE001
            last = f"{base}: {exc}"
    return 503, json.dumps({"error": "db_unreachable", "last": last}), ""


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
        if path == "/ws":
            self._handle_websocket()
            return
        if path == "/healthz":
            status, body, db = _db_request("/healthz")
            self._send(200 if status == 200 else 503, {"ok": status == 200, "db": db, "body": body})
            return
        if path == "/api/seed":
            status, body, db = _db_request("/kv/boot")
            self._send(
                200 if status == 200 else 503,
                {"ok": status == 200, "db": db, "seed": body},
            )
            return
        self._send(404, {"error": "not_found"})

    def _handle_websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(400, {"error": "missing websocket key"})
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()  # noqa: S324
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        payload = _read_ws_text(self.rfile)
        _write_ws_text(self.wfile, f"echo:{payload}")


def _read_ws_text(stream) -> str:  # noqa: ANN001
    header = stream.read(2)
    if len(header) != 2:
        return ""
    _flags, second = header
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", stream.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", stream.read(8))[0]
    mask = stream.read(4) if masked else b""
    data = stream.read(length)
    if masked:
        data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    return data.decode("utf-8", errors="replace")


def _write_ws_text(stream, text: str) -> None:  # noqa: ANN001
    data = text.encode("utf-8")
    stream.write(bytes([0x81]))
    if len(data) < 126:
        stream.write(bytes([len(data)]))
    else:
        stream.write(bytes([126, *struct.pack("!H", len(data))]))
    stream.write(data)
    stream.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
