#!/usr/bin/env python3
"""Record a corpus manifest for the AI fabric retrieval lane."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

CORPUS_ROOT = Path(os.getenv("CORPUS_ROOT", "/corpus"))
ARTIFACTS_ROOT = Path(os.getenv("ARTIFACTS_ROOT", "/artifacts"))


def main() -> int:
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
    print(json.dumps({"ok": True, "manifest": str(target), "document_count": len(documents)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
