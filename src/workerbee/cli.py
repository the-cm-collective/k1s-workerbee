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
from workerbee.daemon import WorkerBeeDaemon
from workerbee.k1s_runtime import resolve_k1s_runtime
from workerbee.mcp_server import serve_mcp
from workerbee.paths import default_state_root
from workerbee.supervisor import WorkerBeeSupervisor
from workerbee.trust import trust_install, trust_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workerbee")
    parser.add_argument("--version", action="version", version=f"workerbee {__version__}")
    parser.add_argument("--project", default="default", help="WorkerBee project name")
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
        choices=["auto", "podman", "docker"],
        help="Container runtime backend",
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
    sub.add_parser("global-dashboard", help="Show WorkerBee global dashboard status")
    ingress = sub.add_parser("ingress", help="Inspect WorkerBee global ingress")
    ingress_sub = ingress.add_subparsers(dest="ingress_cmd", required=True)
    ingress_sub.add_parser("status", help="Show global ingress status")
    trust = sub.add_parser("trust", help="Manage explicit local CA trust")
    trust_sub = trust.add_subparsers(dest="trust_cmd", required=True)
    trust_sub.add_parser("status", help="Show local CA trust status")
    trust_sub.add_parser("install", help="Install WorkerBee Caddy CA into local trust")
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
    serve = mcp_sub.add_parser("serve", help="Serve WorkerBee over Streamable HTTP MCP")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "doctor":
            return _print(_doctor(), json_out=args.json)
        if args.cmd == "mcp":
            serve_mcp(
                project=args.project,
                runtime=args.runtime,
                host=args.host,
                port=args.port,
                state_dir=args.state_dir,
                state_root=args.state_root,
            )
            return 0
        if args.cmd == "projects":
            daemon = WorkerBeeDaemon(state_root=args.state_root, runtime=args.runtime)
            return _print(daemon.projects(), json_out=args.json)
        if args.cmd == "global-dashboard":
            daemon = WorkerBeeDaemon(state_root=args.state_root, runtime=args.runtime)
            return _print(daemon.global_dashboard(), json_out=args.json)
        if args.cmd == "ingress":
            daemon = WorkerBeeDaemon(state_root=args.state_root, runtime=args.runtime)
            return _print(daemon.global_dashboard(), json_out=args.json)
        if args.cmd == "trust":
            root = (args.state_root or default_state_root()).resolve()
            if args.trust_cmd == "status":
                return _print(trust_status(root), json_out=args.json)
            if args.trust_cmd == "install":
                return _print(trust_install(root), json_out=args.json)
        sup = WorkerBeeSupervisor(
            project=args.project,
            runtime=args.runtime,
            state_dir=args.state_dir,
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
        return 0
    if "dashboard_url" in payload:
        print(f"dashboard: {payload['dashboard_url']}")
        print(f"controller: {payload.get('controller_url')}")
        print(f"apishim: {payload.get('apishim_url')}")
        print(f"state: {payload.get('state_dir')}")
        return 0
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _doctor() -> dict[str, Any]:
    checks: dict[str, Any] = {
        "python": sys.version.split()[0],
        "podman": shutil.which("podman"),
        "docker": shutil.which("docker"),
    }
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
        and bool(checks.get("podman") or checks.get("docker"))
    )
    return checks
