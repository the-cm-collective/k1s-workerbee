#!/usr/bin/env python3
"""Advisory router for the AI fabric lab."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

COORDINATOR_URL = os.getenv(
    "COORDINATOR_URL",
    "http://ai-coordinator:8001/v1/chat/completions",
)
EXPERT_URL = os.getenv(
    "EXPERT_URL",
    "http://ai-expert:8002/v1/chat/completions",
)
RETRIEVAL_URL = os.getenv("RETRIEVAL_URL", "http://retrieval-indexer:8082")
DAS_URL = os.getenv("DAS_URL", "http://das-bridge:8081")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COORDINATOR_MODEL = os.getenv("COORDINATOR_MODEL", "general-coordinator")
EXPERT_MODEL = os.getenv("EXPERT_MODEL", "k1s-code-expert")
PROXY_TIMEOUT = float(os.getenv("AI_ROUTER_PROXY_TIMEOUT", "120"))
ADVISORY_MODEL_TIMEOUT = float(os.getenv("AI_ROUTER_ADVISORY_MODEL_TIMEOUT", "45"))
RETRIEVAL_TIMEOUT = float(os.getenv("AI_ROUTER_RETRIEVAL_TIMEOUT", "8"))
SYMBOLIC_TIMEOUT = float(os.getenv("AI_ROUTER_SYMBOLIC_TIMEOUT", "8"))
TRACE_DIR = Path(os.getenv("AI_ROUTER_TRACE_DIR", "/data/traces"))


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-fabric-router/0.2"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json(
                {
                    "ok": True,
                    "service": "ai-router",
                    "retrieval_url": RETRIEVAL_URL,
                    "symbolic_memory_url": DAS_URL,
                }
            )
            return
        if parsed.path == "/metrics":
            self._text("ai_fabric_router_up 1\n")
            return
        if parsed.path == "/v1/models":
            lane_values = parse_qs(parsed.query).get("lane") or []
            lane = lane_values[0] if lane_values else None
            payload, status = _models_response(lane=lane)
            self._json(payload, status=status)
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
        upstream_payload = _openai_payload(payload, lane=lane)
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
            with _post_json(upstream, upstream_payload, timeout=PROXY_TIMEOUT) as response:
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
    query = _query_text(payload)
    retrieval = _retrieve_evidence(query, limit=5)
    symbolic = _query_symbolic_evidence(query, limit=5)
    model = _call_advisory_model(lane, payload, retrieval, symbolic)
    answer = model.get("content") if model.get("ok") else None
    trace = _advisory_trace(
        payload=payload,
        lane=lane,
        query=query,
        retrieval=retrieval,
        symbolic=symbolic,
        model=model,
        answer=answer,
    )
    trace_path = _persist_advisory_trace(trace)
    return {
        "ok": True,
        "lane": lane,
        "authoritative": False,
        "trace_id": trace["trace_id"],
        "trace_path": str(trace_path) if trace_path else None,
        "answer": answer,
        "model": model,
        "evidence": {
            "retrieval": retrieval,
            "symbolic": symbolic,
        },
        "decision_trace": trace,
        "next_actions": [
            "treat this as advisory only; k1s remains authoritative",
            "verify retrieved evidence against live controller state before acting",
            "record accept/reject reason outside the model response",
        ],
        "request": payload,
    }


def _advisory_trace(
    *,
    payload: dict[str, Any],
    lane: str,
    query: str,
    retrieval: dict[str, Any],
    symbolic: dict[str, Any],
    model: dict[str, Any],
    answer: Any,
) -> dict[str, Any]:
    trace_id = str(payload.get("trace_id") or f"trace-{uuid.uuid4().hex}")
    request_id = str(payload.get("request_id") or trace_id)
    retrieval_results = (
        retrieval.get("results") if isinstance(retrieval.get("results"), list) else []
    )
    symbolic_results = (
        symbolic.get("results") if isinstance(symbolic.get("results"), list) else []
    )
    now = datetime.now(UTC).isoformat()
    return {
        "trace_id": trace_id,
        "request_id": request_id,
        "api_version": "workerbee.ai-fabric.advisory-trace/v1",
        "created_at": now,
        "authoritative": False,
        "controller_authority": "k1s",
        "selected_lane": lane,
        "query": query,
        "request": payload,
        "request_contract": {
            "subject_type": str(payload.get("subject_type") or "advisory_query"),
            "subject_id": str(payload.get("subject_id") or query[:120]),
            "intent": str(payload.get("intent") or "advise"),
            "facts_ref": str(payload.get("facts_ref") or DAS_URL),
            "locality_snapshot_ref": str(payload.get("locality_snapshot_ref") or RETRIEVAL_URL),
            "max_candidates": int(payload.get("max_candidates") or 5),
            "time_budget_ms": int(
                payload.get("time_budget_ms") or int(ADVISORY_MODEL_TIMEOUT * 1000)
            ),
            "policy_mode": str(payload.get("policy_mode") or "advisory_only"),
        },
        "response_contract": {
            "provider": str(model.get("upstream") or lane),
            "status": "ok" if model.get("ok") else "model_unavailable",
            "recommendation": str(answer or model.get("error") or ""),
            "confidence": None,
            "evidence_refs": [
                *[
                    item.get("path")
                    for item in retrieval_results
                    if isinstance(item, dict) and item.get("path")
                ],
                *[
                    item.get("id")
                    for item in symbolic_results
                    if isinstance(item, dict) and item.get("id")
                ],
            ],
            "authoritative": False,
        },
        "deterministic_baseline": {
            "selected_lane": lane,
            "route_rule": "explicit_lane_or_keyword",
            "retrieval_limit": 5,
            "symbolic_limit": 5,
        },
        "retrieval": retrieval,
        "symbolic": symbolic,
        "model": model,
        "accepted": None,
        "divergence_reason": "pending_operator_review",
        "replay_status": "recorded",
        "continuity_signals": {
            "request_id": request_id,
            "retrieval_backend": retrieval.get("backend"),
            "symbolic_backend": symbolic.get("backend"),
        },
        "coherence_signals": {
            "retrieval_result_count": len(retrieval_results),
            "symbolic_result_count": len(symbolic_results),
            "model_ok": bool(model.get("ok")),
            "authoritative": False,
        },
        "upstreams": {
            "retrieval_url": RETRIEVAL_URL,
            "qdrant_url": QDRANT_URL,
            "symbolic_memory_url": DAS_URL,
            "coordinator_url": COORDINATOR_URL,
            "expert_url": EXPERT_URL,
        },
    }


def _persist_advisory_trace(trace: dict[str, Any]) -> Path | None:
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        trace_id = str(trace.get("trace_id") or f"trace-{uuid.uuid4().hex}")
        path = TRACE_DIR / f"{trace_id}.json"
        path.write_text(json.dumps(trace, indent=2, sort_keys=True), encoding="utf-8")
        return path
    except OSError:
        return None


def _retrieve_evidence(query: str, *, limit: int) -> dict[str, Any]:
    if not _allowed_upstream(RETRIEVAL_URL):
        return {"ok": False, "url": RETRIEVAL_URL, "results": [], "error": "invalid_retrieval_url"}
    try:
        with _post_json(
            f"{RETRIEVAL_URL.rstrip('/')}/v1/search",
            {"query": query, "limit": limit},
            timeout=RETRIEVAL_TIMEOUT,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "url": RETRIEVAL_URL, "results": [], "error": str(exc)}
    if not isinstance(payload, dict):
        return {"ok": False, "url": RETRIEVAL_URL, "results": [], "error": "invalid_response"}
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    return {
        "ok": bool(payload.get("ok")),
        "url": RETRIEVAL_URL,
        "backend": payload.get("backend"),
        "query": query,
        "results": results,
        "error": payload.get("error") or payload.get("qdrant_error"),
    }


def _query_symbolic_evidence(query: str, *, limit: int) -> dict[str, Any]:
    if not _allowed_upstream(DAS_URL):
        return {"ok": False, "url": DAS_URL, "results": [], "error": "invalid_das_url"}
    try:
        with _post_json(
            f"{DAS_URL.rstrip('/')}/v1/query",
            {"query": query, "limit": limit},
            timeout=SYMBOLIC_TIMEOUT,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "url": DAS_URL, "results": [], "error": str(exc)}
    if not isinstance(payload, dict):
        return {"ok": False, "url": DAS_URL, "results": [], "error": "invalid_response"}
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    return {
        "ok": bool(payload.get("ok")),
        "url": DAS_URL,
        "backend": payload.get("backend"),
        "results": results,
        "error": payload.get("error"),
    }


def _call_advisory_model(
    lane: str,
    payload: dict[str, Any],
    retrieval: dict[str, Any],
    symbolic: dict[str, Any],
) -> dict[str, Any]:
    upstream = EXPERT_URL if lane == "expert" else COORDINATOR_URL
    if not _allowed_upstream(upstream):
        return {"ok": False, "lane": lane, "upstream": upstream, "error": "invalid_upstream"}
    messages = [
        {
            "role": "system",
            "content": (
                "You are an advisory assistant for a k1s/WorkerBee development lab. "
                "Do not claim authority over controller state. Summarize the answer, cite "
                "retrieved paths when useful, and call out verification steps."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "request": payload,
                    "retrieval_evidence": retrieval.get("results") or [],
                    "symbolic_evidence": symbolic.get("results") or [],
                    "controller_authority": "k1s",
                },
                indent=2,
                sort_keys=True,
            ),
        },
    ]
    model_payload = {
        "model": EXPERT_MODEL if lane == "expert" else COORDINATOR_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 512,
    }
    try:
        with _post_json(upstream, model_payload, timeout=ADVISORY_MODEL_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except (URLError, TimeoutError) as exc:
        return {"ok": False, "lane": lane, "upstream": upstream, "error": str(exc)}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "lane": lane, "upstream": upstream, "error": "invalid_model_json"}
    return {
        "ok": True,
        "lane": lane,
        "upstream": upstream,
        "content": _chat_content(data),
        "raw": data,
    }


def _chat_content(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return None


def _models_response(*, lane: str | None = None) -> tuple[dict[str, Any], int]:
    upstreams = {
        "coordinator": COORDINATOR_URL,
        "expert": EXPERT_URL,
    }
    if lane is not None and lane not in upstreams:
        return {"ok": False, "error": "invalid_lane", "lane": lane}, 400

    selected = {lane: upstreams[lane]} if lane else upstreams
    lanes: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    data: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for lane_name, chat_url in selected.items():
        models_url = _models_url(chat_url)
        if not _allowed_upstream(models_url):
            errors[lane_name] = {"url": models_url, "error": "invalid_upstream"}
            continue
        payload = _get_json(models_url, timeout=PROXY_TIMEOUT)
        lanes[lane_name] = payload
        if not payload.get("ok") and payload.get("error"):
            errors[lane_name] = {"url": models_url, "error": payload.get("error")}
            continue
        for item in _model_entries(payload):
            model_id = str(item.get("id") or "")
            if model_id and model_id in seen_ids:
                continue
            if model_id:
                seen_ids.add(model_id)
            data.append(item)

    ok = not errors and bool(data)
    status = 200 if ok else 503
    return {
        "ok": ok,
        "object": "list",
        "data": data,
        "lanes": lanes,
        "errors": errors,
    }, status


def _model_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _models_url(chat_url: str) -> str:
    parsed = urlparse(chat_url)
    return parsed._replace(path="/v1/models", query="", fragment="").geturl()


def _query_text(payload: dict[str, Any]) -> str:
    for key in ("query", "question", "prompt", "input"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    messages = payload.get("messages")
    if isinstance(messages, list):
        text = "\n".join(
            str(item.get("content"))
            for item in messages
            if isinstance(item, dict) and item.get("content")
        )
        if text.strip():
            return text.strip()
    return json.dumps(payload, sort_keys=True)


def _select_lane(payload: dict[str, Any]) -> str:
    explicit = payload.get("lane") or payload.get("target_lane") or payload.get("route_lane")
    if isinstance(explicit, str) and explicit.lower() in {"coordinator", "expert"}:
        return explicit.lower()
    text = json.dumps(payload, sort_keys=True).lower()
    expert_terms = ("python", "k1s", "workerbee", "hyperon", "das", "inferencecell", "traceback")
    return "expert" if any(term in text for term in expert_terms) else "coordinator"


def _openai_payload(payload: dict[str, Any], *, lane: str) -> dict[str, Any]:
    upstream_payload = dict(payload)
    for key in ("lane", "target_lane", "route_lane"):
        upstream_payload.pop(key, None)
    upstream_payload.setdefault("model", EXPERT_MODEL if lane == "expert" else COORDINATOR_MODEL)
    return upstream_payload


def _post_json(url: str, payload: dict[str, Any], *, timeout: float):
    body = json.dumps(payload).encode("utf-8")
    request = Request(  # noqa: S310 - lab service URLs are constrained by configuration.
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urlopen(request, timeout=timeout)  # noqa: S310


def _get_json(url: str, *, timeout: float) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            payload = json.loads(raw)
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    if not isinstance(payload, dict):
        return {"ok": False, "url": url, "error": "invalid_json"}
    payload.setdefault("ok", True)
    return payload


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
