from __future__ import annotations

import ssl
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from workerbee.agent import derive_session_project, derive_session_project_info
from workerbee.contract import WorkerBeeError
from workerbee.daemon import WorkerBeeDaemon
from workerbee.k1s_runtime import K1sRuntime
from workerbee.probe import probe_workerbee_url
from workerbee.supervisor import StackInfo, WorkerBeeSupervisor


def _runtime() -> K1sRuntime:
    return K1sRuntime(
        source="installed",
        python_executable="/usr/bin/python",
        k1s_root=None,
        pythonpath=None,
        ae_origin="/site-packages/ae/__init__.py",
    )


def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workerbee.supervisor.resolve_k1s_runtime", lambda **_: _runtime())


def test_session_project_uses_repo_basename_and_path_hash(tmp_path: Path) -> None:
    one = tmp_path / "repo"
    two = tmp_path / "other" / "repo"
    one.mkdir()
    two.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(one)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(one), "symbolic-ref", "HEAD", "refs/heads/dev"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assert derive_session_project(one).startswith("repo-dev-")
    assert derive_session_project(one) == derive_session_project(one)
    assert derive_session_project(one) != derive_session_project(two)
    assert derive_session_project(one, project="Custom Project") == "custom-project"
    info = derive_session_project_info(one)
    assert info.git_branch == "dev"
    assert info.git_root == one.resolve()
    assert info.explicit_project is False


def test_session_start_persists_cwd_and_lazy_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch)
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    daemon = WorkerBeeDaemon(state_root=tmp_path / "state", cwd=tmp_path)

    result = daemon.session_start(cwd=cwd, goal="exercise integration path")

    assert result["mode"] == "lazy"
    assert result["project"].startswith("checkout-")
    assert result["project_identity"]["explicit_project"] is False
    assert result["project_status"]["running"] is False
    assert result["runbook"]["title"] == "WorkerBee Cloud-Native Loop"
    records = daemon._read_registry()  # noqa: SLF001 - verifies persisted session metadata
    assert records[result["project"]]["cwd_hint"] == str(cwd.resolve())


def test_stop_mode_blocks_active_project_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch)
    daemon = WorkerBeeDaemon(state_root=tmp_path / "state", cwd=tmp_path)
    project = "demo"

    result = daemon.project_mode_set(project=project, mode="stop", cwd=tmp_path)

    assert result["mode"] == "stop"
    with pytest.raises(WorkerBeeError) as exc:
        daemon.with_project(project, lambda _sup: {"ok": True}, require_active=True)
    assert exc.value.code == "PROJECT_STOPPED"


def test_start_mode_starts_and_emits_user_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch)

    def fake_status(self: WorkerBeeSupervisor) -> dict[str, Any]:
        running = bool(getattr(self, "_test_running", False))
        stack = _stack(self).public_dict() if running else None
        return {"running": running, "apishim_running": running, "stack": stack}

    def fake_start(self: WorkerBeeSupervisor) -> StackInfo:
        self._test_running = True
        return _stack(self)

    monkeypatch.setattr(WorkerBeeSupervisor, "status", fake_status)
    monkeypatch.setattr(WorkerBeeSupervisor, "start", fake_start)
    daemon = WorkerBeeDaemon(state_root=tmp_path / "state", cwd=tmp_path)

    result = daemon.project_mode_set(project="demo", mode="start", cwd=tmp_path)

    assert result["mode"] == "start"
    assert result["events"][0]["type"] == "project_stack_started"
    assert "Dashboard:" in result["user_message"]


def test_probe_restricts_to_workerbee_hosts(tmp_path: Path) -> None:
    ca = tmp_path / "root.crt"
    ca.write_text("not a real ca in this validation-only path", encoding="utf-8")
    ingress = {"https_port": 19443, "ca_bundle": str(ca)}

    with pytest.raises(WorkerBeeError) as exc:
        probe_workerbee_url(
            project="demo",
            ingress_info=ingress,
            url="https://example.com:19443/",
        )
    assert exc.value.code == "UNSUPPORTED_PROBE_URL"


def test_probe_reports_status_and_body_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca = tmp_path / "root.crt"
    ca.write_text("fake", encoding="utf-8")
    ingress = {"https_port": 19443, "ca_bundle": str(ca)}

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "text/plain", "Set-Cookie": "hidden=true"}

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"hello from workerbee"

    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    result = probe_workerbee_url(
        project="demo",
        ingress_info=ingress,
        url="https://app.demo.workerbee.localhost:19443/",
        expected_status=200,
        body_contains="workerbee",
    )

    assert result["ok"] is True
    assert result["tls_verified"] is True
    assert result["headers"] == {"Content-Type": "text/plain"}


def _stack(supervisor: WorkerBeeSupervisor) -> StackInfo:
    return StackInfo(
        project=supervisor.project,
        state_dir=str(supervisor.state_dir),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin="/site-packages/ae/__init__.py",
        runtime="docker",
        network=f"workerbee-{supervisor.project}",
        controller_port=19108,
        apishim_port=18445,
        dashboard_url="http://127.0.0.1:19108/dashboard",
        controller_url="http://127.0.0.1:19108",
        apishim_url="https://127.0.0.1:18445",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["shim", "token"]),
    )
