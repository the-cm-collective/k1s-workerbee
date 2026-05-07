"""WorkerBee CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from workerbee import __version__
from workerbee.agent import derive_session_project
from workerbee.containerd_access import release_containerd_socket_access
from workerbee.containerd_helper import (
    containerd_privilege_env,
    containerd_privilege_status,
    ensure_containerd_privilege,
    stop_containerd_helper,
    temporary_containerd_privilege_env,
)
from workerbee.daemon import WorkerBeeDaemon
from workerbee.k1s_runtime import resolve_k1s_runtime
from workerbee.manifests import (
    deploy_local_stage,
    deploy_remote_k1s_stage,
    export_bundle,
    prepare_stage,
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
from workerbee.paths import default_state_root
from workerbee.runtime_support import runtime_diagnostics
from workerbee.supervisor import WorkerBeeSupervisor
from workerbee.trust import trust_install, trust_status, trust_uninstall


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
    sub.add_parser("global-dashboard", help="Show WorkerBee global dashboard status")
    ingress = sub.add_parser("ingress", help="Inspect WorkerBee global ingress")
    ingress_sub = ingress.add_subparsers(dest="ingress_cmd", required=True)
    ingress_sub.add_parser("status", help="Show global ingress status")
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
        choices=["stateless-web", "frontend-api", "frontend-api-store"],
    )
    manifest_prepare.add_argument("--source", type=Path, default=None)
    manifest_validate = manifest_sub.add_parser("validate", help="Validate staged files")
    manifest_validate.add_argument("--stage", type=Path, required=True)
    manifest_local = manifest_sub.add_parser("deploy-local", help="Deploy staged files locally")
    manifest_local.add_argument("--stage", type=Path, required=True)
    manifest_local.add_argument("-n", "--namespace", default=None)
    manifest_local.add_argument("--timeout", type=int, default=180)
    manifest_k1s = manifest_sub.add_parser("deploy-k1s", help="Deploy staged files to remote k1s")
    manifest_k1s.add_argument("--stage", type=Path, required=True)
    manifest_k1s.add_argument("--server", required=True)
    manifest_k1s.add_argument("--token", required=True)
    manifest_k1s.add_argument("-n", "--namespace", default=None)
    manifest_k1s.add_argument("--timeout", type=int, default=180)
    bundle = sub.add_parser("bundle", help="Export staged artifacts")
    bundle_sub = bundle.add_subparsers(dest="bundle_cmd", required=True)
    bundle_export = bundle_sub.add_parser("export", help="Export k1s, k8s, or Helm bundle")
    bundle_export.add_argument("--stage", type=Path, required=True)
    bundle_export.add_argument("--format", choices=["k1s", "k8s", "helm"], default="k1s")
    bundle_export.add_argument("-n", "--namespace", default=None)
    deploy = sub.add_parser("deploy-poc", help="Build and deploy the representative POC stack")
    deploy.add_argument("--timeout", type=float, default=180.0)
    sub.add_parser("apishim-smoke", help="Inspect POC objects through the k1s API shim")
    logs = sub.add_parser("logs", help="Show recent POC app logs")
    logs.add_argument("app", nargs="?", default="api")
    logs.add_argument("--tail", type=int, default=80)
    exec_p = sub.add_parser("exec", help="Run a command in a POC app container")
    exec_p.add_argument("app")
    exec_p.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("export-k8s", help="Export POC manifests to Kubernetes YAML")

    mcp = sub.add_parser("mcp", help="Run the MCP server")
    mcp_sub = mcp.add_subparsers(dest="mcp_cmd", required=True)
    def add_mcp_bind_flags(command: argparse.ArgumentParser) -> None:
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8765)

    start_mcp = mcp_sub.add_parser("start", help="Start WorkerBee MCP in the background")
    add_mcp_bind_flags(start_mcp)
    start_mcp.add_argument("--timeout", type=float, default=45.0)
    stop_mcp = mcp_sub.add_parser("stop", help="Stop the background WorkerBee MCP daemon")
    add_mcp_bind_flags(stop_mcp)
    stop_mcp.add_argument("--timeout", type=float, default=10.0)
    restart_mcp = mcp_sub.add_parser("restart", help="Restart WorkerBee MCP in the background")
    add_mcp_bind_flags(restart_mcp)
    restart_mcp.add_argument("--timeout", type=float, default=45.0)
    status_mcp = mcp_sub.add_parser("status", help="Show background WorkerBee MCP status")
    add_mcp_bind_flags(status_mcp)
    serve = mcp_sub.add_parser("serve", help="Serve WorkerBee over Streamable HTTP MCP")
    add_mcp_bind_flags(serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    containerd_privilege = _containerd_privilege_arg(args)
    try:
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
            daemon = WorkerBeeDaemon(state_root=args.state_root, runtime=args.runtime, cwd=args.cwd)
            return _print(daemon.projects(), json_out=args.json)
        if args.cmd == "project":
            cwd = args.project_cwd or args.cwd or Path.cwd()
            project = args.project_name or args.project or derive_session_project(cwd)
            daemon = WorkerBeeDaemon(
                state_root=args.state_root,
                runtime=args.runtime,
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
        if args.cmd == "global-dashboard":
            daemon = WorkerBeeDaemon(state_root=args.state_root, runtime=args.runtime, cwd=args.cwd)
            return _print(daemon.global_dashboard(), json_out=args.json)
        if args.cmd == "ingress":
            daemon = WorkerBeeDaemon(state_root=args.state_root, runtime=args.runtime, cwd=args.cwd)
            return _print(daemon.global_dashboard(), json_out=args.json)
        if args.cmd == "trust":
            root = (args.state_root or default_state_root()).resolve()
            if args.trust_cmd == "status":
                return _print(trust_status(root), json_out=args.json)
            if args.trust_cmd == "install":
                return _print(trust_install(root, target=args.target), json_out=args.json)
            if args.trust_cmd == "uninstall":
                return _print(trust_uninstall(root, target=args.target), json_out=args.json)
        if args.cmd == "cleanup":
            daemon = WorkerBeeDaemon(state_root=args.state_root, runtime=args.runtime, cwd=args.cwd)
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
        sup = WorkerBeeSupervisor(
            project=args.project or "default",
            runtime=args.runtime,
            state_dir=args.state_dir,
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
                return _print(validate_stage(args.stage), json_out=args.json)
            if args.manifest_cmd == "deploy-local":
                return _print(
                    deploy_local_stage(
                        supervisor=sup,
                        stage_dir=args.stage,
                        namespace=args.namespace,
                        timeout=args.timeout,
                    ),
                    json_out=args.json,
                )
            if args.manifest_cmd == "deploy-k1s":
                return _print(
                    deploy_remote_k1s_stage(
                        supervisor=sup,
                        stage_dir=args.stage,
                        server=args.server,
                        token=args.token,
                        namespace=args.namespace,
                        timeout=args.timeout,
                    ),
                    json_out=args.json,
                )
        if args.cmd == "bundle" and args.bundle_cmd == "export":
            return _print(
                export_bundle(
                    supervisor=sup,
                    stage_dir=args.stage,
                    fmt=args.format,
                    namespace=args.namespace,
                ),
                json_out=args.json,
            )
        if args.cmd == "start":
            info = sup.start()
            return _print(info.public_dict(), json_out=args.json)
        if args.cmd == "stop":
            return _print(sup.stop(purge=args.purge), json_out=args.json)
        if args.cmd == "status":
            return _print(sup.status(), json_out=args.json)
        if args.cmd == "tls-info":
            return _print(sup.tls_info(), json_out=args.json)
        if args.cmd == "reset":
            return _print(sup.reset(), json_out=args.json)
        if args.cmd == "poc-status":
            return _print(sup.poc_status(), json_out=args.json)
        if args.cmd == "build-image":
            return _print(sup.build_image(args.context, tag=args.tag), json_out=args.json)
        if args.cmd == "deploy":
            return _print(
                sup.deploy_manifest(args.file, namespace=args.namespace, timeout=args.timeout),
                json_out=args.json,
            )
        if args.cmd == "deploy-poc":
            return _print(sup.deploy_poc_stack(timeout_seconds=args.timeout), json_out=args.json)
        if args.cmd == "apishim-smoke":
            return _print(sup.apishim_smoke(), json_out=args.json)
        if args.cmd == "logs":
            return _print(sup.logs(app=args.app, tail=args.tail), json_out=args.json)
        if args.cmd == "exec":
            command = list(args.command)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                command = ["sh", "-c", "id && pwd"]
            return _print(sup.run_exec(args.app, command), json_out=args.json)
        if args.cmd == "export-k8s":
            return _print(sup.export_k8s(), json_out=args.json)
    except Exception as exc:  # noqa: BLE001
        print(f"workerbee: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unsupported command: {args.cmd}")
    return 2


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
        print(f"running: {payload.get('running')}")
        print(f"state: {payload.get('state_root')}")
        return 1 if payload.get("ok") is False else 0
    if "dashboard_url" in payload:
        print(f"dashboard: {payload['dashboard_url']}")
        print(f"controller: {payload.get('controller_url')}")
        print(f"apishim: {payload.get('apishim_url')}")
        print(f"state: {payload.get('state_dir')}")
        return 0
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if payload.get("ok") is False else 0


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
