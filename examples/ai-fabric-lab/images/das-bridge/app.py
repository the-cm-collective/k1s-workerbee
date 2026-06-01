#!/usr/bin/env python3
"""Small file-backed DAS bridge placeholder for the AI fabric lab."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.getenv("DAS_DATA_DIR", "/data/das"))
FACT_LOG = DATA_DIR / "facts.jsonl"


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-fabric-das-bridge/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json({"ok": True, "service": "das-bridge"})
            return
        if self.path == "/v1/facts":
            self._json({"facts": _read_facts()})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        if self.path == "/v1/facts":
            fact = _append_fact(payload)
            self._json({"ok": True, "fact": fact}, status=201)
            return
        if self.path == "/v1/query":
            self._json({"ok": True, "facts": _query_facts(payload)})
            return
        self.send_error(404)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
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


def _append_fact(payload: dict[str, Any]) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fact = {
        "namespace": str(payload.get("namespace") or "runtime"),
        "subject": str(payload.get("subject") or ""),
        "predicate": str(payload.get("predicate") or ""),
        "object": payload.get("object"),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    with FACT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(fact, sort_keys=True) + "\n")
    return fact


def _read_facts() -> list[dict[str, Any]]:
    if not FACT_LOG.is_file():
        return []
    facts = []
    for line in FACT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            facts.append(value)
    return facts


def _query_facts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    namespace = payload.get("namespace")
    subject = payload.get("subject")
    return [
        fact
        for fact in _read_facts()
        if (namespace is None or fact.get("namespace") == namespace)
        and (subject is None or fact.get("subject") == subject)
    ]


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("PORT", "8081"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
