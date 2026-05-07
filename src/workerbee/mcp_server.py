"""MCP server adapter for WorkerBee v1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workerbee.contract import protect
from workerbee.daemon import WorkerBeeDaemon
from workerbee.manifests import (
    deploy_local_stage,
    deploy_remote_k1s_stage,
    export_bundle,
    prepare_stage,
    validate_stage,
)
from workerbee.trust import trust_install, trust_status, trust_uninstall


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
    ingress = daemon.start(mcp_bind_url=f"http://{host}:{port}/mcp")
    print(f"WorkerBee global dashboard: {ingress.dashboard_url}", flush=True)
    print(f"WorkerBee state root: {ingress.state_root}", flush=True)
    if not ingress.localhost_dns_ok:
        print("WorkerBee warning: *.localhost DNS did not resolve on this host", flush=True)

    mcp = FastMCP("K1S WorkerBee", host=host, port=port, json_response=True)

    @mcp.tool()
    def workerbee_v1_capabilities() -> dict[str, Any]:
        """Return WorkerBee v1 MCP contract capabilities."""
        return protect("Capabilities", None, daemon.capabilities)

    @mcp.tool()
    def workerbee_v1_projects() -> dict[str, Any]:
        """Return all project-scoped WorkerBee stacks known to this MCP daemon."""
        return protect("Projects", None, daemon.projects)

    @mcp.tool()
    def workerbee_v1_project_start(project: str = "default") -> dict[str, Any]:
        """Start or return a project-local WorkerBee k1s stack."""
        return protect(
            "ProjectStart",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: supervisor.start().public_dict(),
            ),
        )

    @mcp.tool()
    def workerbee_v1_project_status(project: str = "default") -> dict[str, Any]:
        """Return project stack status."""
        return protect("ProjectStatus", project, lambda: daemon.project_status(project))

    @mcp.tool()
    def workerbee_v1_project_stop(
        purge: bool = False,
        project: str = "default",
    ) -> dict[str, Any]:
        """Stop project processes and optionally purge project state."""
        return protect(
            "ProjectStop",
            project,
            lambda: daemon.with_project(project, lambda supervisor: supervisor.stop(purge=purge)),
        )

    @mcp.tool()
    def workerbee_v1_project_reset(project: str = "default") -> dict[str, Any]:
        """Reset project workloads and generated artifacts."""
        return protect(
            "ProjectReset",
            project,
            lambda: daemon.with_project(project, lambda supervisor: supervisor.reset()),
        )

    @mcp.tool()
    def workerbee_v1_logs(
        app: str = "api",
        tail: int = 80,
        project: str = "default",
    ) -> dict[str, Any]:
        """Return recent logs for an app."""
        return protect(
            "Logs",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: supervisor.logs(app=app, tail=tail),
            ),
        )

    @mcp.tool()
    def workerbee_v1_exec(
        app: str,
        command: list[str],
        project: str = "default",
    ) -> dict[str, Any]:
        """Run a bounded command inside an app container."""
        return protect(
            "Exec",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: supervisor.run_exec(app, command),
            ),
        )

    @mcp.tool()
    def workerbee_v1_image_build(
        context: str,
        tag: str | None = None,
        project: str = "default",
    ) -> dict[str, Any]:
        """Build a local image context."""
        return protect(
            "ImageBuild",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: supervisor.build_image(Path(context), tag=tag),
            ),
        )

    @mcp.tool()
    def workerbee_v1_manifest_prepare(
        name: str = "app",
        template: str = "frontend-api-store",
        source: str | None = None,
        project: str = "default",
    ) -> dict[str, Any]:
        """Generate editable staged native k1s files or copy a YAML source."""
        return protect(
            "ManifestPrepare",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: prepare_stage(
                    supervisor=supervisor,
                    name=name,
                    template=template,
                    source=Path(source) if source else None,
                ),
            ),
        )

    @mcp.tool()
    def workerbee_v1_manifest_validate(stage: str, project: str = "default") -> dict[str, Any]:
        """Validate staged manifest files."""
        return protect("ManifestValidate", project, lambda: validate_stage(Path(stage)))

    @mcp.tool()
    def workerbee_v1_manifest_deploy_local(
        stage: str,
        namespace: str | None = None,
        timeout: int = 180,
        project: str = "default",
    ) -> dict[str, Any]:
        """Apply staged native k1s or practical Kubernetes manifests locally."""
        return protect(
            "ManifestDeployLocal",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: deploy_local_stage(
                    supervisor=supervisor,
                    stage_dir=Path(stage),
                    namespace=namespace,
                    timeout=timeout,
                ),
            ),
        )

    @mcp.tool()
    def workerbee_v1_manifest_deploy_remote_k1s(
        stage: str,
        server: str,
        token: str,
        namespace: str | None = None,
        timeout: int = 180,
        project: str = "default",
    ) -> dict[str, Any]:
        """Apply staged native k1s or practical Kubernetes manifests to remote k1s."""
        return protect(
            "ManifestDeployRemoteK1s",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: deploy_remote_k1s_stage(
                    supervisor=supervisor,
                    stage_dir=Path(stage),
                    server=server,
                    token=token,
                    namespace=namespace,
                    timeout=timeout,
                ),
            ),
        )

    @mcp.tool()
    def workerbee_v1_bundle_export(
        stage: str,
        format: str = "k1s",  # noqa: A002 - MCP-facing field name
        namespace: str | None = None,
        project: str = "default",
    ) -> dict[str, Any]:
        """Export staged artifacts as k1s, Kubernetes YAML, or Helm skeleton."""
        return protect(
            "BundleExport",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: export_bundle(
                    supervisor=supervisor,
                    stage_dir=Path(stage),
                    fmt=format,
                    namespace=namespace,
                ),
            ),
        )

    @mcp.tool()
    def workerbee_v1_cleanup(
        execute: bool = False,
        purge_images: bool = False,
    ) -> dict[str, Any]:
        """Dry-run or execute WorkerBee runtime cleanup."""
        return protect(
            "Cleanup",
            None,
            lambda: daemon.cleanup(execute=execute, purge_images=purge_images),
        )

    @mcp.tool()
    def workerbee_v1_trust_status() -> dict[str, Any]:
        """Return WorkerBee local CA trust status."""
        return protect("TrustStatus", None, lambda: trust_status(daemon.state_root))

    @mcp.tool()
    def workerbee_v1_trust_install(target: str = "all") -> dict[str, Any]:
        """Install WorkerBee local CA into explicit trust targets."""
        return protect(
            "TrustInstall",
            None,
            lambda: trust_install(daemon.state_root, target=target),
        )

    @mcp.tool()
    def workerbee_v1_trust_uninstall(target: str = "all") -> dict[str, Any]:
        """Remove WorkerBee local CA from explicit trust targets."""
        return protect(
            "TrustUninstall",
            None,
            lambda: trust_uninstall(daemon.state_root, target=target),
        )

    mcp.run(transport="streamable-http")
