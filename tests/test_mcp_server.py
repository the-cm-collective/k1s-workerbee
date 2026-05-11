from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from workerbee import mcp_server


def test_secret_policy_status_mcp_tool_uses_daemon_status(
    tmp_path,
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    class StopServe(RuntimeError):
        pass

    class FakeMCP:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

        def resource(self, *_args: Any, **_kwargs: Any):
            return self.tool()

        def prompt(self, *_args: Any, **_kwargs: Any):
            return self.tool()

        def run(self, *, transport: str) -> None:
            captured["transport"] = transport
            captured["result"] = self.tools["workerbee_v1_secret_policy_status"]("Alpha")
            raise StopServe

    class FakeDaemon:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def configure_dashboard_lifecycle(self, **_kwargs: Any) -> None:
            pass

        def start(self, *, mcp_bind_url: str | None = None) -> SimpleNamespace:
            captured["mcp_bind_url"] = mcp_bind_url
            return SimpleNamespace(
                dashboard_url="https://dashboard.workerbee.localhost:19443/",
                state_root=str(tmp_path),
                localhost_dns_ok=True,
            )

        def secret_policy_status(self, *, project: str | None = None) -> dict[str, Any]:
            return {
                "ok": True,
                "project": project,
                "secret_policy": {"mode": "sops"},
            }

        def stop_all_projects(self, *, purge: bool = False) -> dict[str, Any]:
            return {"ok": True, "purge": purge}

        def stop_global_ingress(self) -> dict[str, Any]:
            return {"ok": True}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP", FakeMCP)
    monkeypatch.setattr(mcp_server, "WorkerBeeDaemon", FakeDaemon)
    monkeypatch.setattr(mcp_server, "ensure_containerd_privilege", lambda **_kwargs: {})
    monkeypatch.setattr(mcp_server, "containerd_privilege_env", lambda _privilege: {})
    monkeypatch.setattr(
        mcp_server,
        "temporary_containerd_privilege_env",
        lambda _env: nullcontext(),
    )

    with pytest.raises(StopServe):
        mcp_server.serve_mcp(
            project="default",
            runtime="podman",
            state_root=tmp_path,
            port=9876,
        )

    result = captured["result"]
    assert captured["transport"] == "streamable-http"
    assert result["kind"] == "SecretPolicyStatus"
    assert result["project"] == "Alpha"
    assert result["data"]["project"] == "Alpha"
    assert result["data"]["secret_policy"]["mode"] == "sops"
