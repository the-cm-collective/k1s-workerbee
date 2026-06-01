#!/usr/bin/env python3
"""Minimal advisory router for the AI fabric lab."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

COORDINATOR_URL = os.getenv(
    "COORDINATOR_URL",
    "http://ai-models:8001/v1/chat/completions",
)
EXPERT_URL = os.getenv(
    "EXPERT_URL",
    "http://ai-models:8002/v1/chat/completions",
)
DAS_URL = os.getenv("DAS_URL", "http://das-bridge:8081")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
PROXY_TIMEOUT = float(os.getenv("AI_ROUTER_PROXY_TIMEOUT", "120"))


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-fabric-router/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json({"ok": True, "service": "ai-router"})
            return
        if self.path == "/metrics":
            self._text("ai_fabric_router_up 1\n")
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        if self.path in {"/v1/advisory/query", "/v1/advisory/evaluate"}:
            self._json(_advisory_response(payload))
            return
        if self.path == "/v1/chat/completions":
            self._proxy_chat(payload)
            return
        self.send_error(404)

    def _proxy_chat(self, payload: dict[str, Any]) -> None:
        lane = _select_lane(payload)
        upstream = EXPERT_URL if lane == "expert" else COORDINATOR_URL
        if not _allowed_upstream(upstream):
            self._json(
                {
                    "error": "invalid_upstream",
                    "lane": lane,
                    "upstream": upstream,
                },
                status=500,
            )
            return
        try:
            body = json.dumps(payload).encode("utf-8")
            request = Request(  # noqa: S310 - upstream is restricted to http/https above.
                upstream,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=PROXY_TIMEOUT) as response:  # noqa: S310
                self.send_response(response.status)
                self.send_header(
                    "Content-Type",
                    response.headers.get("Content-Type", "application/json"),
                )
                self.end_headers()
                self.wfile.write(response.read())
        except URLError as exc:
            self._json(
                {
                    "error": "upstream_unavailable",
                    "lane": lane,
                    "upstream": upstream,
                    "detail": str(exc),
                },
                status=503,
            )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, payload: str, *, status: int = 200) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _advisory_response(payload: dict[str, Any]) -> dict[str, Any]:
    lane = _select_lane(payload)
    return {
        "ok": True,
        "lane": lane,
        "authoritative": False,
        "decision_trace": {
            "controller_authority": "k1s",
            "retrieval_url": QDRANT_URL,
            "symbolic_memory_url": DAS_URL,
            "selected_lane": lane,
        },
        "next_actions": [
            "retrieve local k1s, WorkerBee, Python, and Hyperon context",
            "query DAS facts for runtime state",
            "ask the selected model lane for an advisory answer",
            "record accept/reject reason outside the model response",
        ],
        "request": payload,
    }


def _select_lane(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True).lower()
    expert_terms = ("python", "k1s", "workerbee", "hyperon", "das", "inferencecell", "traceback")
    return "expert" if any(term in text for term in expert_terms) else "coordinator"


def _allowed_upstream(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def main() -> int:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
