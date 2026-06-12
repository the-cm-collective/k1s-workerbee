"""WorkerBee CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from workerbee import __version__
from workerbee.agent import (
    agent_instructions_markdown,
    derive_session_project,
    install_agent_instructions,
)
from workerbee.config import (
    clear_cli_config,
    cli_defaults,
    default_config_file,
    load_cli_config,
    save_cli_config,
)
from workerbee.containerd_access import release_containerd_socket_access
from workerbee.containerd_helper import (
    containerd_privilege_env,
    containerd_privilege_status,
    containerd_privilege_summary,
    ensure_containerd_privilege,
    stop_containerd_helper,
    temporary_containerd_privilege_env,
)
from workerbee.daemon import WorkerBeeDaemon
from workerbee.ingress import export_global_ingress_ca
from workerbee.k1s_runtime import resolve_k1s_runtime
from workerbee.manifests import (
    deploy_remote_k1s_stage,
    export_bundle,
    prepare_stage,
    resolve_stage_dir,
    validate_stage,
)
from workerbee.mcp_daemon import (
    config_from_args,
    mcp_daemon_status,
    restart_mcp_daemon,
    start_mcp_daemon,
    stop_mcp_daemon,
)
from workerbee.mcp_server import serve_mcp
from workerbee.paths import daemon_project_state_dir, default_state_root
from workerbee.runbooks import DEFAULT_REPO_RUNBOOK_PATH
from workerbee.runtime_support import CONTAINERD_RUNTIME, runtime_diagnostics
from workerbee.security import DEFAULT_SECURITY_CHECKS, assess_stage_security
from workerbee.supervisor import WorkerBeeSupervisor
from workerbee.trust import trust_install, trust_status, trust_uninstall


def _add_edge_link_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from-microk8s", action="store_true", help="Read bootstrap from MicroK8s")
    parser.add_argument("--release", default="k1s-dev-a", help="MicroK8s Helm release")
    parser.add_argument("--namespace", default="k1s-dev-a", help="MicroK8s namespace")
    parser.add_argument("--site-id", default="workerbee-edge", help="External edge site id")
    parser.add_argument("--node-id", default="workerbee-edge-node", help="External edge node id")
    parser.add_argument(
        "--bundle",
        dest="bundle_path",
        type=Path,
        default=None,
        help="External-core bootstrap JSON or env file",
    )
    parser.add_argument("--controller-url", default=None)
    parser.add_argument("--agent-token", default=None)
    parser.add_argument("--nats-leaf-addr", default=None)
    parser.add_argument("--nats-leaf-url", default=None)
    parser.add_argument("--rathole-server-addr", default=None)
    parser.add_argument("--rathole-token", default=None)
    parser.add_argument("--registry-host", default=None)
    parser.add_argument("--stack-domain", default=None)
    parser.add_argument("--wildcard-apps-domain", default=None)
    parser.add_argument("--advertise-host", default=None)
    parser.add_argument(
        "--edge-local-addr",
        default=None,
        help="Address the rathole client should dial for edge-local HTTP traffic",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Use existing gateway/node images",
    )
    parser.add_argument(
        "--no-gpu-smoke",
        action="store_true",
        help="Skip the real NVIDIA runtime smoke during validation",
    )


def _edge_link_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "from_microk8s": bool(getattr(args, "from_microk8s", False)),
        "release": getattr(args, "release", "k1s-dev-a"),
        "namespace": getattr(args, "namespace", "k1s-dev-a"),
        "site_id": getattr(args, "site_id", "workerbee-edge"),
        "node_id": getattr(args, "node_id", "workerbee-edge-node"),
        "bundle_path": getattr(args, "bundle_path", None),
        "controller_url": getattr(args, "controller_url", None),
        "agent_token": getattr(args, "agent_token", None),
        "nats_leaf_addr": getattr(args, "nats_leaf_addr", None),
        "nats_leaf_url": getattr(args, "nats_leaf_url", None),
        "rathole_server_addr": getattr(args, "rathole_server_addr", None),
        "rathole_token": getattr(args, "rathole_token", None),
        "registry_host": getattr(args, "registry_host", None),
        "stack_domain": getattr(args, "stack_domain", None),
        "wildcard_apps_domain": getattr(args, "wildcard_apps_domain", None),
        "advertise_host": getattr(args, "advertise_host", None),
        "edge_local_addr": getattr(args, "edge_local_addr", None),
        "build_images": not bool(getattr(args, "skip_build", False)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workerbee")
    parser.add_argument("--version", action="version", version=f"workerbee {__version__}")
    parser.add_argument("--project", default=None, help="WorkerBee project name")
    parser.add_argument("--cwd", type=Path, default=None, help="Project working directory hint")
    parser.add_argument("--state-dir", type=Path, default=None, help="Override state directory")
    parser.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="Override WorkerBee daemon state root",
    )
    parser.add_argument(
        "--runtime",
        default="auto",
        choices=["auto", "podman", "docker", "containerd"],
        help="Container runtime backend",
    )
    parser.add_argument(
        "--containerd-socket-access",
        default=None,
        choices=["auto", "off"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--containerd-privilege",
        default="auto",
        choices=["auto", "sudo-helper", "unprivileged"],
        help="Privilege strategy for explicit --runtime containerd",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    sub = parser.add_subparsers(dest="cmd", required=True)

    config = sub.add_parser("config", help="Manage local WorkerBee CLI defaults")
    config_sub = config.add_subparsers(dest="config_cmd", required=True)
    config_sub.add_parser("show", help="Show local WorkerBee CLI defaults")
    config_sub.add_parser("path", help="Show the local WorkerBee config path")
    config_set = config_sub.add_parser("set", help="Set local WorkerBee CLI defaults")
    config_set.add_argument("--runtime", choices=["auto", "podman", "docker", "containerd"])
    config_set.add_argument(
        "--containerd-privilege",
        choices=["auto", "sudo-helper", "unprivileged"],
    )
    config_set.add_argument("--state-root", type=Path)
    config_set.add_argument("--project")
    config_set.add_argument("--mcp-host")
    config_set.add_argument("--mcp-port", type=int)
    config_set.add_argument("--mcp-timeout", type=float)
    config_set.add_argument("--ingress-exposure", choices=["loopback", "lan"])
    config_set.add_argument("--ingress-domain")
    config_set.add_argument("--ingress-bind")
    config_set.add_argument("--ingress-ca-port", type=int)
    config_set.add_argument("--ingress-dns", choices=["off", "forwarding"])
    config_set.add_argument("--ingress-dns-port", type=int)
    config_set.add_argument("--ingress-dns-bind")
    config_set.add_argument("--ingress-dns-answer")
    config_set.add_argument("--ingress-dns-upstream")
    config_sub.add_parser("clear", help="Remove local WorkerBee CLI defaults")

    agent = sub.add_parser("agent", help="Print or install agent instructions")
    agent_sub = agent.add_subparsers(dest="agent_cmd", required=True)
    agent_sub.add_parser("instructions", help="Print the WorkerBee AGENTS.md block")
    agent_install = agent_sub.add_parser("install", help="Install WorkerBee AGENTS.md wording")
    agent_install.add_argument("--target", type=Path, default=Path("AGENTS.md"))
    agent_install.add_argument("--check", action="store_true", help="Check without writing")
    agent_install.add_argument("--append", action="store_true", help="Append the block if missing")
    agent_install.add_argument(
        "--allow-create",
        action="store_true",
        help="Allow creating the target AGENTS.md file",
    )

    sub.add_parser("doctor", help="Check local prerequisites")
    sub.add_parser("start", help="Start the local WorkerBee k1s stack")
    stop = sub.add_parser("stop", help="Stop the local WorkerBee k1s stack")
    stop.add_argument(
        "--purge",
        action="store_true",
        help="Remove WorkerBee state and runtime network",
    )
    sub.add_parser("status", help="Show WorkerBee stack status")
    sub.add_parser("projects", help="List WorkerBee daemon projects")
    profile = sub.add_parser(
        "profile",
        help="Manage advanced direct-containerd k1s profiles",
    )
    profile_sub = profile.add_subparsers(dest="profile_cmd", required=True)
    profile_sub.add_parser("list", help="List built-in k1s profiles")
    profile_start = profile_sub.add_parser("start", help="Start a containerized k1s profile")
    profile_start.add_argument("--profile", required=True)
    profile_start.add_argument("--k1s-root", type=Path, default=None)
    profile_start.add_argument("--timeout", type=float, default=180.0)
    _add_edge_link_arguments(profile_start)
    profile_status = profile_sub.add_parser("status", help="Show k1s profile status")
    profile_status.add_argument("--k1s-root", type=Path, default=None)
    profile_stop = profile_sub.add_parser("stop", help="Stop a containerized k1s profile")
    profile_stop.add_argument("--k1s-root", type=Path, default=None)
    profile_stop.add_argument("--purge", action="store_true")
    edge_link = sub.add_parser("edge-link", help="Manage an advanced external k1s edge link")
    edge_link_sub = edge_link.add_subparsers(dest="edge_link_cmd", required=True)
    edge_link_start = edge_link_sub.add_parser("start", help="Start a k1s edge gateway/node link")
    edge_link_start.add_argument("--k1s-root", type=Path, default=None)
    edge_link_start.add_argument("--timeout", type=float, default=180.0)
    _add_edge_link_arguments(edge_link_start)
    edge_link_status = edge_link_sub.add_parser("status", help="Show k1s edge-link status")
    edge_link_status.add_argument("--k1s-root", type=Path, default=None)
    edge_link_stop = edge_link_sub.add_parser("stop", help="Stop a k1s edge-link")
    edge_link_stop.add_argument("--k1s-root", type=Path, default=None)
    edge_link_stop.add_argument("--purge", action="store_true")
    edge_link_validate = edge_link_sub.add_parser("validate", help="Validate a k1s edge-link")
    edge_link_validate.add_argument("--k1s-root", type=Path, default=None)
    edge_link_validate.add_argument("--timeout", type=float, default=180.0)
    _add_edge_link_arguments(edge_link_validate)
    validate = sub.add_parser("validate", help="Run WorkerBee validation scenarios")
    validate.add_argument(
        "--scenario",
        choices=["k1s-profile", "profile-workload", "edge-link"],
        required=True,
    )
    validate.add_argument("--profile", default=None)
    validate.add_argument("--k1s-root", type=Path, default=None)
    validate.add_argument("--timeout", type=float, default=180.0)
    _add_edge_link_arguments(validate)
    project = sub.add_parser("project", help="Manage daemon project policy")
    project_sub = project.add_subparsers(dest="project_cmd", required=True)
    project_mode = project_sub.add_parser("mode", help="Set project mode: start, lazy, or stop")
    project_mode.add_argument("mode", choices=["start", "lazy", "stop"])
    project_mode.add_argument("--cwd", dest="project_cwd", type=Path, default=None)
    project_mode.add_argument("--project", dest="project_name", default=None)
    project_mode.add_argument("--open", action="store_true", help="Open the project dashboard")
    project_status = project_sub.add_parser("status", help="Show project mode and status")
    project_status.add_argument("--cwd", dest="project_cwd", type=Path, default=None)
    project_status.add_argument("--project", dest="project_name", default=None)
    runbook = sub.add_parser("runbook", help="Manage a project WorkerBee runbook")
    runbook_sub = runbook.add_subparsers(dest="runbook_cmd", required=True)

    def add_runbook_scope(command: argparse.ArgumentParser) -> None:
        command.add_argument("--cwd", dest="runbook_cwd", type=Path, default=None)
        command.add_argument("--project", dest="runbook_project", default=None)

    runbook_status = runbook_sub.add_parser("status", help="Show project runbook status")
    add_runbook_scope(runbook_status)
    runbook_update = runbook_sub.add_parser("update", help="Append or replace runbook content")
    add_runbook_scope(runbook_update)
    runbook_update.add_argument("--file", dest="runbook_file", type=Path, default=None)
    runbook_update.add_argument("--content", dest="runbook_content", default=None)
    runbook_update.add_argument("--mode", choices=["append", "replace"], default="append")
    runbook_update.add_argument("--source", default="agent")
    runbook_update.add_argument("--summary", default=None)
    runbook_export = runbook_sub.add_parser("export", help="Export the runbook into the repo")
    add_runbook_scope(runbook_export)
    runbook_export.add_argument("--path", default=DEFAULT_REPO_RUNBOOK_PATH)
    runbook_export.add_argument("--overwrite", action="store_true")
    runbook_import = runbook_sub.add_parser("import", help="Import a repo runbook into state")
    add_runbook_scope(runbook_import)
    runbook_import.add_argument("--path", default=DEFAULT_REPO_RUNBOOK_PATH)
    runbook_import.add_argument("--mode", choices=["append", "replace"], default="replace")
    sub.add_parser("global-dashboard", help="Show WorkerBee global dashboard status")
    ingress = sub.add_parser("ingress", help="Inspect WorkerBee global ingress")
    ingress_sub = ingress.add_subparsers(dest="ingress_cmd", required=True)
    ingress_sub.add_parser("status", help="Show global ingress status")
    ingress_ca = ingress_sub.add_parser(
        "ca",
        help="Export or explicitly regenerate the WorkerBee Caddy CA",
    )
    ingress_ca.add_argument(
        "ca_action",
        nargs="?",
        choices=["export", "regenerate"],
        default="export",
    )
    ingress_ca.add_argument("--output", "-o", type=Path, default=Path("workerbee-ca.crt"))
    ingress_ca.add_argument(
        "--confirm-regenerate",
        action="store_true",
        help="Confirm intentional WorkerBee ingress CA rotation and trust update.",
    )
    trust = sub.add_parser("trust", help="Manage explicit local CA trust")
    trust_sub = trust.add_subparsers(dest="trust_cmd", required=True)
    trust_sub.add_parser("status", help="Show local CA trust status")
    trust_install_p = trust_sub.add_parser("install", help="Install WorkerBee Caddy CA")
    trust_install_p.add_argument(
        "--target",
        default="all",
        choices=["all", "system", "nss", "user"],
    )
    trust_uninstall_p = trust_sub.add_parser("uninstall", help="Uninstall WorkerBee Caddy CA")
    trust_uninstall_p.add_argument(
        "--target",
        default="all",
        choices=["all", "system", "nss", "user"],
    )
    cleanup = sub.add_parser("cleanup", help="Inspect or remove stale WorkerBee runtime resources")
    cleanup.add_argument("--execute", action="store_true", help="Perform cleanup")
    cleanup.add_argument("--purge-images", action="store_true", help="Also remove WorkerBee images")
    containerd_privilege = sub.add_parser(
        "containerd-privilege",
        help="Inspect or stop the WorkerBee direct-containerd root helper",
    )
    containerd_privilege_sub = containerd_privilege.add_subparsers(
        dest="containerd_privilege_cmd",
        required=True,
    )
    containerd_privilege_sub.add_parser("status", help="Show direct-containerd privilege status")
    containerd_privilege_sub.add_parser("stop-helper", help="Stop the WorkerBee root helper")
    containerd_privilege_sub.add_parser(
        "revoke-acl-leases",
        help="Revoke deprecated WorkerBee containerd socket ACL leases",
    )
    sub.add_parser("tls-info", help="Show local API shim TLS paths")
    sub.add_parser("reset", help="Reset WorkerBee project workloads and artifacts")
    sub.add_parser("poc-status", help="Show POC app status through the native k1s API")
    build = sub.add_parser("build-image", help="Build a local image with the configured runtime")
    build.add_argument("context", type=Path)
    build.add_argument("--tag", default=None)
    build.add_argument(
        "-f",
        "--dockerfile",
        type=Path,
        default=None,
        help="Dockerfile path, relative to the build context by default",
    )
    deploy_native = sub.add_parser("deploy", help="Apply a native k1s manifest")
    deploy_native.add_argument("-f", "--file", type=Path, required=True)
    deploy_native.add_argument("-n", "--namespace", default=None)
    deploy_native.add_argument("--timeout", type=int, default=180)
    manifest = sub.add_parser("manifest", help="Prepare, validate, and deploy staged manifests")
    manifest_sub = manifest.add_subparsers(dest="manifest_cmd", required=True)
    manifest_prepare = manifest_sub.add_parser("prepare", help="Generate editable staged files")
    manifest_prepare.add_argument("--name", default="app")
    manifest_prepare.add_argument(
        "--template",
        default="frontend-api-store",
        choices=["stateless-web", "frontend-api", "frontend-api-store", "realtime-web-db"],
    )
    manifest_prepare.add_argument("--source", type=Path, default=None)
    manifest_validate = manifest_sub.add_parser("validate", help="Validate staged files")
    manifest_validate.add_argument("--stage", type=Path, required=True)
    manifest_local = manifest_sub.add_parser("deploy-local", help="Deploy staged files locally")
    manifest_local.add_argument("--stage", type=Path, required=True)
    manifest_local.add_argument("--target", choices=["workerbee", "profile"], default="workerbee")
    manifest_local.add_argument("--profile", default=None)
    manifest_local.add_argument("--k1s-root", type=Path, default=None)
    manifest_local.add_argument("-n", "--namespace", default=None)
    manifest_local.add_argument("--timeout", type=int, default=180)
    manifest_local.add_argument(
        "--prune",
        action="store_true",
        help="Delete workloads from the previous WorkerBee deployment that are absent now",
    )
    manifest_k1s = manifest_sub.add_parser("deploy-k1s", help="Deploy staged files to remote k1s")
    manifest_k1s.add_argument("--stage", type=Path, required=True)
    manifest_k1s.add_argument("--server", required=True)
    manifest_k1s.add_argument("--token", required=True)
    manifest_k1s.add_argument("-n", "--namespace", default=None)
    manifest_k1s.add_argument("--timeout", type=int, default=180)
    manifest_k1s.add_argument(
        "--allow-remote-secretrefs",
        action="store_true",
        help=(
            "Allow native k1s secretRefs during remote deploy. By default WorkerBee "
            "fails closed because secret paths are resolved on the remote controller."
        ),
    )
    bundle = sub.add_parser("bundle", help="Export staged artifacts")
    bundle_sub = bundle.add_subparsers(dest="bundle_cmd", required=True)
    bundle_export = bundle_sub.add_parser("export", help="Export k1s, k8s, or Helm bundle")
    bundle_export.add_argument("--stage", type=Path, required=True)
    bundle_export.add_argument("--format", choices=["k1s", "k8s", "helm"], default="k1s")
    bundle_export.add_argument("-n", "--namespace", default=None)
    security = sub.add_parser("security", help="Run advisory security assessments")
    security_sub = security.add_subparsers(dest="security_cmd", required=True)
    security_assess = security_sub.add_parser(
        "assess",
        help="Assess staged manifests, existing exports, and optional runtime ingress",
    )
    security_assess.add_argument("--stage", type=Path, required=True)
    security_assess.add_argument("--target", choices=["workerbee", "profile"], default="workerbee")
    security_assess.add_argument("-n", "--namespace", default=None)
    security_assess.add_argument(
        "--check",
        action="append",
        choices=list(DEFAULT_SECURITY_CHECKS),
        help="Assessment check to run; repeat to select multiple checks",
    )
    security_assess.add_argument("--timeout", type=float, default=5.0)
    security_review = security_sub.add_parser(
        "review-project",
        help="Review the latest deployed project and write a report",
    )
    security_review.add_argument("--stage", type=Path, default=None)
    security_review.add_argument("--target", choices=["workerbee", "profile"], default="workerbee")
    security_review.add_argument("-n", "--namespace", default=None)
    security_review.add_argument(
        "--check",
        action="append",
        choices=list(DEFAULT_SECURITY_CHECKS),
        help="Assessment check to run; repeat to select multiple checks",
    )
    security_review.add_argument("--timeout", type=float, default=5.0)
    deploy = sub.add_parser("deploy-poc", help="Build and deploy the representative POC stack")
    deploy.add_argument("--timeout", type=float, default=180.0)
    sub.add_parser("apishim-smoke", help="Inspect POC objects through the k1s API shim")
    logs = sub.add_parser("logs", help="Show recent app logs")
    logs.add_argument("app", nargs="?", default="api")
    logs.add_argument("-n", "--namespace", default=None)
    logs.add_argument("--tail", type=int, default=80)
    logs.add_argument(
        "--include-exited",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fall back to logs from exited containers when no running container exists",
    )
    exec_p = sub.add_parser("exec", help="Run a command in an app container")
    exec_p.add_argument("app")
    exec_p.add_argument("-n", "--namespace", default=None)
    exec_p.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("export-k8s", help="Export POC manifests to Kubernetes YAML")

    mcp = sub.add_parser("mcp", help="Run the MCP server")
    mcp_sub = mcp.add_subparsers(dest="mcp_cmd", required=True)
    def add_mcp_bind_flags(
        command: argparse.ArgumentParser,
        *,
        allow_remote: bool = False,
    ) -> None:
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8765)
        if allow_remote:
            command.add_argument(
                "--allow-remote-mcp",
                action="store_true",
                help=(
                    "Allow non-loopback MCP binds. WorkerBee does not yet implement "
                    "standards-compliant MCP OAuth authorization for remote exposure."
                ),
            )

    def add_mcp_ingress_flags(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--ingress-exposure",
            choices=["loopback", "lan"],
            default=None,
            help="Expose WorkerBee app ingress on loopback or the local LAN",
        )
        command.add_argument(
            "--ingress-domain",
            default=None,
            help="Base domain for generated ingress hosts",
        )
        command.add_argument(
            "--ingress-bind",
            default=None,
            help="Address for Caddy ingress to bind",
        )
        command.add_argument(
            "--ingress-ca-port",
            type=int,
            default=None,
            help="HTTP port for LAN-mode WorkerBee CA download",
        )
        command.add_argument(
            "--ingress-dns",
            choices=["off", "forwarding"],
            default=None,
            help="Run WorkerBee LAN dev DNS forwarding for ingress names",
        )
        command.add_argument(
            "--ingress-dns-port",
            type=int,
            default=None,
            help="Port for WorkerBee LAN dev DNS",
        )
        command.add_argument(
            "--ingress-dns-bind",
            default=None,
            help="Address for WorkerBee LAN dev DNS to bind",
        )
        command.add_argument(
            "--ingress-dns-answer",
            default=None,
            help="LAN IP returned for WorkerBee ingress DNS names",
        )
        command.add_argument(
            "--ingress-dns-upstream",
            action="append",
            default=None,
            help="Upstream DNS resolver for non-WorkerBee names; repeatable",
        )

    start_mcp = mcp_sub.add_parser("start", help="Start WorkerBee MCP in the background")
    add_mcp_bind_flags(start_mcp, allow_remote=True)
    add_mcp_ingress_flags(start_mcp)
    start_mcp.add_argument("--timeout", type=float, default=45.0)
    stop_mcp = mcp_sub.add_parser("stop", help="Stop the background WorkerBee MCP daemon")
    add_mcp_bind_flags(stop_mcp)
    stop_mcp.add_argument("--timeout", type=float, default=10.0)
    restart_mcp = mcp_sub.add_parser("restart", help="Restart WorkerBee MCP in the background")
    add_mcp_bind_flags(restart_mcp, allow_remote=True)
    add_mcp_ingress_flags(restart_mcp)
    restart_mcp.add_argument("--timeout", type=float, default=45.0)
    status_mcp = mcp_sub.add_parser("status", help="Show background WorkerBee MCP status")
    add_mcp_bind_flags(status_mcp)
    serve = mcp_sub.add_parser("serve", help="Serve WorkerBee over Streamable HTTP MCP")
    add_mcp_bind_flags(serve, allow_remote=True)
    add_mcp_ingress_flags(serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    try:
        if args.cmd == "config":
            return _handle_config(args)
        if args.cmd == "agent":
            if args.agent_cmd == "instructions":
                print(agent_instructions_markdown().rstrip())
                return 0
            if args.agent_cmd == "install":
                return _print(
                    install_agent_instructions(
                        target=args.target,
                        check=args.check,
                        append=args.append,
                        allow_create=args.allow_create,
                    ),
                    json_out=args.json,
                )
        _apply_cli_defaults(args, raw_argv)
        containerd_privilege = _containerd_privilege_arg(args)
        if args.cmd == "doctor":
            return _print(
                _doctor(
                    runtime=args.runtime,
                    state_root=args.state_root,
                    containerd_privilege=containerd_privilege,
                ),
                json_out=args.json,
            )
        if args.cmd == "mcp":
            if args.mcp_cmd == "serve":
                serve_mcp(
                    project=args.project or "default",
                    runtime=args.runtime,
                    host=args.host,
                    port=args.port,
                    state_dir=args.state_dir,
                    state_root=args.state_root,
                    containerd_privilege=containerd_privilege,
                    allow_remote_mcp=args.allow_remote_mcp,
                    ingress_exposure=args.ingress_exposure,
                    ingress_domain=args.ingress_domain,
                    ingress_bind=args.ingress_bind,
                    ingress_ca_port=args.ingress_ca_port,
                    ingress_dns=args.ingress_dns,
                    ingress_dns_port=args.ingress_dns_port,
                    ingress_dns_bind=args.ingress_dns_bind,
                    ingress_dns_answer=args.ingress_dns_answer,
                    ingress_dns_upstreams=args.ingress_dns_upstream,
                )
                return 0
            if args.state_dir is not None and args.state_root is not None:
                raise RuntimeError("use either --state-dir or --state-root, not both")
            config = config_from_args(
                state_root=args.state_root or args.state_dir,
                runtime=args.runtime,
                project=args.project or "default",
                host=args.host,
                port=args.port,
                containerd_privilege=containerd_privilege,
                allow_remote_mcp=bool(getattr(args, "allow_remote_mcp", False)),
                ingress_exposure=getattr(args, "ingress_exposure", None),
                ingress_domain=getattr(args, "ingress_domain", None),
                ingress_bind=getattr(args, "ingress_bind", None),
                ingress_ca_port=getattr(args, "ingress_ca_port", None),
                ingress_dns=getattr(args, "ingress_dns", None),
                ingress_dns_port=getattr(args, "ingress_dns_port", None),
                ingress_dns_bind=getattr(args, "ingress_dns_bind", None),
                ingress_dns_answer=getattr(args, "ingress_dns_answer", None),
                ingress_dns_upstreams=getattr(args, "ingress_dns_upstream", None),
            )
            if args.mcp_cmd == "start":
                return _print(start_mcp_daemon(config, timeout=args.timeout), json_out=args.json)
            if args.mcp_cmd == "stop":
                return _print(stop_mcp_daemon(config, timeout=args.timeout), json_out=args.json)
            if args.mcp_cmd == "restart":
                return _print(restart_mcp_daemon(config, timeout=args.timeout), json_out=args.json)
            if args.mcp_cmd == "status":
                return _print(mcp_daemon_status(config), json_out=args.json)
            return 0
        if args.cmd == "projects":
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
                containerd_privilege=containerd_privilege,
                cwd=args.cwd,
            )
            return _print(daemon.projects(), json_out=args.json)
        if args.cmd == "profile":
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
                containerd_privilege=containerd_privilege,
                default_project=args.project or "default",
                cwd=args.cwd,
            )
            if args.profile_cmd == "list":
                return _print(daemon.profile_list(), json_out=args.json)
            privilege = ensure_containerd_privilege(
                state_root=args.state_root or default_state_root(),
                runtime=args.runtime,
                mode=containerd_privilege,
            )
            with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
                if args.profile_cmd == "start":
                    result = daemon.profile_start(
                        profile=args.profile,
                        project=args.project,
                        k1s_root=args.k1s_root,
                        timeout=args.timeout,
                        **_edge_link_kwargs(args),
                    )
                elif args.profile_cmd == "status":
                    result = daemon.profile_status(project=args.project, k1s_root=args.k1s_root)
                elif args.profile_cmd == "stop":
                    result = daemon.profile_stop(
                        project=args.project,
                        purge=args.purge,
                        k1s_root=args.k1s_root,
                    )
                else:
                    result = {"ok": False, "error": f"unknown profile command: {args.profile_cmd}"}
                result["containerd_privilege"] = containerd_privilege_summary(privilege)
                return _print(result, json_out=args.json)
        if args.cmd == "edge-link":
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
                containerd_privilege=containerd_privilege,
                default_project=args.project or "default",
                cwd=args.cwd,
            )
            privilege = ensure_containerd_privilege(
                state_root=args.state_root or default_state_root(),
                runtime=args.runtime,
                mode=containerd_privilege,
            )
            with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
                if args.edge_link_cmd == "start":
                    result = daemon.edge_link_start(
                        project=args.project,
                        k1s_root=args.k1s_root,
                        timeout=args.timeout,
                        **_edge_link_kwargs(args),
                    )
                elif args.edge_link_cmd == "status":
                    result = daemon.edge_link_status(project=args.project, k1s_root=args.k1s_root)
                elif args.edge_link_cmd == "stop":
                    result = daemon.edge_link_stop(
                        project=args.project,
                        purge=args.purge,
                        k1s_root=args.k1s_root,
                    )
                elif args.edge_link_cmd == "validate":
                    result = daemon.edge_link_validate(
                        project=args.project,
                        k1s_root=args.k1s_root,
                        timeout=args.timeout,
                        require_gpu_smoke=not bool(args.no_gpu_smoke),
                        **_edge_link_kwargs(args),
                    )
                else:
                    result = {
                        "ok": False,
                        "error": f"unknown edge-link command: {args.edge_link_cmd}",
                    }
                result["containerd_privilege"] = containerd_privilege_summary(privilege)
                return _print(result, json_out=args.json)
        if args.cmd == "validate":
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
                containerd_privilege=containerd_privilege,
                default_project=args.project or "default",
                cwd=args.cwd,
            )
            if args.scenario == "k1s-profile":
                if not args.profile:
                    raise ValueError("--profile is required for k1s-profile validation")
                privilege = ensure_containerd_privilege(
                    state_root=args.state_root or default_state_root(),
                    runtime=args.runtime,
                    mode=containerd_privilege,
                )
                with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
                    result = daemon.profile_validate(
                        profile=args.profile,
                        project=args.project,
                        k1s_root=args.k1s_root,
                        timeout=args.timeout,
                        **_edge_link_kwargs(args),
                    )
                    result["containerd_privilege"] = containerd_privilege_summary(privilege)
                    return _print(result, json_out=args.json)
            if args.scenario == "profile-workload":
                if not args.profile:
                    raise ValueError("--profile is required for profile-workload validation")
                privilege = ensure_containerd_privilege(
                    state_root=args.state_root or default_state_root(),
                    runtime=args.runtime,
                    mode=containerd_privilege,
                )
                with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
                    result = daemon.profile_workload_validate(
                        profile=args.profile,
                        project=args.project,
                        k1s_root=args.k1s_root,
                        timeout=args.timeout,
                    )
                    result["containerd_privilege"] = containerd_privilege_summary(privilege)
                    return _print(result, json_out=args.json)
            if args.scenario == "edge-link":
                privilege = ensure_containerd_privilege(
                    state_root=args.state_root or default_state_root(),
                    runtime=args.runtime,
                    mode=containerd_privilege,
                )
                with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
                    result = daemon.edge_link_validate(
                        project=args.project,
                        k1s_root=args.k1s_root,
                        timeout=args.timeout,
                        require_gpu_smoke=not bool(args.no_gpu_smoke),
                        **_edge_link_kwargs(args),
                    )
                    result["containerd_privilege"] = containerd_privilege_summary(privilege)
                    return _print(result, json_out=args.json)
        if args.cmd == "project":
            cwd = args.project_cwd or args.cwd or Path.cwd()
            project = args.project_name or args.project or derive_session_project(cwd)
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
                containerd_privilege=containerd_privilege,
                default_project=project,
                cwd=cwd,
            )
            if args.project_cmd == "mode":
                return _print(
                    daemon.project_mode_set(
                        project=project,
                        mode=args.mode,
                        cwd=cwd,
                        open_dashboard=args.open,
                    ),
                    json_out=args.json,
                )
            if args.project_cmd == "status":
                return _print(daemon.project_mode_get(project), json_out=args.json)
        if args.cmd == "runbook":
            cwd = args.runbook_cwd or args.cwd or Path.cwd()
            project = args.runbook_project or args.project or derive_session_project(cwd)
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
                containerd_privilege=containerd_privilege,
                default_project=project,
                cwd=cwd,
            )
            if args.runbook_cmd == "status":
                return _print(daemon.project_runbook_status(project), json_out=args.json)
            if args.runbook_cmd == "update":
                content = args.runbook_content
                if content is None and args.runbook_file is not None:
                    content = args.runbook_file.read_text(encoding="utf-8")
                if content is None:
                    raise RuntimeError("runbook update requires --file or --content")
                return _print(
                    daemon.project_runbook_update(
                        project=project,
                        content=content,
                        mode=args.mode,
                        source=args.source,
                        summary=args.summary,
                    ),
                    json_out=args.json,
                )
            if args.runbook_cmd == "export":
                return _print(
                    daemon.project_runbook_export(
                        project=project,
                        path=args.path,
                        overwrite=args.overwrite,
                    ),
                    json_out=args.json,
                )
            if args.runbook_cmd == "import":
                return _print(
                    daemon.project_runbook_import(
                        project=project,
                        path=args.path,
                        mode=args.mode,
                    ),
                    json_out=args.json,
                )
        if args.cmd == "global-dashboard":
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
                containerd_privilege=containerd_privilege,
                cwd=args.cwd,
            )
            return _print(daemon.global_dashboard(), json_out=args.json)
        if args.cmd == "ingress":
            root = args.state_root or args.state_dir or default_state_root()
            if args.ingress_cmd == "status":
                daemon = WorkerBeeDaemon(
                    state_root=root,
                    runtime=args.runtime,
                    containerd_privilege=containerd_privilege,
                    cwd=args.cwd,
                )
                return _print(daemon.global_dashboard(), json_out=args.json)
            if args.ingress_cmd == "ca":
                if args.ca_action == "regenerate":
                    daemon = WorkerBeeDaemon(
                        state_root=root,
                        runtime=args.runtime,
                        containerd_privilege=containerd_privilege,
                        cwd=args.cwd,
                    )
                    return _print(
                        daemon.ingress_ca_regenerate(confirm=bool(args.confirm_regenerate)),
                        json_out=args.json,
                    )
                return _print(
                    export_global_ingress_ca(root, output=args.output, runtime=args.runtime),
                    json_out=args.json,
                )
        if args.cmd == "trust":
            root = (args.state_root or default_state_root()).resolve()
            if args.trust_cmd == "status":
                return _print(trust_status(root), json_out=args.json)
            if args.trust_cmd == "install":
                return _print(trust_install(root, target=args.target), json_out=args.json)
            if args.trust_cmd == "uninstall":
                return _print(trust_uninstall(root, target=args.target), json_out=args.json)
        if args.cmd == "cleanup":
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
                containerd_privilege=containerd_privilege,
                cwd=args.cwd,
            )
            privilege = ensure_containerd_privilege(
                state_root=args.state_root or default_state_root(),
                runtime=args.runtime,
                mode=containerd_privilege,
            )
            with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
                return _print(
                    daemon.cleanup(execute=args.execute, purge_images=args.purge_images),
                    json_out=args.json,
                )
        if args.cmd == "containerd-privilege":
            root = (args.state_root or default_state_root()).resolve()
            if args.containerd_privilege_cmd == "status":
                return _print(
                    containerd_privilege_status(
                        state_root=root,
                        runtime=args.runtime,
                        mode=containerd_privilege,
                    ),
                    json_out=args.json,
                )
            if args.containerd_privilege_cmd == "stop-helper":
                return _print(stop_containerd_helper(root), json_out=args.json)
            if args.containerd_privilege_cmd == "revoke-acl-leases":
                return _print(
                    release_containerd_socket_access(
                        state_root=root,
                        runtime="containerd",
                        mode="auto",
                        force=True,
                    ),
                    json_out=args.json,
                )
        supervisor_project = args.project or "default"
        sup = WorkerBeeSupervisor(
            project=supervisor_project,
            runtime=args.runtime,
            state_dir=_supervisor_state_dir(args, supervisor_project),
            cwd=args.cwd,
        )
        if args.cmd == "manifest":
            if args.manifest_cmd == "prepare":
                return _print(
                    prepare_stage(
                        supervisor=sup,
                        name=args.name,
                        template=args.template,
                        source=args.source,
                    ),
                    json_out=args.json,
                )
            if args.manifest_cmd == "validate":
                return _print(
                    validate_stage(resolve_stage_dir(sup, args.stage), cwd=sup.cwd),
                    json_out=args.json,
                )
            if args.manifest_cmd == "deploy-local":
                if args.target == "profile":
                    daemon = WorkerBeeDaemon(
                        state_root=args.state_root,
                        runtime=args.runtime,
                        containerd_privilege=containerd_privilege,
                        default_project=args.project or "default",
                        cwd=args.cwd,
                    )
                    privilege = ensure_containerd_privilege(
                        state_root=args.state_root or default_state_root(),
                        runtime=args.runtime,
                        mode=containerd_privilege,
                    )
                    with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
                        result = daemon.manifest_deploy_local(
                            stage=args.stage,
                            target="profile",
                            profile=args.profile,
                            project=args.project,
                            namespace=args.namespace,
                            timeout=args.timeout,
                            k1s_root=args.k1s_root,
                            prune=args.prune,
                        )
                        result["containerd_privilege"] = containerd_privilege_summary(privilege)
                        return _print(result, json_out=args.json)
                daemon = WorkerBeeDaemon(
                    state_root=args.state_root,
                    runtime=args.runtime,
                    containerd_privilege=containerd_privilege,
                    default_project=args.project or "default",
                    cwd=args.cwd,
                )
                result = daemon.manifest_deploy_local(
                    stage=args.stage,
                    target="workerbee",
                    project=args.project,
                    namespace=args.namespace,
                    timeout=args.timeout,
                    prune=args.prune,
                )
                if str(args.runtime).lower() == CONTAINERD_RUNTIME:
                    privilege = ensure_containerd_privilege(
                        state_root=args.state_root or default_state_root(),
                        runtime=args.runtime,
                        mode=containerd_privilege,
                    )
                    result.setdefault(
                        "containerd_privilege",
                        containerd_privilege_summary(privilege),
                    )
                return _print(result, json_out=args.json)
            if args.manifest_cmd == "deploy-k1s":
                return _print(
                    deploy_remote_k1s_stage(
                        supervisor=sup,
                        stage_dir=resolve_stage_dir(sup, args.stage),
                        server=args.server,
                        token=args.token,
                        namespace=args.namespace,
                        timeout=args.timeout,
                        allow_remote_secretrefs=args.allow_remote_secretrefs,
                    ),
                    json_out=args.json,
                )
        if args.cmd == "bundle" and args.bundle_cmd == "export":
            return _print(
                export_bundle(
                    supervisor=sup,
                    stage_dir=resolve_stage_dir(sup, args.stage),
                    fmt=args.format,
                    namespace=args.namespace,
                ),
                json_out=args.json,
            )
        if args.cmd == "security" and args.security_cmd == "assess":
            return _print(
                assess_stage_security(
                    supervisor=sup,
                    stage_dir=resolve_stage_dir(sup, args.stage),
                    namespace=args.namespace,
                    target=args.target,
                    checks=args.check,
                    runtime_probe=None,
                    timeout=args.timeout,
                ),
                json_out=args.json,
            )
        if args.cmd == "security" and args.security_cmd == "review-project":
            review_stage = resolve_stage_dir(sup, args.stage) if args.stage is not None else None
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
                containerd_privilege=containerd_privilege,
                default_project=args.project or "default",
                cwd=args.cwd,
            )
            return _print(
                daemon.security_review_project(
                    project=args.project,
                    stage=review_stage,
                    target=args.target,
                    namespace=args.namespace,
                    checks=args.check,
                    timeout=args.timeout,
                ),
                json_out=args.json,
            )
        if args.cmd == "start":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=lambda: sup.start().public_dict(),
                ),
                json_out=args.json,
            )
        if args.cmd == "stop":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=lambda: sup.stop(purge=args.purge),
                ),
                json_out=args.json,
            )
        if args.cmd == "status":
            return _print(sup.status(), json_out=args.json)
        if args.cmd == "tls-info":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=sup.tls_info,
                ),
                json_out=args.json,
            )
        if args.cmd == "reset":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=sup.reset,
                ),
                json_out=args.json,
            )
        if args.cmd == "poc-status":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=sup.poc_status,
                ),
                json_out=args.json,
            )
        if args.cmd == "build-image":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=lambda: sup.build_image(
                        args.context,
                        tag=args.tag,
                        dockerfile=args.dockerfile,
                    ),
                ),
                json_out=args.json,
            )
        if args.cmd == "deploy":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=lambda: sup.deploy_manifest(
                        args.file,
                        namespace=args.namespace,
                        timeout=args.timeout,
                    ),
                ),
                json_out=args.json,
            )
        if args.cmd == "deploy-poc":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=lambda: sup.deploy_poc_stack(timeout_seconds=args.timeout),
                ),
                json_out=args.json,
            )
        if args.cmd == "apishim-smoke":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=sup.apishim_smoke,
                ),
                json_out=args.json,
            )
        if args.cmd == "logs":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=lambda: sup.logs(
                        app=args.app,
                        namespace=args.namespace,
                        tail=args.tail,
                        include_exited=args.include_exited,
                    ),
                ),
                json_out=args.json,
            )
        if args.cmd == "exec":
            command = list(args.command)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                command = ["sh", "-c", "id && pwd"]
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=lambda: sup.run_exec(
                        args.app,
                        command,
                        namespace=args.namespace,
                    ),
                ),
                json_out=args.json,
            )
        if args.cmd == "export-k8s":
            return _print(
                _run_supervisor_action(
                    args=args,
                    supervisor=sup,
                    containerd_privilege=containerd_privilege,
                    action=sup.export_k8s,
                ),
                json_out=args.json,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"workerbee: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unsupported command: {args.cmd}")
    return 2


def _supervisor_state_dir(args: argparse.Namespace, project: str) -> Path | None:
    if args.state_dir is not None:
        return args.state_dir
    if args.state_root is not None:
        return daemon_project_state_dir(project, state_root=args.state_root)
    return None


def _run_supervisor_action(
    *,
    args: argparse.Namespace,
    supervisor: WorkerBeeSupervisor,
    containerd_privilege: str,
    action: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if str(args.runtime).lower() != CONTAINERD_RUNTIME:
        return action()
    privilege = ensure_containerd_privilege(
        state_root=supervisor.state_dir.parent.parent,
        runtime=args.runtime,
        mode=containerd_privilege,
    )
    with temporary_containerd_privilege_env(containerd_privilege_env(privilege)):
        result = action()
    result.setdefault("containerd_privilege", containerd_privilege_summary(privilege))
    return result


def _print(payload: dict[str, Any], *, json_out: bool) -> int:
    if json_out:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if payload.get("ok") is False else 0
    if payload.get("user_message"):
        print(payload["user_message"])
        if payload.get("dashboard_url"):
            print(f"dashboard: {payload['dashboard_url']}")
        return 0
    if "mcp_url" in payload:
        print(f"mcp: {payload['mcp_url']}")
        if payload.get("dashboard_url"):
            print(f"dashboard: {payload['dashboard_url']}")
        if payload.get("dashboard_ca_download_url"):
            print(f"ca dashboard: {payload['dashboard_ca_download_url']}")
        if payload.get("ca_download_url"):
            print(f"ca: {payload['ca_download_url']}")
        _print_ca_guidance(payload)
        dns = payload.get("dns")
        if isinstance(dns, dict) and dns.get("enabled"):
            print(
                f"dns: {dns.get('bind_host')}:{dns.get('port')} "
                f"({dns.get('base_domain') or payload.get('ingress_domain')})"
            )
        if payload.get("codex_mcp_add"):
            print(f"codex: {payload['codex_mcp_add']}")
        if payload.get("agent_instructions"):
            print(f"agents: {payload['agent_instructions']}")
        print(f"running: {payload.get('running')}")
        print(f"state: {payload.get('state_root')}")
        return 1 if payload.get("ok") is False else 0
    if payload.get("ca_export"):
        print(f"ca exported: {payload.get('output')}")
        print(f"ca source: {payload.get('ca_bundle')}")
        print(f"ca sha256: {payload.get('ca_sha256')}")
        if payload.get("ca_download_url"):
            print(f"ca url: {payload['ca_download_url']}")
        if payload.get("dashboard_ca_download_url"):
            print(f"ca dashboard: {payload['dashboard_ca_download_url']}")
        return 1 if payload.get("ok") is False else 0
    if _is_global_ingress_payload(payload):
        if payload.get("dashboard_url"):
            print(f"dashboard: {payload['dashboard_url']}")
        print(f"ingress: {'running' if payload.get('running') else 'stopped'}")
        if payload.get("ca_bundle"):
            print(f"ca bundle: {payload['ca_bundle']}")
        if payload.get("ca_sha256"):
            print(f"ca sha256: {payload['ca_sha256']}")
        if payload.get("ca_download_url"):
            print(f"ca url: {payload['ca_download_url']}")
        if payload.get("dashboard_ca_download_url"):
            print(f"ca dashboard: {payload['dashboard_ca_download_url']}")
        _print_ca_guidance(payload)
        dns = payload.get("dns")
        if isinstance(dns, dict) and dns.get("enabled"):
            print(
                f"dns: {dns.get('bind_host')}:{dns.get('port')} "
                f"({dns.get('base_domain') or payload.get('base_domain')})"
            )
        print(f"state: {payload.get('state_root')}")
        return 1 if payload.get("ok") is False else 0
    if "system_trust_backend" in payload:
        print(f"ca bundle: {payload.get('ca_bundle')}")
        print(f"ca ready: {payload.get('ca_ready')}")
        if payload.get("ca_sha256"):
            print(f"ca sha256: {payload['ca_sha256']}")
        print(f"system trust: {payload.get('system_trust_backend')}")
        _print_ca_guidance(payload)
        return 1 if payload.get("ok") is False else 0
    if "dashboard_url" in payload:
        print(f"dashboard: {payload['dashboard_url']}")
        print(f"controller: {payload.get('controller_url')}")
        print(f"apishim: {payload.get('apishim_url')}")
        print(f"state: {payload.get('state_dir')}")
        return 0
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if payload.get("ok") is False else 0


def _extract_ca_commands(payload: dict[str, Any]) -> dict[str, Any]:
    commands = payload.get("ca_commands") or payload.get("commands")
    if isinstance(commands, dict):
        return commands
    global_dashboard = payload.get("global_dashboard")
    if isinstance(global_dashboard, dict):
        commands = global_dashboard.get("ca_commands")
        if isinstance(commands, dict):
            return commands
    return {}


def _print_ca_guidance(payload: dict[str, Any]) -> None:
    commands = _extract_ca_commands(payload)
    if not commands:
        return
    export = commands.get("export")
    regenerate = commands.get("regenerate")
    trust_system = commands.get("trust_system") or commands.get("install_system")
    trust_nss = commands.get("trust_nss") or commands.get("install_nss")
    download_curl = commands.get("download_curl")
    if export:
        print(f"ca export: {export}")
    if regenerate:
        print(f"ca regenerate: {regenerate}")
    if trust_system:
        print(f"local trust: {trust_system}")
    if trust_nss:
        print(f"browser trust: {trust_nss}")
    if download_curl:
        print(f"lan device: {download_curl}")


def _is_global_ingress_payload(payload: dict[str, Any]) -> bool:
    return (
        "enabled" in payload
        and "state_root" in payload
        and any(
            key in payload
            for key in (
                "base_domain",
                "ca_bundle",
                "caddy_container",
                "https_port",
            )
        )
    )


def _handle_config(args: argparse.Namespace) -> int:
    path = default_config_file()
    if args.config_cmd == "path":
        return _print({"ok": True, "path": str(path)}, json_out=args.json)
    if args.config_cmd == "show":
        return _print(
            {
                "ok": True,
                "path": str(path),
                "config": load_cli_config(path),
                "effective": cli_defaults(path),
            },
            json_out=args.json,
        )
    if args.config_cmd == "clear":
        cleared = clear_cli_config(path)
        return _print({"ok": True, "path": str(cleared), "cleared": True}, json_out=args.json)
    if args.config_cmd == "set":
        updates = {
            "runtime": args.runtime,
            "containerd_privilege": args.containerd_privilege,
            "state_root": str(args.state_root.expanduser()) if args.state_root else None,
            "project": args.project,
            "mcp_host": args.mcp_host,
            "mcp_port": args.mcp_port,
            "mcp_timeout": args.mcp_timeout,
            "ingress_exposure": args.ingress_exposure,
            "ingress_domain": args.ingress_domain,
            "ingress_bind": args.ingress_bind,
            "ingress_ca_port": args.ingress_ca_port,
            "ingress_dns": args.ingress_dns,
            "ingress_dns_port": args.ingress_dns_port,
            "ingress_dns_bind": args.ingress_dns_bind,
            "ingress_dns_answer": args.ingress_dns_answer,
            "ingress_dns_upstream": args.ingress_dns_upstream,
        }
        updates = {key: value for key, value in updates.items() if value not in (None, "")}
        if not updates:
            raise RuntimeError("config set requires at least one default option")
        config = load_cli_config(path)
        config.update(updates)
        saved = save_cli_config(config, path)
        return _print(
            {"ok": True, "path": str(saved), "config": load_cli_config(saved)},
            json_out=args.json,
        )
    raise RuntimeError(f"unknown config command: {args.config_cmd}")


def _apply_cli_defaults(args: argparse.Namespace, argv: list[str]) -> None:
    defaults = cli_defaults()
    if not _arg_present(argv, "--runtime") and defaults.get("runtime"):
        args.runtime = str(defaults["runtime"])
    if (
        not _arg_present(argv, "--containerd-privilege")
        and defaults.get("containerd_privilege")
    ):
        args.containerd_privilege = str(defaults["containerd_privilege"])
    if not _arg_present(argv, "--state-root") and defaults.get("state_root"):
        args.state_root = Path(str(defaults["state_root"])).expanduser()
    if not _arg_present(argv, "--project") and defaults.get("project"):
        args.project = str(defaults["project"])
    if args.cmd == "mcp":
        if not _arg_present(argv, "--host") and defaults.get("mcp_host"):
            args.host = str(defaults["mcp_host"])
        if not _arg_present(argv, "--port") and defaults.get("mcp_port"):
            args.port = int(defaults["mcp_port"])
        if (
            args.mcp_cmd in {"start", "stop", "restart"}
            and not _arg_present(argv, "--timeout")
            and defaults.get("mcp_timeout")
        ):
            args.timeout = float(defaults["mcp_timeout"])
        if hasattr(args, "ingress_exposure"):
            if not _arg_present(argv, "--ingress-exposure") and defaults.get("ingress_exposure"):
                args.ingress_exposure = str(defaults["ingress_exposure"])
            if not _arg_present(argv, "--ingress-domain") and defaults.get("ingress_domain"):
                args.ingress_domain = str(defaults["ingress_domain"])
            if not _arg_present(argv, "--ingress-bind") and defaults.get("ingress_bind"):
                args.ingress_bind = str(defaults["ingress_bind"])
            if not _arg_present(argv, "--ingress-ca-port") and defaults.get("ingress_ca_port"):
                args.ingress_ca_port = int(defaults["ingress_ca_port"])
            if not _arg_present(argv, "--ingress-dns") and defaults.get("ingress_dns"):
                args.ingress_dns = str(defaults["ingress_dns"])
            if not _arg_present(argv, "--ingress-dns-port") and defaults.get(
                "ingress_dns_port"
            ):
                args.ingress_dns_port = int(defaults["ingress_dns_port"])
            if not _arg_present(argv, "--ingress-dns-bind") and defaults.get(
                "ingress_dns_bind"
            ):
                args.ingress_dns_bind = str(defaults["ingress_dns_bind"])
            if not _arg_present(argv, "--ingress-dns-answer") and defaults.get(
                "ingress_dns_answer"
            ):
                args.ingress_dns_answer = str(defaults["ingress_dns_answer"])
            if not _arg_present(argv, "--ingress-dns-upstream") and defaults.get(
                "ingress_dns_upstream"
            ):
                args.ingress_dns_upstream = str(defaults["ingress_dns_upstream"]).split()


def _arg_present(argv: list[str], option: str) -> bool:
    prefix = f"{option}="
    return any(arg == option or arg.startswith(prefix) for arg in argv)


def _doctor(
    *,
    runtime: str = "auto",
    state_root: Path | None = None,
    containerd_privilege: str = "auto",
) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "python": sys.version.split()[0],
        "podman": shutil.which("podman"),
        "docker": shutil.which("docker"),
        "nerdctl": shutil.which("nerdctl"),
        "buildctl": shutil.which("buildctl"),
    }
    root = (state_root or default_state_root()).resolve()
    checks["runtime"] = runtime_diagnostics(runtime, state_root=root)
    checks["containerd_runtime"] = runtime_diagnostics("containerd", state_root=root)
    checks["containerd_privilege"] = containerd_privilege_status(
        state_root=root,
        runtime=runtime,
        mode=containerd_privilege,
    )
    try:
        runtime = resolve_k1s_runtime()
        checks["k1s_runtime_source"] = runtime.source
        checks["k1s_root"] = str(runtime.k1s_root) if runtime.k1s_root else None
        checks["k1s_python"] = runtime.python_executable
        checks["ae_origin"] = runtime.ae_origin
        checks["ae_version"] = getattr(runtime, "ae_version", None)
        env = runtime.apply_env(os.environ.copy())
        proc = subprocess.run(
            [runtime.python_executable, "-m", "ae.cli", "--help"],
            env=env,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        checks["ae_import"] = proc.returncode == 0
        if proc.returncode != 0:
            checks["ae_import_error"] = proc.stderr[-500:]
        helper = subprocess.run(
            [
                runtime.python_executable,
                "-c",
                "from ae.apishim.env import ensure_local_apishim_env",
            ],
            env=env,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        checks["apishim_env_helper"] = helper.returncode == 0
        if helper.returncode != 0:
            checks["apishim_env_helper_error"] = helper.stderr[-500:]
    except Exception as exc:  # noqa: BLE001
        checks["k1s_runtime_error"] = str(exc)
        checks["ae_import"] = False
    try:
        import mcp  # noqa: F401

        checks["mcp_sdk"] = True
    except Exception as exc:  # noqa: BLE001
        checks["mcp_sdk"] = False
        checks["mcp_sdk_error"] = str(exc)
    checks["ok"] = (
        bool(checks.get("ae_import"))
        and bool(checks.get("apishim_env_helper"))
        and bool(checks.get("runtime", {}).get("ok"))
    )
    return checks


def _containerd_privilege_arg(args: argparse.Namespace) -> str:
    if getattr(args, "containerd_socket_access", None) == "off":
        return "unprivileged"
    return str(getattr(args, "containerd_privilege", "auto") or "auto")
