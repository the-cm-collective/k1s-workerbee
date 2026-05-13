from __future__ import annotations

import subprocess
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

from workerbee.k1s_runtime import K1sRuntime
from workerbee.supervisor import StackInfo, WorkerBeeSupervisor


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
