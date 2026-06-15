from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

import pytest

from workerbee.contract import WorkerBeeError
from workerbee.k1s_runtime import K1sRuntime
from workerbee.supervisor import (
    StackInfo,
    WorkerBeeSupervisor,
    _is_controller_for_specs,
    _recorded_stack_host_ports,
    _sudo_kill,
    _terminate_pid,
)


def test_run_ae_retries_remote_apply_read_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    monkeypatch.setattr("workerbee.supervisor.secret_env_for_project", lambda _state: {})
    monkeypatch.setattr("workerbee.supervisor.time.sleep", lambda _seconds: None)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout=(
                    "remote apply failed: HTTPConnectionPool(host='127.0.0.1', port=19108): "
                    "Read timed out. (read timeout=10)\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="applied desired state\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")

    result = sup.run_ae(
        ["--server", "http://127.0.0.1:19108", "--token", "secret", "apply", "-f", "app.yaml"],
        info=_stack(tmp_path),
        timeout=120,
    )

    assert result["returncode"] == 0
    assert result["attempts"] == 2
    assert len(calls) == 2


def test_deploy_manifest_posts_directly_with_workerbee_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    manifest = tmp_path / "web.k1s.yaml"
    manifest.write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: web
  namespace: old
spec:
  image: workerbee-demo-web:dev
""",
        encoding="utf-8",
    )
    sup.start = lambda: _stack(tmp_path)  # type: ignore[method-assign]
    captured: dict[str, Any] = {}
    response_payload = {
        "app": "secure--web",
        "revision": 1,
        "status": "ready",
        "created": 1,
        "updated": 0,
        "removed": 0,
    }

    def fake_request(url: str, **kwargs: Any) -> SimpleNamespace:
        captured["url"] = url
        captured.update(kwargs)
        return SimpleNamespace(
            status=200,
            text='{"ok": true}',
            json=lambda: response_payload,
        )

    monkeypatch.setattr("workerbee.supervisor.request", fake_request)

    result = sup.deploy_manifest(manifest, namespace="secure", timeout=120)

    assert captured["url"] == "http://127.0.0.1:19108/apply"
    assert captured["method"] == "POST"
    assert captured["token"] == "-".join(["admin", "token"])
    assert captured["timeout"] == 120.0
    assert captured["json_body"]["metadata"]["namespace"] == "secure"
    assert result["apply"]["transport"] == "direct-controller"
    assert result["apply"]["cmd"][-2:] == ["-n", "secure"]
    assert result["apply"]["cmd"][result["apply"]["cmd"].index("--token") + 1] == "***"


def test_restart_workload_posts_directly_and_masks_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    sup.start = lambda: _stack(tmp_path)  # type: ignore[method-assign]
    captured: dict[str, Any] = {}
    response_payload = {
        "app": "secure--web",
        "revision": 2,
        "status": "ready",
        "ready": 1,
        "desired": 1,
        "created": 1,
        "updated": 0,
        "removed": 1,
        "restartAt": "2026-06-02T00:00:00+00:00",
    }

    def fake_request(url: str, **kwargs: Any) -> SimpleNamespace:
        captured["url"] = url
        captured.update(kwargs)
        return SimpleNamespace(
            status=200,
            text='{"ok": true}',
            json=lambda: response_payload,
        )

    monkeypatch.setattr("workerbee.supervisor.request", fake_request)

    result = sup.restart_workload("secure/web", timeout=120)

    assert captured["url"] == "http://127.0.0.1:19108/rollout/restart/secure--web"
    assert captured["method"] == "POST"
    assert captured["token"] == "-".join(["admin", "token"])
    assert captured["timeout"] == 120.0
    assert result["namespace"] == "secure"
    assert result["app"] == "web"
    assert result["app_key"] == "secure--web"
    assert result["restart"]["transport"] == "direct-controller"
    assert result["restart"]["cmd"][result["restart"]["cmd"].index("--token") + 1] == "***"
    assert "restartAt=2026-06-02T00:00:00+00:00" in result["restart"]["stdout"]


def test_resolve_runtime_prefers_existing_stack_runtime_when_auto(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="auto")
    captured: dict[str, str] = {}
    sup._write_stack(_stack(tmp_path, runtime="containerd"))  # noqa: SLF001

    def fake_resolve_runtime(requested: str) -> str:
        captured["requested"] = requested
        return requested

    monkeypatch.setattr("workerbee.supervisor.resolve_runtime", fake_resolve_runtime)
    assert sup._resolve_runtime() == "containerd"  # noqa: SLF001
    assert captured["requested"] == "containerd"


def test_start_restarts_healthy_stack_when_requested_runtime_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    sup._write_stack(_stack(tmp_path, runtime="podman"))  # noqa: SLF001
    stopped: list[bool] = []
    cleaned: list[str] = []

    monkeypatch.setattr("workerbee.supervisor.resolve_runtime", lambda requested: requested)
    monkeypatch.setattr(sup, "_controller_healthy", lambda _info: True)
    monkeypatch.setattr(sup, "_apishim_healthy", lambda _info: True)
    monkeypatch.setattr(sup, "_stack_requires_ingress_restart", lambda _info: False)
    monkeypatch.setattr(
        sup,
        "stop",
        lambda *, purge=False: stopped.append(purge) or {"ok": True},
    )

    def fake_cleanup_project_runtime_containers(
        runtime: str,
        *,
        include_namespaces: bool,
    ) -> None:
        del include_namespaces
        cleaned.append(runtime)

    monkeypatch.setattr(
        sup,
        "_cleanup_project_runtime_containers",
        fake_cleanup_project_runtime_containers,
    )
    monkeypatch.setattr(sup, "_ensure_network", lambda _runtime, _network: None)
    monkeypatch.setattr(sup, "_allocate_poc_service_ports", lambda _runtime: {})
    monkeypatch.setattr(sup, "_write_stack_ingress_sites", lambda **_kwargs: {})
    monkeypatch.setattr(sup, "_start_apishim", lambda _info: 111)
    monkeypatch.setattr(sup, "_start_controller", lambda _info: 222)
    monkeypatch.setattr(sup, "_verify_controller_ownership", lambda _info: None)
    monkeypatch.setattr(sup, "_reap_orphan_controllers", lambda **_kwargs: [])
    monkeypatch.setattr("workerbee.supervisor.wait_for_http", lambda *_args, **_kwargs: None)

    info = sup.start()

    assert stopped == [False]
    assert cleaned == ["docker"]
    assert info.runtime == "docker"


def test_cleanup_runtime_removes_project_and_deploy_namespace_containers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="podman")
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(cmd)
        if cmd[:3] == ["podman", "ps", "-aq"]:
            label = cmd[-1]
            stdout = {
                "label=workerbee.project=demo": "cid-project\ncid-dupe\n",
                "label=workerbee.k1s.dev/project=demo": "cid-manifest\ncid-dupe\n",
                "label=ae.namespace=workerbee-poc": "cid-poc\n",
                "label=ae.namespace=demo": "cid-namespace\n",
            }.get(label, "")
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    sup._cleanup_runtime(_stack(tmp_path, runtime="podman"), purge=True)  # noqa: SLF001

    rm_cmd = next(cmd for cmd in commands if cmd[:3] == ["podman", "rm", "-f"])
    assert rm_cmd == [
        "podman",
        "rm",
        "-f",
        "cid-project",
        "cid-dupe",
        "cid-manifest",
        "cid-poc",
        "cid-namespace",
    ]
    assert ["podman", "network", "rm", "workerbee-demo"] in commands


def test_logs_falls_back_to_exited_runtime_container(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    calls: list[list[str]] = []

    def fake_run_ae(
        _self,
        args: list[str],
        *,
        info: StackInfo,
        timeout: int = 30,
    ) -> dict[str, Any]:
        _ = (args, info, timeout)
        raise RuntimeError("No pods available")

    def fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        if cmd[:3] == ["docker", "ps", "-q"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["docker", "ps", "-aq"]:
            return SimpleNamespace(returncode=0, stdout="cid-exited\n", stderr="")
        if cmd[:2] == ["docker", "logs"]:
            return SimpleNamespace(returncode=0, stdout="failed job logs\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    sup.start = lambda: _stack(tmp_path)  # type: ignore[method-assign]
    sup.run_ae = MethodType(fake_run_ae, sup)  # type: ignore[method-assign]
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = sup.logs(app="job", namespace="demo")

    assert result["source"] == "docker"
    assert result["container_state"] == "exited"
    assert result["stdout"] == "failed job logs\n"
    assert any(cmd[:3] == ["docker", "ps", "-aq"] for cmd in calls)


def test_logs_uses_apishim_before_runtime_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="containerd")
    requested: list[str] = []

    def fake_run_ae(
        _self,
        args: list[str],
        *,
        info: StackInfo,
        timeout: int = 30,
    ) -> dict[str, Any]:
        _ = (args, info, timeout)
        raise RuntimeError("controller logs unavailable")

    def fake_request(url: str, **kwargs: Any) -> SimpleNamespace:
        requested.append(url)
        assert kwargs["token"] == "-".join(["apishim", "token"])
        if url.endswith("/api/v1/namespaces/demo/pods"):
            return SimpleNamespace(
                status=200,
                text='{"items":[]}',
                json=lambda: {
                    "items": [
                        {
                            "metadata": {
                                "name": "api-rev1-0",
                                "labels": {"app": "api"},
                            }
                        }
                    ]
                },
            )
        if url.endswith("/api/v1/namespaces/demo/pods/api-rev1-0/log?tailLines=12"):
            return SimpleNamespace(status=200, text="api shim logs\n", json=lambda: None)
        raise AssertionError(f"unexpected URL {url}")

    def fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        raise AssertionError(f"runtime logs should not run: {cmd}")

    sup.start = lambda: _stack(tmp_path, runtime="containerd")  # type: ignore[method-assign]
    sup.run_ae = MethodType(fake_run_ae, sup)  # type: ignore[method-assign]
    monkeypatch.setattr("workerbee.supervisor.request", fake_request)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = sup.logs(app="api", namespace="demo", tail=12)

    assert result["source"] == "apishim"
    assert result["resolved_namespace"] == "demo"
    assert result["resolved_app"] == "api"
    assert result["pods"] == ["api-rev1-0"]
    assert result["stdout"] == "api shim logs\n"
    assert result["k1s_error"] == "controller logs unavailable"
    assert len(requested) == 2


def test_logs_falls_back_to_runtime_when_apishim_has_no_pod(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    calls: list[list[str]] = []

    def fake_run_ae(
        _self,
        args: list[str],
        *,
        info: StackInfo,
        timeout: int = 30,
    ) -> dict[str, Any]:
        _ = (args, info, timeout)
        raise RuntimeError("controller logs unavailable")

    def fake_request(_url: str, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            status=200,
            text='{"items":[]}',
            json=lambda: {"items": []},
        )

    def fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        if cmd[:3] == ["docker", "ps", "-q"]:
            return SimpleNamespace(returncode=0, stdout="cid-api\n", stderr="")
        if cmd[:2] == ["docker", "logs"]:
            return SimpleNamespace(returncode=0, stdout="runtime logs\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    sup.start = lambda: _stack(tmp_path)  # type: ignore[method-assign]
    sup.run_ae = MethodType(fake_run_ae, sup)  # type: ignore[method-assign]
    monkeypatch.setattr("workerbee.supervisor.request", fake_request)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = sup.logs(app="api", namespace="demo")

    assert result["source"] == "docker"
    assert result["stdout"] == "runtime logs\n"
    assert result["k1s_error"] == "controller logs unavailable"
    assert result["apishim_error"] == "no API shim pod found for app demo/api"
    assert any(cmd[:3] == ["docker", "ps", "-q"] for cmd in calls)


def test_recorded_stack_host_ports_blocks_sibling_stack_ports(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    sibling = projects / "sibling-stack"
    other = projects / "other-stack"
    current = projects / "current-stack"
    broken = projects / "broken-stack"
    for project_dir in (sibling, other, current, broken):
        project_dir.mkdir(parents=True)
    sibling.joinpath("stack.json").write_text(
        '{"controller_port": 19108, "apishim_port": 18445}',
        encoding="utf-8",
    )
    other.joinpath("stack.json").write_text(
        '{"controller_port": "19109", "apishim_port": 18446}',
        encoding="utf-8",
    )
    current.joinpath("stack.json").write_text(
        '{"controller_port": 19110, "apishim_port": 18447}',
        encoding="utf-8",
    )
    broken.joinpath("stack.json").write_text("{not json", encoding="utf-8")

    ports = _recorded_stack_host_ports(tmp_path, exclude_project="current-stack")

    assert ports == {19108, 18445, 19109, 18446}


def _patch_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin=None,
        ),
    )


def _stack(tmp_path: Path, *, runtime: str = "docker") -> StackInfo:
    return StackInfo(
        project="demo",
        state_dir=str(tmp_path / "state"),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin=None,
        runtime=runtime,
        network="workerbee-demo",
        controller_port=19108,
        apishim_port=18445,
        dashboard_url="http://127.0.0.1:19108/dashboard",
        controller_url="http://127.0.0.1:19108",
        apishim_url="http://127.0.0.1:18445",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["apishim", "token"]),
    )


def test_verify_controller_ownership_returns_when_healthy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    info = _stack(tmp_path)
    info.controller_pid = 4321
    monkeypatch.setattr("workerbee.supervisor._pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "workerbee.supervisor.request",
        lambda *_args, **_kwargs: SimpleNamespace(status=200),
    )

    # Should not raise.
    sup._verify_controller_ownership(info, timeout_seconds=1)  # noqa: SLF001


def test_verify_controller_ownership_fails_fast_when_port_held(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    info = _stack(tmp_path)
    info.controller_pid = 4321
    # Our controller process is gone but a foreign listener still answers /health.
    monkeypatch.setattr("workerbee.supervisor._pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        "workerbee.supervisor.request",
        lambda *_args, **_kwargs: SimpleNamespace(status=200),
    )

    with pytest.raises(WorkerBeeError) as excinfo:
        sup._verify_controller_ownership(info, timeout_seconds=1)  # noqa: SLF001
    assert excinfo.value.code == "CONTROLLER_PORT_HELD"
    assert "port held by another process" in excinfo.value.message


def test_verify_controller_ownership_fails_when_controller_exits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    info = _stack(tmp_path)
    info.controller_pid = 4321
    monkeypatch.setattr("workerbee.supervisor._pid_alive", lambda _pid: False)

    def _refused(*_args: Any, **_kwargs: Any):
        raise OSError("connection refused")

    monkeypatch.setattr("workerbee.supervisor.request", _refused)

    with pytest.raises(WorkerBeeError) as excinfo:
        sup._verify_controller_ownership(info, timeout_seconds=1)  # noqa: SLF001
    assert excinfo.value.code == "CONTROLLER_START_FAILED"


def test_is_controller_for_specs_matches_only_this_project(tmp_path: Path) -> None:
    specs = (tmp_path / "state" / "specs").resolve()
    controller = [
        "/usr/bin/python",
        "-m",
        "ae.controller",
        "--loop",
        "--specs",
        str(specs),
        "--metrics-port",
        "19108",
        "--watch",
    ]
    assert _is_controller_for_specs(controller, specs) is True
    # Different project's specs directory must not match.
    assert _is_controller_for_specs(controller, (tmp_path / "other" / "specs").resolve()) is False
    # An apishim (no --specs) must not match.
    assert _is_controller_for_specs(["python", "-m", "ae.apishim", "serve"], specs) is False


def test_reap_orphan_controllers_terminates_matching_pids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    specs = (sup.state_dir / "specs").resolve()
    orphan = [
        "/usr/bin/python",
        "-m",
        "ae.controller",
        "--loop",
        "--specs",
        str(specs),
        "--metrics-port",
        "19108",
    ]
    foreign = ["/usr/bin/python", "-m", "ae.controller", "--specs", "/somewhere/else/specs"]
    monkeypatch.setattr(
        "workerbee.supervisor._iter_process_cmdlines",
        lambda: [(901, orphan), (902, foreign), (903, ["python", "-m", "ae.apishim", "serve"])],
    )
    terminated: list[int] = []
    monkeypatch.setattr("workerbee.supervisor._terminate_pid", lambda pid: terminated.append(pid))

    reaped = sup._reap_orphan_controllers()  # noqa: SLF001
    assert reaped == [901]
    assert terminated == [901]


def test_reap_orphan_controllers_skips_tracked_pid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    specs = (sup.state_dir / "specs").resolve()
    tracked = ["python", "-m", "ae.controller", "--specs", str(specs)]
    monkeypatch.setattr(
        "workerbee.supervisor._iter_process_cmdlines",
        lambda: [(555, tracked)],
    )
    terminated: list[int] = []
    monkeypatch.setattr("workerbee.supervisor._terminate_pid", lambda pid: terminated.append(pid))

    reaped = sup._reap_orphan_controllers(exclude_pids={555})  # noqa: SLF001
    assert reaped == []
    assert terminated == []


def test_terminate_pid_escalates_to_sudo_on_eperm(monkeypatch) -> None:
    monkeypatch.setattr("workerbee.supervisor._signal_pid", lambda _pid, _sig: "eperm")
    monkeypatch.setattr("workerbee.supervisor._pid_alive", lambda _pid: False)
    escalated: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "workerbee.supervisor._sudo_kill",
        lambda pid, sig: escalated.append((pid, sig)),
    )

    _terminate_pid(7777)
    assert (7777, signal.SIGTERM) in escalated


def test_sudo_kill_targets_group_and_pid(monkeypatch) -> None:
    monkeypatch.setattr("workerbee.supervisor.shutil.which", lambda _name: "/usr/bin/sudo")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "workerbee.supervisor.subprocess.run",
        lambda cmd, **_kwargs: commands.append(cmd),
    )

    _sudo_kill(1234, signal.SIGTERM)
    assert ["/usr/bin/sudo", "-n", "kill", "-15", "-1234"] in commands
    assert ["/usr/bin/sudo", "-n", "kill", "-15", "1234"] in commands


def test_sudo_kill_noop_without_sudo(monkeypatch) -> None:
    monkeypatch.setattr("workerbee.supervisor.shutil.which", lambda _name: None)
    called: list[Any] = []
    monkeypatch.setattr(
        "workerbee.supervisor.subprocess.run",
        lambda *_args, **_kwargs: called.append(True),
    )

    _sudo_kill(1234, signal.SIGTERM)
    assert called == []
