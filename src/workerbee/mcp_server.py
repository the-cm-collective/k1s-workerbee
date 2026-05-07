"""MCP server adapter for WorkerBee."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workerbee.supervisor import WorkerBeeSupervisor


def serve_mcp(
    *,
    project: str = "default",
    runtime: str = "auto",
    host: str = "127.0.0.1",
    port: int = 8765,
    state_dir: Path | None = None,
) -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - depends on optional runtime install
        raise RuntimeError(
            "The MCP SDK is not installed. Install with `python -m pip install -e .`."
        ) from exc

    supervisor = WorkerBeeSupervisor(project=project, runtime=runtime, state_dir=state_dir)
    info = supervisor.start()
    print(f"WorkerBee dashboard: {info.dashboard_url}", flush=True)
    print(f"WorkerBee controller: {info.controller_url}", flush=True)
    print(f"WorkerBee API shim: {info.apishim_url}", flush=True)

    mcp = FastMCP("K1S WorkerBee", host=host, port=port, json_response=True)

    @mcp.tool()
    def workerbee_start() -> dict[str, Any]:
        """Start or return the local WorkerBee k1s stack."""
        return supervisor.start().public_dict()

    @mcp.tool()
    def workerbee_dashboard() -> dict[str, Any]:
        """Return dashboard and API URLs for the current WorkerBee stack."""
        current = supervisor.start()
        return {
            "dashboard_url": current.dashboard_url,
            "controller_url": current.controller_url,
            "apishim_url": current.apishim_url,
        }

    @mcp.tool()
    def workerbee_deploy_poc_stack() -> dict[str, Any]:
        """Build and deploy the representative WorkerBee POC app stack."""
        return supervisor.deploy_poc_stack()

    @mcp.tool()
    def workerbee_status() -> dict[str, Any]:
        """Return WorkerBee stack status."""
        return supervisor.status()

    @mcp.tool()
    def workerbee_poc_status() -> dict[str, Any]:
        """Return POC app status through the native k1s API."""
        return supervisor.poc_status()

    @mcp.tool()
    def workerbee_logs(app: str = "api", tail: int = 80) -> dict[str, Any]:
        """Return recent logs for a POC app."""
        return supervisor.logs(app=app, tail=tail)

    @mcp.tool()
    def workerbee_exec(app: str, command: list[str]) -> dict[str, Any]:
        """Run a bounded command inside a POC app container."""
        return supervisor.run_exec(app, command)

    @mcp.tool()
    def workerbee_apishim_smoke() -> dict[str, Any]:
        """Inspect POC resources through the Kubernetes API shim."""
        return supervisor.apishim_smoke()

    @mcp.tool()
    def workerbee_export_k8s() -> dict[str, Any]:
        """Export POC app manifests to Kubernetes YAML artifacts."""
        return supervisor.export_k8s()

    @mcp.tool()
    def workerbee_stop(purge: bool = False) -> dict[str, Any]:
        """Stop WorkerBee-managed processes and optionally purge local state."""
        return supervisor.stop(purge=purge)

    mcp.run(transport="streamable-http")
