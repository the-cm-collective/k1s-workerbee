from __future__ import annotations

import html
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def _backend_urls() -> list[str]:
    raw = os.getenv("BACKEND_URLS", "")
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()]


def _backend_seed() -> tuple[bool, str, str]:
    last = ""
    for base in _backend_urls():
        try:
            with urllib.request.urlopen(f"{base}/api/seed", timeout=3) as resp:  # noqa: S310
                return True, base, resp.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            last = f"{base}: {exc}"
    return False, "", last


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        ok, backend, body = _backend_seed()
        if path == "/healthz":
            payload = json.dumps({"ok": ok, "backend": backend, "body": body[:500]}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        api_base = os.getenv("PUBLIC_API_BASE", "")
        ws_url = os.getenv("PUBLIC_WS_URL", "")
        content = f"""<!doctype html>
<html>
  <head><title>WorkerBee Realtime</title></head>
  <body>
    <h1>WorkerBee Realtime</h1>
    <p id="seed">Seed reachable: {html.escape(str(ok))}</p>
    <pre>{html.escape(body[:1000])}</pre>
    <script>
      window.workerbeeRealtime = {{apiBase: {json.dumps(api_base)}, wsUrl: {json.dumps(ws_url)}}};
      const ws = new WebSocket(window.workerbeeRealtime.wsUrl);
      ws.onopen = () => ws.send('browser-smoke');
      ws.onmessage = (event) => {{
        const p = document.createElement('p');
        p.id = 'ws-result';
        p.textContent = event.data;
        document.body.appendChild(p);
      }};
    </script>
  </body>
</html>
""".encode()
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
