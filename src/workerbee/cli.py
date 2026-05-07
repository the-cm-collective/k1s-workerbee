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
from workerbee.mcp_server import serve_mcp
from workerbee.paths import resolve_k1s_python, resolve_k1s_root
from workerbee.supervisor import WorkerBeeSupervisor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workerbee")
    parser.add_argument("--version", action="version", version=f"workerbee {__version__}")
    parser.add_argument("--project", default="default", help="WorkerBee project name")
    parser.add_argument("--state-dir", type=Path, default=None, help="Override state directory")
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
    sub.add_parser("poc-status", help="Show POC app status through the native k1s API")
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
            )
            return 0
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
        if args.cmd == "poc-status":
            return _print(sup.poc_status(), json_out=args.json)
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
        k1s_root = resolve_k1s_root()
        checks["k1s_root"] = str(k1s_root)
        python_bin = resolve_k1s_python(k1s_root)
        checks["k1s_python"] = python_bin
        env = os.environ.copy()
        env["PYTHONPATH"] = str(k1s_root / "src")
        proc = subprocess.run(
            [python_bin, "-m", "ae.cli", "--help"],
            env=env,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        checks["ae_import"] = proc.returncode == 0
        if proc.returncode != 0:
            checks["ae_import_error"] = proc.stderr[-500:]
    except Exception as exc:  # noqa: BLE001
        checks["k1s_root_error"] = str(exc)
        checks["ae_import"] = False
    try:
        import mcp  # noqa: F401

        checks["mcp_sdk"] = True
    except Exception as exc:  # noqa: BLE001
        checks["mcp_sdk"] = False
        checks["mcp_sdk_error"] = str(exc)
    checks["ok"] = bool(checks.get("ae_import")) and bool(
        checks.get("podman") or checks.get("docker")
    )
    return checks
