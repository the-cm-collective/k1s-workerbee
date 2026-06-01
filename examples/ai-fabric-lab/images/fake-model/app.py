#!/usr/bin/env python3
"""OpenAI-compatible fake model for GPU-free AI fabric plumbing tests."""

from __future__ import annotations

import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_ID = os.getenv("AI_FAKE_MODEL_ID", "ai-fabric-fake-model")
PORT = int(os.getenv("AI_FAKE_MODEL_PORT", "8090"))


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-fabric-fake-model/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json({"ok": True, "service": "fake-model", "model": MODEL_ID})
            return
        if self.path == "/v1/models":
            self._json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_ID,
                            "object": "model",
                            "created": 0,
                            "owned_by": "workerbee",
                        }
                    ],
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        payload = self._read_json()
        completion = _chat_completion(payload)
        self._json(completion)

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


def _chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    model = str(payload.get("model") or MODEL_ID)
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    prompt = _last_user_message(messages)
    content = (
        "Fake advisory model response. "
        "This validates router, retrieval, DAS, and trace plumbing only. "
        f"model={model}; prompt={prompt[:180]}"
    )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": len(content.split()),
            "total_tokens": len(prompt.split()) + len(content.split()),
        },
    }


def _last_user_message(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # noqa: S104
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
