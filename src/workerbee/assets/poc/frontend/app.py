from __future__ import annotations

import html
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def _api_urls() -> list[str]:
    raw = os.getenv("API_URLS", "")
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()]


def _api_check() -> tuple[bool, str, str]:
    last = ""
    for base in _api_urls():
        try:
            with urllib.request.urlopen(f"{base}/api/check", timeout=3) as resp:
                return True, base, resp.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            last = f"{base}: {exc}"
    return False, "", last


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        ok, api, body = _api_check()
        if path == "/healthz":
            payload = json.dumps({"ok": ok, "api": api, "body": body[:300]}).encode("utf-8")
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        content = f"""<!doctype html>
<html>
  <head><title>WorkerBee POC</title></head>
  <body>
    <h1>WorkerBee POC</h1>
    <p>API reachable: {html.escape(str(ok))}</p>
    <pre>{html.escape(body[:1000])}</pre>
  </body>
</html>
""".encode("utf-8")
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

