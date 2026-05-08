from __future__ import annotations

import json
import ssl
import subprocess
import urllib.request
from pathlib import Path
from types import MethodType, SimpleNamespace
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


def test_probe_posts_json_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca = tmp_path / "root.crt"
    ca.write_text("fake", encoding="utf-8")
    ingress = {"https_port": 19443, "ca_bundle": str(ca)}
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 201
        headers = {"Content-Type": "application/json"}

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request: urllib.request.Request, **_kwargs: object) -> FakeResponse:
        captured["method"] = request.get_method()
        captured["data"] = request.data
        captured["content_type"] = request.headers.get("Content-type")
        return FakeResponse()

    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = probe_workerbee_url(
        project="demo",
        ingress_info=ingress,
        url="https://app.demo.workerbee.localhost:19443/session",
        method="POST",
        json_body={"name": "demo"},
        expected_status=201,
        body_contains='"ok"',
    )

    assert result["ok"] is True
    assert captured["method"] == "POST"
    assert json.loads(captured["data"].decode("utf-8")) == {"name": "demo"}
    assert captured["content_type"] == "application/json"


def test_probe_allows_custom_signed_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca = tmp_path / "root.crt"
    ca.write_text("fake", encoding="utf-8")
    ingress = {"https_port": 19443, "ca_bundle": str(ca)}
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"uploaded"

    def fake_urlopen(request: urllib.request.Request, **_kwargs: object) -> FakeResponse:
        captured["method"] = request.get_method()
        captured["content_type"] = request.headers.get("Content-type")
        captured["data"] = request.data
        return FakeResponse()

    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = probe_workerbee_url(
        project="demo",
        ingress_info=ingress,
        url="https://s3.demo.workerbee.localhost:19443/object",
        method="PUT",
        body="payload",
        headers={"Content-Type": "video/webm"},
        expected_status=200,
    )

    assert result["ok"] is True
    assert captured["method"] == "PUT"
    assert captured["content_type"] == "video/webm"
    assert captured["data"] == b"payload"


def test_probe_rejects_host_header_override(tmp_path: Path) -> None:
    ca = tmp_path / "root.crt"
    ca.write_text("fake", encoding="utf-8")
    ingress = {"https_port": 19443, "ca_bundle": str(ca)}

    with pytest.raises(WorkerBeeError) as exc:
        probe_workerbee_url(
            project="demo",
            ingress_info=ingress,
            url="https://app.demo.workerbee.localhost:19443/",
            headers={"Host": "example.com"},
        )
    assert exc.value.code == "VALIDATION_FAILED"


def test_probe_rejects_invalid_body_options(tmp_path: Path) -> None:
    ca = tmp_path / "root.crt"
    ca.write_text("fake", encoding="utf-8")
    ingress = {"https_port": 19443, "ca_bundle": str(ca)}

    with pytest.raises(WorkerBeeError) as exc:
        probe_workerbee_url(
            project="demo",
            ingress_info=ingress,
            url="https://app.demo.workerbee.localhost:19443/",
            method="GET",
            json_body={"invalid": True},
        )
    assert exc.value.code == "VALIDATION_FAILED"

    with pytest.raises(WorkerBeeError) as exc:
        probe_workerbee_url(
            project="demo",
            ingress_info=ingress,
            url="https://app.demo.workerbee.localhost:19443/",
            method="POST",
            json_body={"invalid": True},
            body="invalid",
        )
    assert exc.value.code == "VALIDATION_FAILED"


def test_workerbee_logs_resolve_project_namespace_and_runtime_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(
        project="rawform-poc-dev-ad9ec5062e",
        state_dir=tmp_path / "state",
        runtime="docker",
    )
    calls: list[list[str]] = []

    def fake_run_ae(
        _self: WorkerBeeSupervisor,
        args: list[str],
        **_kwargs: object,
    ) -> dict[str, Any]:
        assert "rawform-poc-dev-ad9ec5062e/api" in args
        raise RuntimeError("No status recorded")

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        if cmd[:3] == ["docker", "ps", "-q"]:
            return SimpleNamespace(returncode=0, stdout="cid-api\n", stderr="")
        if cmd[:2] == ["docker", "logs"]:
            return SimpleNamespace(returncode=0, stdout="api logs\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    sup.start = lambda: _stack(sup)  # type: ignore[method-assign]
    sup.run_ae = MethodType(fake_run_ae, sup)  # type: ignore[method-assign]
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = sup.logs(app="api")

    assert result["source"] == "docker"
    assert result["resolved_namespace"] == "rawform-poc-dev-ad9ec5062e"
    assert result["resolved_app"] == "api"
    assert result["stdout"] == "api logs\n"
    assert "label=ae.namespace=rawform-poc-dev-ad9ec5062e" in calls[0]
    assert "label=app=api" in calls[0]


def test_workerbee_exec_accepts_namespace_app_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        if cmd[:3] == ["docker", "ps", "-q"]:
            return SimpleNamespace(returncode=0, stdout="cid-api\n", stderr="")
        if cmd[:3] == ["docker", "exec", "cid-api"]:
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    sup.start = lambda: _stack(sup)  # type: ignore[method-assign]
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = sup.run_exec("rawform/api", ["printenv"])

    assert result["resolved_namespace"] == "rawform"
    assert result["resolved_app"] == "api"
    assert result["stdout"] == "ok\n"
    assert "label=ae.namespace=rawform" in calls[0]
    assert calls[1] == ["docker", "exec", "cid-api", "printenv"]


def test_run_ae_cli_sets_http_timeout_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch)
    sup = WorkerBeeSupervisor(project="demo", state_dir=tmp_path / "state", runtime="docker")
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    sup.run_ae_cli(["status"], timeout=240, env_overrides={"EXTRA": "1"})

    env = captured["env"]
    assert env["AE_CLI_HTTP_TIMEOUT"] == "240"
    assert env["EXTRA"] == "1"

    sup.run_ae(["status"], info=_stack(sup), timeout=7)

    env = captured["env"]
    assert env["AE_CLI_HTTP_TIMEOUT"] == "10"


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
