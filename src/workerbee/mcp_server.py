"""MCP server adapter for WorkerBee."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workerbee.daemon import WorkerBeeDaemon


def serve_mcp(
    *,
    project: str = "default",
    runtime: str = "auto",
    host: str = "127.0.0.1",
    port: int = 8765,
    state_dir: Path | None = None,
    state_root: Path | None = None,
) -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - depends on optional runtime install
        raise RuntimeError(
            "The MCP SDK is not installed. Install with `python -m pip install -e .`."
        ) from exc

    if state_dir is not None and state_root is not None:
        raise RuntimeError("use either --state-dir or --state-root, not both")
    daemon = WorkerBeeDaemon(
        state_root=state_root or state_dir,
        runtime=runtime,
        default_project=project,
    )
    ingress = daemon.start()
    print(f"WorkerBee global dashboard: {ingress.dashboard_url}", flush=True)
    print(f"WorkerBee state root: {ingress.state_root}", flush=True)
    if not ingress.localhost_dns_ok:
        print("WorkerBee warning: *.localhost DNS did not resolve on this host", flush=True)

    mcp = FastMCP("K1S WorkerBee", host=host, port=port, json_response=True)

    @mcp.tool()
    def workerbee_start(project: str = "default") -> dict[str, Any]:
        """Start or return the local WorkerBee k1s stack."""
        return daemon.with_project(project, lambda supervisor: supervisor.start().public_dict())

    @mcp.tool()
    def workerbee_dashboard(project: str = "default") -> dict[str, Any]:
        """Return dashboard and API URLs for a WorkerBee project stack."""
        current = daemon.with_project(project, lambda supervisor: supervisor.start())
        return {
            "dashboard_url": current.dashboard_url,
            "controller_url": current.controller_url,
            "apishim_url": current.apishim_url,
            "ingress": current.ingress,
        }

    @mcp.tool()
    def workerbee_global_dashboard() -> dict[str, Any]:
        """Return the global WorkerBee dashboard URL and ingress status."""
        return daemon.global_dashboard()

    @mcp.tool()
    def workerbee_projects() -> dict[str, Any]:
        """Return all project-scoped WorkerBee stacks known to this MCP daemon."""
        return daemon.projects()

    @mcp.tool()
    def workerbee_project_status(project: str) -> dict[str, Any]:
        """Return status for a single project-scoped WorkerBee stack."""
        return daemon.project_status(project)

    @mcp.tool()
    def workerbee_deploy_poc_stack(project: str = "default") -> dict[str, Any]:
        """Build and deploy the representative WorkerBee POC app stack."""
        return daemon.with_project(project, lambda supervisor: supervisor.deploy_poc_stack())

    @mcp.tool()
    def workerbee_build_image(
        context: str, tag: str | None = None, project: str = "default"
    ) -> dict[str, Any]:
        """Build a local image with the configured runtime."""
        return daemon.with_project(
            project,
            lambda supervisor: supervisor.build_image(Path(context), tag=tag),
        )

    @mcp.tool()
    def workerbee_deploy_manifest(
        path: str,
        namespace: str | None = None,
        timeout: int = 180,
        project: str = "default",
    ) -> dict[str, Any]:
        """Apply a native ae.dev/v1alpha1 k1s manifest."""
        return daemon.with_project(
            project,
            lambda supervisor: supervisor.deploy_manifest(
                Path(path),
                namespace=namespace,
                timeout=timeout,
            ),
        )

    @mcp.tool()
    def workerbee_status(project: str = "default") -> dict[str, Any]:
        """Return WorkerBee stack status."""
        return daemon.with_project(project, lambda supervisor: supervisor.status())

    @mcp.tool()
    def workerbee_tls_info(project: str = "default") -> dict[str, Any]:
        """Return local API shim TLS paths and trust guidance."""
        return daemon.with_project(project, lambda supervisor: supervisor.tls_info())

    @mcp.tool()
    def workerbee_poc_status(project: str = "default") -> dict[str, Any]:
        """Return POC app status through the native k1s API."""
        return daemon.with_project(project, lambda supervisor: supervisor.poc_status())

    @mcp.tool()
    def workerbee_logs(
        app: str = "api", tail: int = 80, project: str = "default"
    ) -> dict[str, Any]:
        """Return recent logs for a POC app."""
        return daemon.with_project(
            project,
            lambda supervisor: supervisor.logs(app=app, tail=tail),
        )

    @mcp.tool()
    def workerbee_exec(app: str, command: list[str], project: str = "default") -> dict[str, Any]:
        """Run a bounded command inside a POC app container."""
        return daemon.with_project(
            project,
            lambda supervisor: supervisor.run_exec(app, command),
        )

    @mcp.tool()
    def workerbee_apishim_smoke(project: str = "default") -> dict[str, Any]:
        """Inspect POC resources through the Kubernetes API shim."""
        return daemon.with_project(project, lambda supervisor: supervisor.apishim_smoke())

    @mcp.tool()
    def workerbee_export_k8s(project: str = "default") -> dict[str, Any]:
        """Export POC app manifests to Kubernetes YAML artifacts."""
        return daemon.with_project(project, lambda supervisor: supervisor.export_k8s())

    @mcp.tool()
    def workerbee_stop(purge: bool = False, project: str = "default") -> dict[str, Any]:
        """Stop WorkerBee-managed processes and optionally purge local state."""
        return daemon.with_project(project, lambda supervisor: supervisor.stop(purge=purge))

    @mcp.tool()
    def workerbee_reset(project: str = "default") -> dict[str, Any]:
        """Reset WorkerBee project workloads and generated artifacts."""
        return daemon.with_project(project, lambda supervisor: supervisor.reset())

    mcp.run(transport="streamable-http")
