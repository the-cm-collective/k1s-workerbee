#!/usr/bin/env python3
"""Record a corpus manifest for the AI fabric retrieval lane."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CORPUS_ROOT = Path(os.getenv("CORPUS_ROOT", "/corpus"))
ARTIFACTS_ROOT = Path(os.getenv("ARTIFACTS_ROOT", "/artifacts"))
SERVE_AFTER_INDEX = os.getenv("AI_INDEXER_SERVE", "").strip().lower() in {"1", "true", "yes"}


def main() -> int:
    result = write_manifest()
    print(json.dumps(result))
    if SERVE_AFTER_INDEX:
        port = int(os.getenv("PORT", "8082"))
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
        server.serve_forever()
    return 0


def write_manifest() -> dict[str, object]:
    ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    documents = []
    for path in sorted(CORPUS_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".py", ".txt", ".yaml", ".yml", ".json", ".toml"}:
            continue
        documents.append(
            {
                "path": str(path.relative_to(CORPUS_ROOT)),
                "bytes": path.stat().st_size,
            }
        )
    manifest = {
        "api_version": "ai-fabric.corpus-manifest/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus_root": str(CORPUS_ROOT),
        "document_count": len(documents),
        "documents": documents,
    }
    target = ARTIFACTS_ROOT / "corpus-manifest.json"
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return {"ok": True, "manifest": str(target), "document_count": len(documents)}


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-fabric-retrieval-indexer/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            payload = {"ok": True, "manifest": str(ARTIFACTS_ROOT / "corpus-manifest.json")}
            body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


if __name__ == "__main__":
    raise SystemExit(main())
