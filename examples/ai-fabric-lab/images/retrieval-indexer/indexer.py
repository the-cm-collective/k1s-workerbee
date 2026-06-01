#!/usr/bin/env python3
"""Index the AI fabric corpus and serve retrieval evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CORPUS_ROOT = Path(os.getenv("CORPUS_ROOT", "/corpus"))
ARTIFACTS_ROOT = Path(os.getenv("ARTIFACTS_ROOT", "/artifacts"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
COLLECTION = os.getenv("AI_RETRIEVAL_COLLECTION", "ai_fabric_corpus")
VECTOR_SIZE = int(os.getenv("AI_RETRIEVAL_VECTOR_SIZE", "384"))
CHUNK_CHARS = int(os.getenv("AI_RETRIEVAL_CHUNK_CHARS", "1800"))
MAX_FILE_BYTES = int(os.getenv("AI_RETRIEVAL_MAX_FILE_BYTES", str(256 * 1024)))
QDRANT_TIMEOUT = float(os.getenv("AI_RETRIEVAL_QDRANT_TIMEOUT", "10"))
SERVE_AFTER_INDEX = os.getenv("AI_INDEXER_SERVE", "").strip().lower() in {"1", "true", "yes"}

DOCUMENT_SUFFIXES = {".md", ".py", ".txt", ".yaml", ".yml", ".json", ".toml"}
TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")

INDEX_STATE: dict[str, Any] = {}
INDEXED_CHUNKS: list[dict[str, Any]] = []


def main() -> int:
    result = build_index()
    print(json.dumps(result, sort_keys=True))
    if SERVE_AFTER_INDEX:
        port = int(os.getenv("PORT", "8082"))
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
        server.serve_forever()
    return 0 if result.get("ok") else 1


def build_index() -> dict[str, Any]:
    ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    documents = discover_documents(CORPUS_ROOT)
    chunks = chunk_documents(documents)
    qdrant_result = upsert_qdrant(chunks)
    manifest = write_manifest(documents, chunks, qdrant_result)
    result = {
        "ok": True,
        "manifest": str(manifest),
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "collection": COLLECTION,
        "qdrant_indexed": qdrant_result["ok"],
        "qdrant_error": qdrant_result.get("error"),
    }
    INDEXED_CHUNKS.clear()
    INDEXED_CHUNKS.extend(chunks)
    INDEX_STATE.clear()
    INDEX_STATE.update(result)
    return result


def discover_documents(root: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    if not root.exists():
        return documents
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in DOCUMENT_SUFFIXES:
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(root)
        documents.append(
            {
                "path": str(relative),
                "source": relative.parts[0] if relative.parts else "corpus",
                "bytes": size,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text,
            }
        )
    return documents


def chunk_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for document in documents:
        parts = _chunk_text(str(document["text"]))
        for chunk_id, text in enumerate(parts):
            point_id = _point_id(str(document["path"]), chunk_id, text)
            chunks.append(
                {
                    "id": point_id,
                    "source": document["source"],
                    "path": document["path"],
                    "chunk_id": chunk_id,
                    "text": text,
                    "document_sha256": document["sha256"],
                    "vector": embed_text(text, VECTOR_SIZE),
                }
            )
    return chunks


def write_manifest(
    documents: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    qdrant_result: dict[str, Any],
) -> Path:
    manifest_documents = [
        {
            "path": document["path"],
            "source": document["source"],
            "bytes": document["bytes"],
            "sha256": document["sha256"],
        }
        for document in documents
    ]
    manifest = {
        "api_version": "ai-fabric.corpus-manifest/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus_root": str(CORPUS_ROOT),
        "collection": COLLECTION,
        "vector_size": VECTOR_SIZE,
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "qdrant_indexed": qdrant_result["ok"],
        "qdrant_error": qdrant_result.get("error"),
        "documents": manifest_documents,
    }
    target = ARTIFACTS_ROOT / "corpus-manifest.json"
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return target


def _chunk_text(text: str) -> list[str]:
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not normalized:
        return []
    paragraphs = re.split(r"\n{2,}", normalized)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= CHUNK_CHARS:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= CHUNK_CHARS:
            current = paragraph
            continue
        for index in range(0, len(paragraph), CHUNK_CHARS):
            chunk = paragraph[index : index + CHUNK_CHARS].strip()
            if chunk:
                chunks.append(chunk)
        current = ""
    if current:
        chunks.append(current)
    return chunks


def embed_text(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    tokens = TOKEN_RE.findall(text.lower())
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        weight = 1.0 + min(len(token), 24) / 24.0
        vector[index] += sign * weight
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return vector
    return [value / magnitude for value in vector]


def search(query: str, *, limit: int = 5) -> dict[str, Any]:
    vector = embed_text(query, VECTOR_SIZE)
    qdrant = search_qdrant(vector, limit=limit)
    if qdrant["ok"]:
        return {"ok": True, "backend": "qdrant", "results": qdrant["results"]}
    local = search_local(query, vector, limit=limit)
    return {
        "ok": True,
        "backend": "local",
        "results": local,
        "qdrant_error": qdrant.get("error"),
    }


def search_local(query: str, vector: list[float], *, limit: int = 5) -> list[dict[str, Any]]:
    query_terms = set(TOKEN_RE.findall(query.lower()))
    scored = []
    for chunk in INDEXED_CHUNKS:
        dense_score = _cosine(vector, chunk["vector"])
        text_terms = set(TOKEN_RE.findall(str(chunk["text"]).lower()))
        sparse_score = len(query_terms & text_terms) / max(len(query_terms), 1)
        score = dense_score + sparse_score
        scored.append((score, chunk))
    results = []
    for score, chunk in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]:
        results.append(_result_payload(chunk, score=score))
    return results


def upsert_qdrant(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    if not chunks:
        return {"ok": True, "points": 0}
    try:
        ensure_qdrant_collection()
        for index in range(0, len(chunks), 64):
            batch = chunks[index : index + 64]
            points = [
                {
                    "id": chunk["id"],
                    "vector": chunk["vector"],
                    "payload": _qdrant_payload(chunk),
                }
                for chunk in batch
            ]
            qdrant_request(
                "PUT",
                f"/collections/{COLLECTION}/points?wait=true",
                {"points": points},
            )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "points": len(chunks)}


def ensure_qdrant_collection() -> None:
    try:
        payload = qdrant_request("GET", f"/collections/{COLLECTION}", None)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        payload = {}
    vector_size = _qdrant_vector_size(payload)
    if vector_size == VECTOR_SIZE:
        return
    if vector_size is not None:
        qdrant_request("DELETE", f"/collections/{COLLECTION}", None)
    qdrant_request(
        "PUT",
        f"/collections/{COLLECTION}",
        {"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}},
    )


def search_qdrant(vector: list[float], *, limit: int) -> dict[str, Any]:
    try:
        payload = qdrant_request(
            "POST",
            f"/collections/{COLLECTION}/points/search",
            {"vector": vector, "limit": limit, "with_payload": True},
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    results = []
    for item in payload.get("result") or []:
        chunk = item.get("payload") if isinstance(item, dict) else {}
        if isinstance(chunk, dict):
            results.append(_result_payload(chunk, score=float(item.get("score") or 0.0)))
    return {"ok": True, "results": results}


def qdrant_request(method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(  # noqa: S310 - QDRANT_URL is configured inside the lab network.
        f"{QDRANT_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=QDRANT_TIMEOUT) as response:  # noqa: S310
        body = response.read()
    if not body:
        return {}
    decoded = json.loads(body.decode("utf-8"))
    return decoded if isinstance(decoded, dict) else {}


def _qdrant_vector_size(payload: dict[str, Any]) -> int | None:
    vectors = (((payload.get("result") or {}).get("config") or {}).get("params") or {}).get(
        "vectors"
    )
    if isinstance(vectors, dict) and isinstance(vectors.get("size"), int):
        return int(vectors["size"])
    return None


def _qdrant_payload(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": chunk["source"],
        "path": chunk["path"],
        "chunk_id": chunk["chunk_id"],
        "text": chunk["text"],
        "document_sha256": chunk["document_sha256"],
    }


def _result_payload(chunk: dict[str, Any], *, score: float) -> dict[str, Any]:
    return {
        "source": chunk.get("source"),
        "path": chunk.get("path"),
        "chunk_id": chunk.get("chunk_id"),
        "score": round(score, 6),
        "text": chunk.get("text"),
    }


def _point_id(path: str, chunk_id: int, text: str) -> str:
    digest = hashlib.sha256(f"{path}:{chunk_id}:{text}".encode()).hexdigest()
    return str(uuid.UUID(hex=digest[:32]))


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-fabric-retrieval-indexer/0.2"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json({"ok": True, "service": "retrieval-indexer", **INDEX_STATE})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/search":
            payload = self._read_json()
            query = str(payload.get("query") or "").strip()
            limit = max(1, min(int(payload.get("limit") or 5), 20))
            if not query:
                self._json({"ok": False, "error": "query_required"}, status=400)
                return
            self._json({"query": query, **search(query, limit=limit)})
            return
        if self.path == "/v1/reindex":
            self._json(build_index())
            return
        self.send_error(404)

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


if __name__ == "__main__":
    raise SystemExit(main())
