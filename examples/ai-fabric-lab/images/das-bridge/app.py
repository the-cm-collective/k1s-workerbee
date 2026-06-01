#!/usr/bin/env python3
"""Hyperon DAS-backed symbolic evidence bridge for the AI fabric lab."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DATA_DIR = Path(os.getenv("DAS_DATA_DIR", "/data/das"))
FACT_LOG = DATA_DIR / "facts.jsonl"
BACKEND_NAME = os.getenv("AI_DAS_BACKEND", "hyperon-das")
FACT_NODE_TYPE = "Concept"
PREDICATE_NODE_TYPE = "Predicate"
FACT_LINK_TYPE = "ai-fabric:fact"
TOKEN_RE = re.compile(r"[a-z0-9_.:-]+")
QUERY_STOPWORDS = {
    "about",
    "and",
    "for",
    "from",
    "how",
    "is",
    "the",
    "this",
    "what",
    "with",
}


class HyperonDASBackend:
    def __init__(self, backend_name: str) -> None:
        self.name = backend_name
        self.ready = False
        self.error: str | None = None
        self.about: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._das: Any | None = None
        self._node_type: Any | None = None
        self._link_type: Any | None = None
        self._initialize()

    def _initialize(self) -> None:
        if self.name != "hyperon-das":
            self.error = f"unsupported backend: {self.name}"
            return
        try:
            from hyperon_das import DistributedAtomSpace
            from hyperon_das_atomdb.database import LinkT, NodeT
        except Exception as exc:  # pragma: no cover - exercised in runtime image.
            self.error = str(exc)
            return
        try:
            self._das = DistributedAtomSpace()
            self._node_type = NodeT
            self._link_type = LinkT
            self.about = DistributedAtomSpace.about()
            self.ready = True
        except Exception as exc:  # pragma: no cover - defensive runtime path.
            self.error = str(exc)

    def replay(self, facts: list[dict[str, Any]]) -> None:
        for fact in facts:
            self.add_fact(fact)

    def add_fact(self, fact: dict[str, Any]) -> dict[str, Any]:
        if (
            not self.ready
            or self._das is None
            or self._node_type is None
            or self._link_type is None
        ):
            return {"ok": False, "error": self.error or "backend_unavailable"}
        with self._lock:
            try:
                subject = self._das.add_node(
                    self._node_type(
                        type=FACT_NODE_TYPE,
                        name=_node_name(fact["namespace"], fact["subject"]),
                        custom_attributes={"namespace": str(fact["namespace"])},
                    )
                )
                predicate = self._das.add_node(
                    self._node_type(type=PREDICATE_NODE_TYPE, name=str(fact["predicate"]))
                )
                obj = self._das.add_node(
                    self._node_type(type=FACT_NODE_TYPE, name=_object_name(fact["object"]))
                )
                link = self._das.add_link(
                    self._link_type(
                        type=FACT_LINK_TYPE,
                        targets=[predicate, subject, obj],
                        custom_attributes={"fact_id": str(fact["id"])},
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                return {"ok": False, "error": str(exc)}
            try:
                self._das.commit_changes()
                commit = {"ok": True}
            except NotImplementedError as exc:  # pragma: no cover - runtime package variance.
                commit = {"ok": False, "error": str(exc) or "not_implemented"}
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                commit = {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "commit": commit,
            "subject_handle": getattr(subject, "handle", None),
            "predicate_handle": getattr(predicate, "handle", None),
            "object_handle": getattr(obj, "handle", None),
            "link_handle": getattr(link, "handle", None),
        }

    def status(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "ready": self.ready,
            "error": self.error,
            "about": self.about,
        }
        if self.ready and self._das is not None:
            try:
                payload["atoms"] = self._das.count_atoms({"precise": True})
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                payload["atom_error"] = str(exc)
        return payload


BACKEND = HyperonDASBackend(BACKEND_NAME)
FACT_LOCK = threading.RLock()


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-fabric-das-bridge/0.2"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json(
                {
                    "ok": True,
                    "service": "das-bridge",
                    "backend": BACKEND.status(),
                    "fact_count": len(_read_facts()),
                }
            )
            return
        if parsed.path == "/v1/facts":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["100"])[0])
            self._json({"ok": True, "facts": _read_facts(limit=limit)})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        payload = self._read_json()
        if parsed.path == "/v1/facts":
            fact = _append_fact(payload)
            backend = BACKEND.add_fact(fact)
            self._json({"ok": True, "fact": fact, "backend": backend}, status=201)
            return
        if parsed.path == "/v1/query":
            facts = _query_facts(payload)
            self._json(
                {
                    "ok": True,
                    "backend": BACKEND.status(),
                    "facts": facts,
                    "results": facts,
                }
            )
            return
        if parsed.path == "/v1/import/runtime":
            facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
            imported = [_append_fact(item) for item in facts if isinstance(item, dict)]
            for fact in imported:
                BACKEND.add_fact(fact)
            self._json({"ok": True, "imported": len(imported), "facts": imported})
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
    fact = _normalize_fact(payload)
    with FACT_LOCK, FACT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(fact, sort_keys=True) + "\n")
    return fact


def _normalize_fact(payload: dict[str, Any]) -> dict[str, Any]:
    fact = {
        "namespace": str(payload.get("namespace") or "runtime"),
        "subject": str(payload.get("subject") or ""),
        "predicate": str(payload.get("predicate") or "observed"),
        "object": payload.get("object"),
        "source": str(payload.get("source") or "api"),
        "recorded_at": str(
            payload.get("recorded_at") or datetime.now(timezone.utc).isoformat()  # noqa: UP017
        ),
    }
    fact["id"] = _fact_id(fact)
    return fact


def _read_facts(*, limit: int | None = None) -> list[dict[str, Any]]:
    if not FACT_LOG.is_file():
        return []
    facts = []
    with FACT_LOCK:
        lines = FACT_LOG.read_text(encoding="utf-8").splitlines()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            facts.append(value)
    if limit is not None:
        return facts[-max(0, limit) :]
    return facts


def _query_facts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    namespace = payload.get("namespace")
    subject = payload.get("subject")
    predicate = payload.get("predicate")
    text = str(payload.get("query") or "").lower()
    terms = [term for term in TOKEN_RE.findall(text) if term not in QUERY_STOPWORDS]
    limit = max(1, min(int(payload.get("limit") or 10), 100))
    matches = []
    for index, fact in enumerate(_read_facts()):
        if namespace is not None and fact.get("namespace") != namespace:
            continue
        if subject is not None and fact.get("subject") != subject:
            continue
        if predicate is not None and fact.get("predicate") != predicate:
            continue
        fact_text = json.dumps(fact, sort_keys=True).lower()
        score = sum(1 for term in terms if term in fact_text)
        if terms and score == 0:
            continue
        matches.append((score, index, fact))
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [fact for _, _, fact in matches[:limit]]


def _fact_id(fact: dict[str, Any]) -> str:
    stable = {
        "namespace": fact["namespace"],
        "subject": fact["subject"],
        "predicate": fact["predicate"],
        "object": fact["object"],
        "source": fact["source"],
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _node_name(namespace: str, subject: str) -> str:
    return f"{namespace}:{subject}"


def _object_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKEND.replay(_read_facts())
    port = int(os.getenv("PORT", "8081"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
