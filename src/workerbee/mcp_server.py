"""MCP server adapter for WorkerBee v1."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import Any

from workerbee.agent import runbook_markdown
from workerbee.containerd_helper import (
    containerd_privilege_env,
    ensure_containerd_privilege,
    stop_containerd_helper,
    temporary_containerd_privilege_env,
)
from workerbee.contract import protect
from workerbee.daemon import WorkerBeeDaemon
from workerbee.ingress import resolve_ingress_settings
from workerbee.manifests import (
    deploy_remote_k1s_stage,
    export_bundle,
    prepare_stage,
    resolve_stage_dir,
    validate_stage,
)
from workerbee.mcp_daemon import require_mcp_loopback_or_opt_in
from workerbee.paths import default_state_root
from workerbee.runbooks import DEFAULT_REPO_RUNBOOK_PATH
from workerbee.trust import trust_install, trust_status, trust_uninstall

INGRESS_PROBE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "workerbee_v1_ingress_probeArguments",
    "properties": {
        "project": {"type": "string", "default": "default", "title": "Project"},
        "url": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "title": "Url",
        },
        "host": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "title": "Host",
        },
        "path": {"type": "string", "default": "/", "title": "Path"},
        "method": {"type": "string", "default": "GET", "title": "Method"},
        "expected_status": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "default": None,
            "title": "Expected Status",
        },
        "body_contains": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "title": "Body Contains",
        },
        "json_body": {
            "anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}],
            "default": None,
            "title": "Json Body",
        },
        "body": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "title": "Body",
        },
        "headers": {
            "anyOf": [
                {"type": "object", "additionalProperties": {"type": "string"}},
                {"type": "null"},
            ],
            "default": None,
            "title": "Headers",
        },
        "timeout": {"type": "number", "default": 10.0, "title": "Timeout"},
    },
}


def serve_mcp(
    *,
    project: str = "default",
    runtime: str = "auto",
    host: str = "127.0.0.1",
    port: int = 8765,
    state_dir: Path | None = None,
    state_root: Path | None = None,
    containerd_privilege: str = "auto",
    allow_remote_mcp: bool = False,
    ingress_exposure: str | None = None,
    ingress_domain: str | None = None,
    ingress_bind: str | None = None,
    ingress_ca_port: int | None = None,
    ingress_dns: str | None = None,
    ingress_dns_port: int | None = None,
    ingress_dns_bind: str | None = None,
    ingress_dns_answer: str | None = None,
    ingress_dns_upstreams: list[str] | tuple[str, ...] | str | None = None,
) -> None:
    require_mcp_loopback_or_opt_in(host, allow_remote_mcp=allow_remote_mcp)
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - depends on optional runtime install
        raise RuntimeError(
            "The MCP SDK is not installed. Install with `python -m pip install -e .`."
        ) from exc

    if state_dir is not None and state_root is not None:
        raise RuntimeError("use either --state-dir or --state-root, not both")
    privilege = ensure_containerd_privilege(
        state_root=state_root or state_dir or default_state_root(),
        runtime=runtime,
        mode=containerd_privilege,
    )
    privilege_env = containerd_privilege_env(privilege)
    os.environ.update(privilege_env)
    ingress_settings = resolve_ingress_settings(
        exposure=ingress_exposure,
        base_domain=ingress_domain,
        bind_host=ingress_bind,
        ca_http_port=ingress_ca_port,
        dns_mode=ingress_dns,
        dns_port=ingress_dns_port,
        dns_bind=ingress_dns_bind,
        dns_answer=ingress_dns_answer,
        dns_upstreams=ingress_dns_upstreams,
    )
    daemon = WorkerBeeDaemon(
        state_root=state_root or state_dir,
        runtime=runtime,
        containerd_privilege=containerd_privilege,
        default_project=project,
        ingress_settings=ingress_settings,
    )
    metadata_file = (state_root or state_dir or default_state_root()).resolve() / (
        "global/mcp-daemon.json"
    )
    daemon.configure_dashboard_lifecycle(
        shutdown=lambda: _request_mcp_shutdown(metadata_file),
        reboot=lambda: _request_mcp_reboot(
            _serve_exec_argv(
                project=project,
                runtime=runtime,
                host=host,
                port=port,
                state_dir=state_dir,
                state_root=state_root,
                containerd_privilege=containerd_privilege,
                ingress_exposure=ingress_settings.exposure,
                ingress_domain=ingress_settings.base_domain,
                ingress_bind=ingress_settings.bind_host,
                ingress_ca_port=ingress_settings.ca_http_port,
                ingress_dns=ingress_settings.dns.mode,
                ingress_dns_port=ingress_settings.dns.port,
                ingress_dns_bind=ingress_settings.dns.bind_host,
                ingress_dns_answer=ingress_settings.dns.answer,
                ingress_dns_upstreams=ingress_settings.dns.upstreams,
            )
        ),
    )
    try:
        with temporary_containerd_privilege_env(privilege_env):
            ingress = daemon.start(mcp_bind_url=f"http://{host}:{port}/mcp")
    except Exception:
        _release_foreground_privilege(
            privilege=privilege,
            daemon=daemon,
            privilege_env=privilege_env,
            stop_stacks=False,
        )
        raise
    print(f"WorkerBee global dashboard: {ingress.dashboard_url}", flush=True)
    ingress_public = ingress.public_dict() if hasattr(ingress, "public_dict") else {}
    ca_commands = ingress_public.get("ca_commands") if isinstance(ingress_public, dict) else {}
    dashboard_ca_download_url = getattr(ingress, "dashboard_ca_download_url", None)
    if dashboard_ca_download_url:
        print(f"WorkerBee dashboard CA: {dashboard_ca_download_url}", flush=True)
    ca_download_url = getattr(ingress, "ca_download_url", None)
    if ca_download_url:
        print(f"WorkerBee CA download: {ca_download_url}", flush=True)
    if isinstance(ca_commands, dict) and ca_commands:
        if ca_commands.get("export"):
            print(f"WorkerBee CA export: {ca_commands['export']}", flush=True)
        if ca_commands.get("trust_system"):
            print(f"WorkerBee local trust: {ca_commands['trust_system']}", flush=True)
        if ca_commands.get("trust_nss"):
            print(f"WorkerBee browser trust: {ca_commands['trust_nss']}", flush=True)
    dns = getattr(ingress, "dns", None)
    if isinstance(dns, dict) and dns.get("enabled"):
        print(
            f"WorkerBee DNS: {dns.get('bind_host')}:{dns.get('port')} "
            f"for {dns.get('base_domain')}",
            flush=True,
        )
    print(f"WorkerBee state root: {ingress.state_root}", flush=True)
    dns_ok = getattr(ingress, "dashboard_dns_ok", getattr(ingress, "localhost_dns_ok", True))
    if not dns_ok:
        print(
            f"WorkerBee warning: {ingress.dashboard_url} DNS did not resolve on this host",
            flush=True,
        )

    mcp = FastMCP("K1S WorkerBee", host=host, port=port, json_response=True)

    @mcp.tool()
    def workerbee_v1_capabilities() -> dict[str, Any]:
        """Return WorkerBee v1 MCP contract capabilities."""
        return protect("Capabilities", None, daemon.capabilities)

    @mcp.tool()
    def workerbee_v1_session_start(
        cwd: str,
        goal: str | None = None,
        project: str | None = None,
        open_dashboard: bool = False,
    ) -> dict[str, Any]:
        """Bootstrap an agent session and return project scope plus workflow guidance."""
        return protect(
            "SessionStart",
            project,
            lambda: daemon.session_start(
                cwd=cwd,
                goal=goal,
                project=project,
                open_dashboard=open_dashboard,
            ),
        )

    @mcp.tool()
    def workerbee_v1_projects() -> dict[str, Any]:
        """Return all project-scoped WorkerBee stacks known to this MCP daemon."""
        return protect("Projects", None, daemon.projects)

    @mcp.tool()
    def workerbee_v1_ingress_status() -> dict[str, Any]:
        """Return WorkerBee global ingress, DNS, CA readiness, and command guidance."""
        return protect("IngressStatus", None, daemon.global_dashboard)

    @mcp.tool()
    def workerbee_v1_ingress_ca_regenerate(confirm: bool = False) -> dict[str, Any]:
        """Explicitly regenerate WorkerBee global ingress CA after confirmation."""
        return protect(
            "IngressCARegenerate",
            None,
            lambda: daemon.ingress_ca_regenerate(confirm=confirm),
        )

    @mcp.tool()
    def workerbee_v1_secret_policy_status(project: str = "default") -> dict[str, Any]:
        """Return secure-by-default secret policy status for a WorkerBee project."""
        return protect(
            "SecretPolicyStatus",
            project,
            lambda: daemon.secret_policy_status(project=project),
        )

    @mcp.tool()
    def workerbee_v1_project_mode_get(project: str = "default") -> dict[str, Any]:
        """Return persisted WorkerBee mode for a project."""
        return protect("ProjectModeGet", project, lambda: daemon.project_mode_get(project))

    @mcp.tool()
    def workerbee_v1_project_mode_set(
        mode: str,
        project: str = "default",
        open_dashboard: bool = False,
    ) -> dict[str, Any]:
        """Persist project mode: start, lazy, or stop."""
        return protect(
            "ProjectModeSet",
            project,
            lambda: daemon.project_mode_set(
                project=project,
                mode=mode,
                open_dashboard=open_dashboard,
            ),
        )

    @mcp.tool()
    def workerbee_v1_project_start(
        project: str = "default",
        open_dashboard: bool = False,
    ) -> dict[str, Any]:
        """Start or return a project-local WorkerBee k1s stack."""
        return protect(
            "ProjectStart",
            project,
            lambda: daemon.project_start(project=project, open_dashboard=open_dashboard),
        )

    @mcp.tool()
    def workerbee_v1_project_status(project: str = "default") -> dict[str, Any]:
        """Return project stack status."""
        return protect("ProjectStatus", project, lambda: daemon.project_status(project))

    @mcp.tool()
    def workerbee_v1_project_runbook_status(project: str = "default") -> dict[str, Any]:
        """Return per-project WorkerBee runbook status."""
        return protect(
            "ProjectRunbookStatus",
            project,
            lambda: daemon.project_runbook_status(project),
        )

    @mcp.tool()
    def workerbee_v1_project_runbook_update(
        content: str,
        project: str = "default",
        mode: str = "append",
        source: str = "agent",
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Append or replace a per-project WorkerBee runbook."""
        return protect(
            "ProjectRunbookUpdate",
            project,
            lambda: daemon.project_runbook_update(
                project=project,
                content=content,
                mode=mode,
                source=source,
                summary=summary,
            ),
        )

    @mcp.tool()
    def workerbee_v1_project_runbook_export(
        project: str = "default",
        path: str = DEFAULT_REPO_RUNBOOK_PATH,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Export the per-project WorkerBee runbook to a repo-relative path."""
        return protect(
            "ProjectRunbookExport",
            project,
            lambda: daemon.project_runbook_export(
                project=project,
                path=path,
                overwrite=overwrite,
            ),
        )

    @mcp.tool()
    def workerbee_v1_project_runbook_import(
        project: str = "default",
        path: str = DEFAULT_REPO_RUNBOOK_PATH,
        mode: str = "replace",
    ) -> dict[str, Any]:
        """Import a repo-relative WorkerBee runbook into project state."""
        return protect(
            "ProjectRunbookImport",
            project,
            lambda: daemon.project_runbook_import(
                project=project,
                path=path,
                mode=mode,
            ),
        )

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
            lambda: daemon.with_project(
                project,
                lambda supervisor: supervisor.reset(),
                require_active=True,
            ),
        )

    @mcp.tool()
    def workerbee_v1_profile_list() -> dict[str, Any]:
        """List built-in direct-containerd k1s profiles."""
        return protect("ProfileList", None, daemon.profile_list)

    @mcp.tool()
    def workerbee_v1_profile_start(
        profile: str,
        project: str = "default",
        k1s_root: str | None = None,
        timeout: float = 180.0,
        from_microk8s: bool = False,
        release: str = "k1s-dev-a",
        namespace: str = "k1s-dev-a",
        site_id: str = "workerbee-edge",
        node_id: str = "workerbee-edge-node",
        bundle: dict[str, Any] | str | None = None,
        bundle_path: str | None = None,
        controller_url: str | None = None,
        agent_token: str | None = None,
        nats_leaf_addr: str | None = None,
        nats_leaf_url: str | None = None,
        rathole_server_addr: str | None = None,
        rathole_token: str | None = None,
        registry_host: str | None = None,
        stack_domain: str | None = None,
        wildcard_apps_domain: str | None = None,
        advertise_host: str | None = None,
        edge_local_addr: str | None = None,
        build_images: bool = True,
    ) -> dict[str, Any]:
        """Start a containerized k1s profile. Requires WorkerBee direct containerd."""
        return protect(
            "ProfileStart",
            project,
            lambda: daemon.profile_start(
                profile=profile,
                project=project,
                k1s_root=k1s_root,
                timeout=timeout,
                from_microk8s=from_microk8s,
                release=release,
                namespace=namespace,
                site_id=site_id,
                node_id=node_id,
                bundle=bundle,
                bundle_path=bundle_path,
                controller_url=controller_url,
                agent_token=agent_token,
                nats_leaf_addr=nats_leaf_addr,
                nats_leaf_url=nats_leaf_url,
                rathole_server_addr=rathole_server_addr,
                rathole_token=rathole_token,
                registry_host=registry_host,
                stack_domain=stack_domain,
                wildcard_apps_domain=wildcard_apps_domain,
                advertise_host=advertise_host,
                edge_local_addr=edge_local_addr,
                build_images=build_images,
            ),
        )

    @mcp.tool()
    def workerbee_v1_profile_status(
        project: str = "default",
        k1s_root: str | None = None,
    ) -> dict[str, Any]:
        """Return status for a containerized k1s profile."""
        return protect(
            "ProfileStatus",
            project,
            lambda: daemon.profile_status(project=project, k1s_root=k1s_root),
        )

    @mcp.tool()
    def workerbee_v1_profile_stop(
        purge: bool = False,
        project: str = "default",
        k1s_root: str | None = None,
    ) -> dict[str, Any]:
        """Stop a containerized k1s profile and optionally purge profile state."""
        return protect(
            "ProfileStop",
            project,
            lambda: daemon.profile_stop(project=project, purge=purge, k1s_root=k1s_root),
        )

    @mcp.tool()
    def workerbee_v1_profile_validate(
        profile: str,
        project: str = "default",
        k1s_root: str | None = None,
        timeout: float = 180.0,
        from_microk8s: bool = False,
        release: str = "k1s-dev-a",
        namespace: str = "k1s-dev-a",
        site_id: str = "workerbee-edge",
        node_id: str = "workerbee-edge-node",
        bundle: dict[str, Any] | str | None = None,
        bundle_path: str | None = None,
        controller_url: str | None = None,
        agent_token: str | None = None,
        nats_leaf_addr: str | None = None,
        nats_leaf_url: str | None = None,
        rathole_server_addr: str | None = None,
        rathole_token: str | None = None,
        registry_host: str | None = None,
        stack_domain: str | None = None,
        wildcard_apps_domain: str | None = None,
        advertise_host: str | None = None,
        edge_local_addr: str | None = None,
        build_images: bool = True,
        require_gpu_smoke: bool = True,
    ) -> dict[str, Any]:
        """Run validation for a containerized k1s profile."""
        return protect(
            "ProfileValidate",
            project,
            lambda: daemon.profile_validate(
                profile=profile,
                project=project,
                k1s_root=k1s_root,
                timeout=timeout,
                from_microk8s=from_microk8s,
                release=release,
                namespace=namespace,
                site_id=site_id,
                node_id=node_id,
                bundle=bundle,
                bundle_path=bundle_path,
                controller_url=controller_url,
                agent_token=agent_token,
                nats_leaf_addr=nats_leaf_addr,
                nats_leaf_url=nats_leaf_url,
                rathole_server_addr=rathole_server_addr,
                rathole_token=rathole_token,
                registry_host=registry_host,
                stack_domain=stack_domain,
                wildcard_apps_domain=wildcard_apps_domain,
                advertise_host=advertise_host,
                edge_local_addr=edge_local_addr,
                build_images=build_images,
                require_gpu_smoke=require_gpu_smoke,
            ),
        )

    @mcp.tool()
    def workerbee_v1_edge_link_start(
        project: str = "default",
        k1s_root: str | None = None,
        from_microk8s: bool = False,
        release: str = "k1s-dev-a",
        namespace: str = "k1s-dev-a",
        site_id: str = "workerbee-edge",
        node_id: str = "workerbee-edge-node",
        bundle: dict[str, Any] | str | None = None,
        bundle_path: str | None = None,
        controller_url: str | None = None,
        agent_token: str | None = None,
        nats_leaf_addr: str | None = None,
        nats_leaf_url: str | None = None,
        rathole_server_addr: str | None = None,
        rathole_token: str | None = None,
        registry_host: str | None = None,
        stack_domain: str | None = None,
        wildcard_apps_domain: str | None = None,
        advertise_host: str | None = None,
        edge_local_addr: str | None = None,
        timeout: float = 180.0,
        build_images: bool = True,
    ) -> dict[str, Any]:
        """Start an advanced k1s edge gateway/node link to an external core."""
        return protect(
            "EdgeLinkStart",
            project,
            lambda: daemon.edge_link_start(
                project=project,
                k1s_root=k1s_root,
                from_microk8s=from_microk8s,
                release=release,
                namespace=namespace,
                site_id=site_id,
                node_id=node_id,
                bundle=bundle,
                bundle_path=bundle_path,
                controller_url=controller_url,
                agent_token=agent_token,
                nats_leaf_addr=nats_leaf_addr,
                nats_leaf_url=nats_leaf_url,
                rathole_server_addr=rathole_server_addr,
                rathole_token=rathole_token,
                registry_host=registry_host,
                stack_domain=stack_domain,
                wildcard_apps_domain=wildcard_apps_domain,
                advertise_host=advertise_host,
                edge_local_addr=edge_local_addr,
                timeout=timeout,
                build_images=build_images,
            ),
        )

    @mcp.tool()
    def workerbee_v1_edge_link_status(
        project: str = "default",
        k1s_root: str | None = None,
    ) -> dict[str, Any]:
        """Return status for a k1s edge-link."""
        return protect(
            "EdgeLinkStatus",
            project,
            lambda: daemon.edge_link_status(project=project, k1s_root=k1s_root),
        )

    @mcp.tool()
    def workerbee_v1_edge_link_validate(
        project: str = "default",
        k1s_root: str | None = None,
        from_microk8s: bool = False,
        release: str = "k1s-dev-a",
        namespace: str = "k1s-dev-a",
        site_id: str = "workerbee-edge",
        node_id: str = "workerbee-edge-node",
        bundle: dict[str, Any] | str | None = None,
        bundle_path: str | None = None,
        controller_url: str | None = None,
        agent_token: str | None = None,
        nats_leaf_addr: str | None = None,
        nats_leaf_url: str | None = None,
        rathole_server_addr: str | None = None,
        rathole_token: str | None = None,
        registry_host: str | None = None,
        stack_domain: str | None = None,
        wildcard_apps_domain: str | None = None,
        advertise_host: str | None = None,
        edge_local_addr: str | None = None,
        timeout: float = 180.0,
        build_images: bool = True,
        require_gpu_smoke: bool = True,
    ) -> dict[str, Any]:
        """Validate an advanced k1s edge-link, including GPU smoke when enabled."""
        return protect(
            "EdgeLinkValidate",
            project,
            lambda: daemon.edge_link_validate(
                project=project,
                k1s_root=k1s_root,
                from_microk8s=from_microk8s,
                release=release,
                namespace=namespace,
                site_id=site_id,
                node_id=node_id,
                bundle=bundle,
                bundle_path=bundle_path,
                controller_url=controller_url,
                agent_token=agent_token,
                nats_leaf_addr=nats_leaf_addr,
                nats_leaf_url=nats_leaf_url,
                rathole_server_addr=rathole_server_addr,
                rathole_token=rathole_token,
                registry_host=registry_host,
                stack_domain=stack_domain,
                wildcard_apps_domain=wildcard_apps_domain,
                advertise_host=advertise_host,
                edge_local_addr=edge_local_addr,
                timeout=timeout,
                build_images=build_images,
                require_gpu_smoke=require_gpu_smoke,
            ),
        )

    @mcp.tool()
    def workerbee_v1_edge_link_stop(
        purge: bool = False,
        project: str = "default",
        k1s_root: str | None = None,
    ) -> dict[str, Any]:
        """Stop a k1s edge-link and optionally purge its state."""
        return protect(
            "EdgeLinkStop",
            project,
            lambda: daemon.edge_link_stop(project=project, purge=purge, k1s_root=k1s_root),
        )

    @mcp.tool()
    def workerbee_v1_logs(
        app: str = "api",
        tail: int = 80,
        project: str = "default",
        target: str = "workerbee",
        profile: str | None = None,
        namespace: str | None = None,
        include_exited: bool = True,
    ) -> dict[str, Any]:
        """Return recent logs for an app."""
        if target == "profile":
            return protect(
                "Logs",
                project,
                lambda: daemon.profile_logs(
                    app=app,
                    project=project,
                    profile=profile,
                    namespace=namespace,
                    tail=tail,
                ),
            )
        return protect(
            "Logs",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: supervisor.logs(
                    app=app,
                    namespace=namespace,
                    tail=tail,
                    include_exited=include_exited,
                ),
                require_active=True,
            ),
        )

    @mcp.tool()
    def workerbee_v1_workload_restart(
        app: str,
        project: str = "default",
        namespace: str | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        """Restart a deployed workload after rebuilding an unchanged local image tag."""
        return protect(
            "WorkloadRestart",
            project,
            lambda: daemon.workload_restart(
                app=app,
                project=project,
                namespace=namespace,
                timeout=timeout,
            ),
        )

    @mcp.tool()
    def workerbee_v1_profile_workload_status(
        project: str = "default",
        profile: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Return workload status through a project-scoped k1s profile API."""
        return protect(
            "ProfileWorkloadStatus",
            project,
            lambda: daemon.profile_workload_status(
                project=project,
                profile=profile,
                namespace=namespace,
            ),
        )

    @mcp.tool()
    def workerbee_v1_exec(
        app: str,
        command: list[str],
        project: str = "default",
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Run a bounded command inside an app container."""
        return protect(
            "Exec",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: supervisor.run_exec(app, command, namespace=namespace),
                require_active=True,
            ),
        )

    @mcp.tool()
    def workerbee_v1_ingress_probe(
        project: str = "default",
        url: str | None = None,
        host: str | None = None,
        path: str = "/",
        method: str = "GET",
        expected_status: int | None = None,
        body_contains: str | None = None,
        json_body: dict[str, Any] | None = None,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Probe a WorkerBee-managed local HTTPS ingress URL with the WorkerBee CA."""
        return protect(
            "IngressProbe",
            project,
            lambda: daemon.ingress_probe(
                project=project,
                url=url,
                host=host,
                path=path,
                method=method,
                expected_status=expected_status,
                body_contains=body_contains,
                json_body=json_body,
                body=body,
                headers=headers,
                timeout=timeout,
            ),
        )

    @mcp.tool()
    def workerbee_v1_image_build(
        context: str,
        tag: str | None = None,
        dockerfile: str | None = None,
        hardening_profile: str | None = None,
        project: str = "default",
    ) -> dict[str, Any]:
        """Build a local image context with optional hardening metadata."""
        return protect(
            "ImageBuild",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: supervisor.build_image(
                    Path(context),
                    tag=tag,
                    dockerfile=Path(dockerfile) if dockerfile else None,
                    hardening_profile=hardening_profile,
                ),
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
        return protect(
            "ManifestValidate",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: validate_stage(
                    resolve_stage_dir(supervisor, stage),
                    cwd=supervisor.cwd,
                ),
            ),
        )

    @mcp.tool()
    def workerbee_v1_manifest_deploy_local(
        stage: str,
        namespace: str | None = None,
        timeout: int = 180,
        project: str = "default",
        target: str = "workerbee",
        profile: str | None = None,
        k1s_root: str | None = None,
        prune: bool = False,
    ) -> dict[str, Any]:
        """Apply staged native k1s or practical Kubernetes manifests locally."""
        return protect(
            "ManifestDeployLocal",
            project,
            lambda: daemon.manifest_deploy_local(
                stage=Path(stage),
                target=target,
                profile=profile,
                project=project,
                namespace=namespace,
                timeout=timeout,
                k1s_root=k1s_root,
                prune=prune,
            ),
        )

    @mcp.tool()
    def workerbee_v1_profile_workload_validate(
        profile: str,
        project: str = "default",
        k1s_root: str | None = None,
        timeout: float = 240.0,
    ) -> dict[str, Any]:
        """Build, deploy, and probe the realtime workload on a k1s profile."""
        return protect(
            "ProfileWorkloadValidate",
            project,
            lambda: daemon.profile_workload_validate(
                profile=profile,
                project=project,
                k1s_root=k1s_root,
                timeout=timeout,
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
        allow_remote_secretrefs: bool = False,
    ) -> dict[str, Any]:
        """Apply staged native k1s or practical Kubernetes manifests to remote k1s."""
        return protect(
            "ManifestDeployRemoteK1s",
            project,
            lambda: daemon.with_project(
                project,
                lambda supervisor: deploy_remote_k1s_stage(
                    supervisor=supervisor,
                    stage_dir=resolve_stage_dir(supervisor, stage),
                    server=server,
                    token=token,
                    namespace=namespace,
                    timeout=timeout,
                    allow_remote_secretrefs=allow_remote_secretrefs,
                ),
                require_not_stopped=True,
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
                    stage_dir=resolve_stage_dir(supervisor, stage),
                    fmt=format,
                    namespace=namespace,
                ),
            ),
        )

    @mcp.tool()
    def workerbee_v1_security_assess(
        stage: str,
        project: str = "default",
        target: str = "workerbee",
        namespace: str | None = None,
        checks: list[str] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Run advisory manifest/export/runtime security assessment for a staged app."""
        return protect(
            "SecurityAssess",
            project,
            lambda: daemon.security_assess(
                stage=Path(stage),
                target=target,
                project=project,
                namespace=namespace,
                checks=checks,
                timeout=timeout,
            ),
        )

    @mcp.tool()
    def workerbee_v1_security_review_project(
        project: str = "default",
        stage: str | None = None,
        target: str = "workerbee",
        namespace: str | None = None,
        checks: list[str] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Review the latest deployed WorkerBee project and write a report artifact."""
        return protect(
            "SecurityReviewProject",
            project,
            lambda: daemon.security_review_project(
                project=project,
                stage=Path(stage) if stage else None,
                target=target,
                namespace=namespace,
                checks=checks,
                timeout=timeout,
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

    @mcp.resource(
        "workerbee://runbook/v1",
        name="workerbee-runbook-v1",
        title="WorkerBee Cloud-Native Loop",
        description="Agent workflow guidance for using WorkerBee as a local k1s workbench.",
        mime_type="text/markdown",
    )
    def workerbee_runbook_resource() -> str:
        return runbook_markdown()

    @mcp.prompt(
        name="workerbee_cloud_native_loop",
        title="Use WorkerBee Cloud-Native Loop",
        description="Inject WorkerBee's recommended build/deploy/test/export workflow.",
    )
    def workerbee_cloud_native_loop(cwd: str, goal: str = "") -> str:
        return (
            f"Use WorkerBee for this cloud-native task.\n\ncwd: {cwd}\n"
            f"goal: {goal or '(not provided)'}\n\n{runbook_markdown()}"
        )

    _publish_explicit_tool_schema(
        mcp,
        "workerbee_v1_ingress_probe",
        INGRESS_PROBE_INPUT_SCHEMA,
    )

    try:
        mcp.run(transport="streamable-http")
    finally:
        _release_foreground_privilege(
            privilege=privilege,
            daemon=daemon,
            privilege_env=privilege_env,
            stop_stacks=True,
        )


def _release_foreground_privilege(
    *,
    privilege: dict[str, Any],
    daemon: WorkerBeeDaemon,
    privilege_env: dict[str, str],
    stop_stacks: bool,
) -> None:
    helper = privilege.get("helper") if isinstance(privilege, dict) else None
    with temporary_containerd_privilege_env(privilege_env):
        if stop_stacks and isinstance(helper, dict) and helper.get("started"):
            cleanup = daemon.stop_all_projects(purge=False)
            ingress_cleanup = daemon.stop_global_ingress()
            if not bool(cleanup.get("ok")) or not bool(ingress_cleanup.get("ok")):
                return
        elif stop_stacks:
            daemon.stop_global_ingress()
        if not isinstance(helper, dict) or not helper.get("started"):
            return
        stop_containerd_helper(daemon.state_root)


def _publish_explicit_tool_schema(mcp: Any, name: str, schema: dict[str, Any]) -> bool:
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        return False
    tool = tools.get(name)
    if tool is None:
        return False
    tool.parameters = dict(schema)
    return True


def _serve_exec_argv(
    *,
    project: str,
    runtime: str,
    host: str,
    port: int,
    state_dir: Path | None,
    state_root: Path | None,
    containerd_privilege: str,
    ingress_exposure: str | None,
    ingress_domain: str | None,
    ingress_bind: str | None,
    ingress_ca_port: int | None,
    ingress_dns: str | None,
    ingress_dns_port: int | None,
    ingress_dns_bind: str | None,
    ingress_dns_answer: str | None,
    ingress_dns_upstreams: list[str] | tuple[str, ...] | str | None,
) -> list[str]:
    argv = [sys.executable, "-m", "workerbee"]
    if state_root is not None:
        argv.extend(["--state-root", str(state_root)])
    elif state_dir is not None:
        argv.extend(["--state-dir", str(state_dir)])
    argv.extend(
        [
            "--runtime",
            runtime,
            "--containerd-privilege",
            containerd_privilege,
            "--project",
            project,
            "mcp",
            "serve",
            "--host",
            host,
            "--port",
            str(port),
        ]
    )
    settings = resolve_ingress_settings(
        exposure=ingress_exposure,
        base_domain=ingress_domain,
        bind_host=ingress_bind,
        ca_http_port=ingress_ca_port,
        dns_mode=ingress_dns,
        dns_port=ingress_dns_port,
        dns_bind=ingress_dns_bind,
        dns_answer=ingress_dns_answer,
        dns_upstreams=ingress_dns_upstreams,
    )
    argv.extend(
        [
            "--ingress-exposure",
            settings.exposure,
            "--ingress-domain",
            settings.base_domain,
            "--ingress-bind",
            settings.bind_host,
            "--ingress-ca-port",
            str(settings.ca_http_port),
        ]
    )
    if settings.dns.enabled:
        argv.extend(
            [
                "--ingress-dns",
                settings.dns.mode,
                "--ingress-dns-port",
                str(settings.dns.port),
                "--ingress-dns-bind",
                str(settings.dns.bind_host or ""),
                "--ingress-dns-answer",
                str(settings.dns.answer or ""),
            ]
        )
        for upstream in settings.dns.upstreams:
            argv.extend(["--ingress-dns-upstream", upstream])
    return argv


def _request_mcp_shutdown(metadata_file: Path) -> None:
    _ = metadata_file
    os.kill(os.getpid(), signal.SIGINT)


def _request_mcp_reboot(argv: list[str]) -> None:
    os.execvpe(sys.executable, argv, os.environ.copy())  # noqa: S606
