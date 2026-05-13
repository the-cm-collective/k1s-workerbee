from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from workerbee.containerd_helper import (
    containerd_privilege_env,
    ensure_containerd_privilege,
    remove_containerd_helper_tree,
    stop_containerd_helper,
    temporary_containerd_privilege_env,
)
from workerbee.contract import WorkerBeeError
from workerbee.daemon import (
    WorkerBeeDaemon,
    _copy_realtime_contexts,
    _websocket_probe,
)
from workerbee.manifests import deploy_remote_k1s_stage, prepare_stage
from workerbee.paths import find_k1s_root
from workerbee.remote_k1s_target import RemoteK1sTarget
from workerbee.runtime_support import CONTAINERD_RUNTIME
from workerbee.supervisor import project_slug

pytestmark = pytest.mark.skipif(
    os.getenv("WORKERBEE_LIVE_REMOTE_K1S_DEPLOY") != "1",
    reason="set WORKERBEE_LIVE_REMOTE_K1S_DEPLOY=1 to run the live remote k1s deploy scenario",
)


def test_live_workerbee_deploys_realtime_app_to_local_remote_k1s(tmp_path: Path) -> None:
    runtime = os.getenv("WORKERBEE_REMOTE_K1S_RUNTIME", CONTAINERD_RUNTIME)
    if runtime != CONTAINERD_RUNTIME:
        pytest.skip("remote k1s live deploy test currently requires runtime=containerd")
    project = project_slug(os.getenv("WORKERBEE_REMOTE_K1S_PROJECT", "remote-k1s-live-app"))
    state_root = tmp_path / "workerbee-state"
    report_path = state_root / "global" / "remote-k1s-live-report.json"
    hold_for_inspection = os.getenv("WORKERBEE_LIVE_REMOTE_K1S_KEEP") == "1"
    raw_k1s_root = os.getenv("WORKERBEE_LIVE_REMOTE_K1S_ROOT")
    repo_root = Path(__file__).resolve().parents[1]
    k1s_root = (
        Path(raw_k1s_root).expanduser().resolve()
        if raw_k1s_root
        else find_k1s_root(repo_root)
    )
    if k1s_root is None or not k1s_root.is_dir():
        pytest.skip("could not locate k1s checkout; set WORKERBEE_LIVE_REMOTE_K1S_ROOT")
    daemon = WorkerBeeDaemon(
        state_root=state_root,
        runtime=runtime,
        default_project=project,
        cwd=repo_root,
    )
    target: RemoteK1sTarget | None = None
    report: dict[str, Any] | None = None
    host_conflict_before = _host_conflict_snapshot()

    try:
        privilege = ensure_containerd_privilege(
            state_root=state_root,
            runtime=runtime,
            mode=os.getenv("WORKERBEE_CONTAINERD_PRIVILEGE", "auto"),
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001 - live prereq skip
        pytest.skip(f"containerd privilege is unavailable: {exc}")
    privilege_env = containerd_privilege_env(privilege)

    try:
        with temporary_containerd_privilege_env(privilege_env):
            ingress = daemon.start(mcp_bind_url="pytest-live-remote-k1s")
            supervisor = daemon.supervisor(project)
            contexts = _copy_realtime_contexts(state_root, project)
            builds = [
                supervisor.build_image(
                    path,
                    tag=_local_containerd_realtime_image(project, app),
                )
                for app, path in contexts.items()
            ]
            prepared = prepare_stage(
                supervisor=supervisor,
                name="remote-k1s-realtime",
                template="realtime-web-db",
            )
            _rewrite_stage_images_for_local_containerd(
                Path(str(prepared["stage_dir"])),
                project=project,
            )
            target = RemoteK1sTarget(
                state_root=state_root,
                project=project,
                cwd=supervisor.cwd,
                ingress=supervisor.ingress,
                k1s_root=k1s_root,
            )
            target_info = target.start(timeout=240.0)
            deploy = deploy_remote_k1s_stage(
                supervisor=supervisor,
                stage_dir=Path(str(prepared["stage_dir"])),
                server=target_info.controller_url,
                token=target_info.admin_token,
                namespace=project,
                timeout=300,
            )
            frontend = _wait_for_ingress_probe(
                daemon,
                project=project,
                host=f"app.{project}.workerbee.localhost",
                path="/healthz",
                timeout_seconds=120.0,
            )
            backend = _wait_for_ingress_probe(
                daemon,
                project=project,
                host=f"api.{project}.workerbee.localhost",
                path="/healthz",
                timeout_seconds=120.0,
            )
            websocket = _websocket_probe(
                f"wss://api.{project}.workerbee.localhost:{ingress.https_port}/ws",
                ca_bundle=str(ingress.ca_bundle),
                expected="echo:workerbee",
                timeout=60.0,
            )
            host_conflict_check = _host_conflict_check(host_conflict_before)
            report = {
                "api_version": "workerbee.remote_k1s_live/v1",
                "scenario": "remote-k1s-realtime-deploy",
                "project": project,
                "runtime": runtime,
                "state_root": str(state_root),
                "stage_dir": prepared["stage_dir"],
                "builds": [_compact_build(item) for item in builds],
                "target": target_info.public_dict(),
                "deploy": _compact_deploy(deploy),
                "frontend": _compact_probe(frontend),
                "backend": _compact_probe(backend),
                "websocket": websocket,
                "host_conflict_check": host_conflict_check,
                "cleanup_command": target_info.cleanup_command,
                "manual_inspection": hold_for_inspection,
                "generated_at": time.time(),
            }
            _write_report(report_path, report)

            assert all(item["ok"] for item in builds)
            assert deploy["ok"] is True
            assert frontend["ok"] is True
            assert backend["ok"] is True
            assert websocket["ok"] is True
            assert host_conflict_check["ok"] is True
            assert report_path.is_file()
            if hold_for_inspection:
                _pause_for_manual_inspection(target_info.dashboard_url, target_info.cleanup_command)
    finally:
        if report is not None:
            _write_report(report_path, report)
        with temporary_containerd_privilege_env(privilege_env):
            with suppress(Exception):
                if target is not None:
                    target.stop()
            with suppress(Exception):
                daemon.cleanup(execute=True, purge_images=True)
            with suppress(Exception):
                daemon.stop_global_ingress()
            for path in (
                state_root / "projects" / project,
                state_root / "global" / "caddy-data",
                state_root / "global" / "containerd-cni-net.d",
                state_root / "global" / "containerd-data",
            ):
                with suppress(Exception):
                    remove_containerd_helper_tree(state_root, path)
        with suppress(Exception):
            stop_containerd_helper(state_root)


def _wait_for_ingress_probe(
    daemon: WorkerBeeDaemon,
    *,
    project: str,
    host: str,
    path: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            result = daemon.ingress_probe(
                project=project,
                host=host,
                path=path,
                expected_status=200,
                timeout=5.0,
            )
        except WorkerBeeError as exc:
            last = {
                "ok": False,
                "host": host,
                "path": path,
                "error": str(exc),
                "workerbee_error": exc.public_dict(),
            }
        except Exception as exc:  # noqa: BLE001
            last = {"ok": False, "host": host, "path": path, "error": str(exc)}
        else:
            last = result
            if result.get("ok"):
                return result
        time.sleep(2.0)
    return last or {"ok": False, "host": host, "path": path, "error": "probe timed out"}


def _compact_build(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "tag": result.get("tag"),
        "runtime": result.get("runtime"),
        "build_backend": result.get("build_backend"),
    }


def _compact_deploy(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "project": result.get("project"),
        "stage_dir": result.get("stage_dir"),
        "manifests": result.get("manifests"),
        "apply_count": len(result.get("apply") or []),
    }


def _compact_probe(result: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "ok": result.get("ok"),
        "url": result.get("url"),
        "connect_url": result.get("connect_url"),
        "status": result.get("status"),
        "method": result.get("probe_method") or result.get("method"),
        "error": result.get("error") or result.get("primary_error"),
    }
    for key in (
        "primary_error",
        "loopback_error",
        "probe_recovery",
        "route_diagnostics",
        "workerbee_error",
    ):
        if key in result:
            compact[key] = result[key]
    workerbee_error = result.get("workerbee_error")
    if isinstance(workerbee_error, dict):
        details = workerbee_error.get("details")
        if isinstance(details, dict):
            for key in (
                "primary_error",
                "loopback_error",
                "probe_recovery",
                "route_diagnostics",
            ):
                if key in details and key not in compact:
                    compact[key] = details[key]
    return compact


def _local_containerd_realtime_image(project: str, app: str) -> str:
    return f"localhost/workerbee-{project}-realtime-{app}:dev"


def _rewrite_stage_images_for_local_containerd(stage_dir: Path, *, project: str) -> None:
    manifest_dir = stage_dir / "manifests"
    for app in ("db", "backend", "frontend"):
        path = manifest_dir / f"{app}.k1s.yaml"
        original = f"image: workerbee-{project}-realtime-{app}:dev"
        replacement = f"image: {_local_containerd_realtime_image(project, app)}"
        text = path.read_text(encoding="utf-8")
        if original in text:
            path.write_text(text.replace(original, replacement), encoding="utf-8")


def _pause_for_manual_inspection(dashboard_url: str, cleanup_command: str) -> None:
    prompt = (
        f"WorkerBee remote k1s dashboard: {dashboard_url}\n"
        "Press Enter to clean up the remote k1s test target.\n"
        f"Fallback cleanup command: {cleanup_command}\n"
    )
    if sys.stdin.isatty():
        print(prompt, end="", flush=True)
        input()
        return
    try:
        with Path("/dev/tty").open("r+", encoding="utf-8") as tty:
            tty.write(prompt)
            tty.flush()
            tty.readline()
            return
    except OSError:
        print(prompt, end="", flush=True)
        print("stdin is not interactive; cleaning up without pausing.", flush=True)


def _host_conflict_snapshot() -> dict[str, Any]:
    return {
        "interfaces": _host_interfaces(),
        "nerdctl0_exists": Path("/sys/class/net/nerdctl0").exists(),
        "microk8s_cni": _microk8s_cni_snapshot(),
    }


def _host_conflict_check(before: dict[str, Any]) -> dict[str, Any]:
    after = _host_conflict_snapshot()
    issues = []
    if not before.get("nerdctl0_exists") and after.get("nerdctl0_exists"):
        issues.append("nerdctl0_created")
    before_cni = _cni_digest_map(before.get("microk8s_cni"))
    after_cni = _cni_digest_map(after.get("microk8s_cni"))
    if before_cni and after_cni and before_cni != after_cni:
        issues.append("microk8s_cni_config_changed")
    return {
        "ok": not issues,
        "issues": issues,
        "before": before,
        "after": after,
        "new_interfaces": sorted(set(after["interfaces"]) - set(before.get("interfaces") or [])),
        "preexisting_nerdctl0": bool(before.get("nerdctl0_exists")),
    }


def _host_interfaces() -> list[str]:
    net_root = Path("/sys/class/net")
    if not net_root.is_dir():
        return []
    return sorted(path.name for path in net_root.iterdir())


def _microk8s_cni_snapshot() -> dict[str, Any]:
    root = Path("/var/snap/microk8s")
    if not root.exists():
        return {"available": False, "root": str(root), "files": []}
    files = []
    for cni_dir in sorted(root.glob("*/args/cni-network")):
        if not cni_dir.is_dir():
            continue
        for path in sorted(cni_dir.rglob("*")):
            if path.is_file():
                files.append(_fingerprint_file(path))
    return {"available": True, "root": str(root), "files": [item for item in files if item]}


def _fingerprint_file(path: Path) -> dict[str, Any] | None:
    try:
        data = path.read_bytes()
        stat = path.stat()
    except OSError:
        return None
    return {
        "path": str(path),
        "resolved": str(path.resolve(strict=False)),
        "size": stat.st_size,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _cni_digest_map(snapshot: object) -> dict[str, str]:
    if not isinstance(snapshot, dict):
        return {}
    files = snapshot.get("files")
    if not isinstance(files, list):
        return {}
    out = {}
    for item in files:
        if isinstance(item, dict) and item.get("path") and item.get("sha256"):
            out[str(item["path"])] = str(item["sha256"])
    return out


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
