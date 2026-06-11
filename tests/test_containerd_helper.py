from __future__ import annotations

import errno
import hashlib
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from workerbee import containerd_helper
from workerbee.containerd_helper import (
    _handle_remove_tree,
    containerd_available_for_auto,
    containerd_privilege_env,
    containerd_privilege_summary,
    effective_containerd_privilege_mode,
    ensure_containerd_helper,
    ensure_containerd_privilege,
    validate_helper_argv,
)
from workerbee.contract import WorkerBeeError


def test_containerd_privilege_policy_only_for_explicit_containerd() -> None:
    assert effective_containerd_privilege_mode("containerd", "auto") == "auto"
    assert effective_containerd_privilege_mode("containerd", "sudo-helper") == "sudo-helper"
    assert effective_containerd_privilege_mode("containerd", "unprivileged") == "unprivileged"
    assert effective_containerd_privilege_mode("auto", "auto") == "off"
    assert effective_containerd_privilege_mode("docker", "sudo-helper") == "off"


def test_ensure_containerd_privilege_uses_unprivileged_when_probe_works(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.containerd_helper.containerd_nerdctl_probe",
        lambda _address=None: {"ok": True, "namespaces": ["default"]},
    )

    result = ensure_containerd_privilege(
        state_root=tmp_path,
        runtime="containerd",
        mode="auto",
    )

    assert result["effective_mode"] == "unprivileged"
    assert result["env"] == {}


def test_ensure_containerd_privilege_starts_helper_when_probe_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.containerd_helper.containerd_nerdctl_probe",
        lambda _address=None: {
            "ok": False,
            "code": "NERDCTL_ROOTLESS_MODE",
            "message": "rootless containerd not running",
        },
    )
    monkeypatch.setattr(
        "workerbee.containerd_helper.ensure_containerd_helper",
        lambda root, **_kwargs: {
            "ok": True,
            "started": True,
            "env": {"WORKERBEE_NERDCTL_BIN": str(root / "global" / "bin" / "workerbee-nerdctl")},
        },
    )

    result = ensure_containerd_privilege(
        state_root=tmp_path,
        runtime="containerd",
        mode="auto",
    )

    assert result["effective_mode"] == "sudo-helper"
    assert result["helper"]["started"] is True
    assert result["env"]["WORKERBEE_NERDCTL_BIN"].endswith("workerbee-nerdctl")


def test_containerd_available_for_auto_accepts_unprivileged_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.containerd_helper.containerd_nerdctl_probe",
        lambda _address=None: {"ok": True, "namespaces": ["default"]},
    )

    result = containerd_available_for_auto(state_root=tmp_path)

    assert result["ok"] is True
    assert result["selected"] == "containerd"
    assert result["source"] == "unprivileged"


def test_containerd_available_for_auto_accepts_responsive_helper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.containerd_helper.containerd_privilege_status",
        lambda **_kwargs: {
            "runtime": "containerd",
            "effective_mode": "auto",
            "helper": {
                "responsive": True,
                "socket": str(tmp_path / "global" / "containerd-helper.sock"),
                "wrapper": str(tmp_path / "global" / "bin" / "workerbee-nerdctl"),
            },
            "unprivileged_probe": {"ok": False, "code": "CONTAINERD_SOCKET_PERMISSION_DENIED"},
        },
    )
    monkeypatch.setattr(
        "workerbee.containerd_helper.containerd_helper_nerdctl_probe",
        lambda _root: {"ok": True, "namespaces": ["workerbee-demo"]},
    )

    result = containerd_available_for_auto(state_root=tmp_path)

    assert result["ok"] is True
    assert result["selected"] == "containerd"
    assert result["source"] == "sudo-helper"


def test_containerd_available_for_auto_does_not_start_helper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.containerd_helper.containerd_nerdctl_probe",
        lambda _address=None: {
            "ok": False,
            "code": "CONTAINERD_SOCKET_PERMISSION_DENIED",
            "message": "permission denied",
        },
    )

    def fail_start(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("auto availability check must not start sudo-helper")

    monkeypatch.setattr("workerbee.containerd_helper.ensure_containerd_helper", fail_start)

    result = containerd_available_for_auto(state_root=tmp_path)

    assert result["ok"] is False
    assert result["selected"] is None
    assert result["error"]["code"] == "CONTAINERD_AUTO_UNAVAILABLE"


def test_containerd_privilege_env_synthesizes_from_helper_status(tmp_path: Path) -> None:
    socket = tmp_path / "global" / "containerd-helper.sock"
    wrapper = tmp_path / "global" / "bin" / "workerbee-nerdctl"

    env = containerd_privilege_env(
        {
            "effective_mode": "sudo-helper",
            "helper": {
                "responsive": True,
                "socket": str(socket),
                "wrapper": str(wrapper),
            },
        },
    )

    assert env["WORKERBEE_NERDCTL_BIN"] == str(wrapper)
    assert env["AE_NERDCTL_BIN"] == str(wrapper)
    assert env["WORKERBEE_CONTAINERD_HELPER_SOCKET"] == str(socket)


def test_helper_socket_path_falls_back_for_long_state_root(tmp_path: Path) -> None:
    long_root = tmp_path / ("nested-" + ("x" * 80)) / ("state-" + ("y" * 80))
    paths = containerd_helper._helper_paths(long_root)  # noqa: SLF001

    assert paths["metadata"] == long_root.resolve() / "global" / "containerd-helper.json"
    assert paths["log"] == long_root.resolve() / "global" / "containerd-helper.log"
    assert paths["socket"].name.endswith("containerd-helper.sock")
    assert len(str(paths["socket"])) <= containerd_helper.HELPER_SOCKET_PATH_LIMIT
    assert paths["socket"].parent != long_root.resolve() / "global"


def test_containerd_privilege_summary_suppresses_expected_probe_details(tmp_path: Path) -> None:
    summary = containerd_privilege_summary(
        {
            "ok": True,
            "enabled": True,
            "requested_mode": "sudo-helper",
            "effective_mode": "sudo-helper",
            "runtime": "containerd",
            "state_root": str(tmp_path),
            "helper": {
                "status": {
                    "running": True,
                    "responsive": True,
                    "pid": 123,
                    "socket": str(tmp_path / "helper.sock"),
                }
            },
            "unprivileged_probe": {
                "ok": False,
                "code": "CONTAINERD_SOCKET_PERMISSION_DENIED",
                "message": "permission denied",
            },
        }
    )

    assert summary is not None
    assert summary["socket_access"] == "sudo-helper"
    assert summary["unprivileged_access"] == "denied_expected"
    assert "unprivileged_probe" not in summary
    assert "CONTAINERD_SOCKET_PERMISSION_DENIED" not in str(summary)


def test_helper_remove_tree_is_limited_to_project_state(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "root-owned-placeholder").write_text("data", encoding="utf-8")
    global_data = tmp_path / "global" / "containerd-data"
    global_data.mkdir(parents=True)
    (global_data / "root-owned-placeholder").write_text("data", encoding="utf-8")

    result = _handle_remove_tree({"path": str(project)}, state_root=tmp_path)

    assert result["ok"] is True
    assert result["removed"] is True
    assert not project.exists()
    global_result = _handle_remove_tree({"path": str(global_data)}, state_root=tmp_path)
    assert global_result["ok"] is True
    assert global_result["removed"] is True
    assert not global_data.exists()
    denied = _handle_remove_tree({"path": str(tmp_path / "global")}, state_root=tmp_path)
    assert denied["ok"] is False
    assert denied["error"]["code"] == "CONTAINERD_HELPER_REMOVE_PATH_DENIED"


def test_helper_response_ignores_broken_pipe() -> None:
    class ClosedConnection:
        def sendall(self, _payload: bytes) -> None:
            raise BrokenPipeError

    containerd_helper._send_helper_response(ClosedConnection(), {"ok": True})  # noqa: SLF001


def test_helper_connect_retries_transient_busy_socket(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    class FakeSocket:
        def settimeout(self, _timeout: float) -> None:
            return

        def connect(self, _path: str) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise BlockingIOError(errno.EAGAIN, "temporarily unavailable")

    monkeypatch.setattr(containerd_helper.time, "sleep", sleeps.append)

    containerd_helper._connect_helper_socket(  # noqa: SLF001
        FakeSocket(),
        tmp_path / "containerd-helper.sock",
        timeout=1.0,
    )

    assert attempts == 2
    assert sleeps


def test_ensure_containerd_helper_starts_single_background_sudo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    status_calls = 0
    launched: dict[str, object] = {}

    def fake_status(root: Path) -> dict[str, object]:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            return {"responsive": False}
        return {
            "responsive": True,
            "socket": str(root / "global" / "containerd-helper.sock"),
            "pid": 1234,
        }

    class FakePopen:
        pid = 1234

        def __init__(self, argv: list[str], **kwargs: object) -> None:
            launched["argv"] = argv
            launched["kwargs"] = kwargs

    def fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("helper startup must not use a separate sudo -v probe")

    monkeypatch.setattr(containerd_helper.shutil, "which", fake_which)
    monkeypatch.setattr(containerd_helper.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(containerd_helper.subprocess, "run", fail_run)
    monkeypatch.setattr(containerd_helper, "containerd_helper_status", fake_status)
    monkeypatch.setattr(containerd_helper, "_wait_for_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        containerd_helper,
        "containerd_helper_nerdctl_probe",
        lambda _root: {"ok": True},
    )

    result = ensure_containerd_helper(tmp_path, timeout=0.01)

    argv = launched["argv"]
    kwargs = launched["kwargs"]
    assert isinstance(argv, list)
    assert argv[:2] == ["/usr/bin/sudo", "-b"]
    assert "-n" not in argv
    assert isinstance(kwargs, dict)
    assert kwargs.get("stdin") is not subprocess.DEVNULL
    assert kwargs.get("start_new_session") is not True
    assert result["started"] is True
    assert result["pid"] == 1234


def test_validate_helper_argv_allows_workerbee_scoped_command(tmp_path: Path) -> None:
    state_hash = _state_hash(tmp_path)
    validate_helper_argv(
        [
            "--address",
            "unix:///run/containerd/containerd.sock",
            "--namespace",
            f"workerbee-{state_hash}-demo",
            "--data-root",
            str(tmp_path / "projects" / "demo" / "containerd-data"),
            "--cni-netconfpath",
            str(tmp_path / "projects" / "demo" / "containerd-cni-net.d"),
            "ps",
            "-aq",
        ],
        state_root=tmp_path,
        address="unix:///run/containerd/containerd.sock",
    )


def test_validate_helper_argv_denies_microk8s_socket_by_default(tmp_path: Path) -> None:
    state_hash = _state_hash(tmp_path)
    address = "unix:///var/snap/microk8s/common/run/containerd.sock"

    with pytest.raises(WorkerBeeError) as exc:
        validate_helper_argv(
            [
                "--address",
                address,
                "--namespace",
                f"workerbee-{state_hash}-demo",
                "--data-root",
                str(tmp_path / "projects" / "demo" / "containerd-data"),
                "--cni-netconfpath",
                str(tmp_path / "projects" / "demo" / "containerd-cni-net.d"),
                "ps",
            ],
            state_root=tmp_path,
            address=address,
        )

    assert exc.value.code == "CONTAINERD_MICROK8S_CONFLICT"


def test_validate_helper_argv_allows_microk8s_socket_with_explicit_override(
    tmp_path: Path,
) -> None:
    state_hash = _state_hash(tmp_path)
    address = "unix:///var/snap/microk8s/common/run/containerd.sock"

    result = validate_helper_argv(
        [
            "--address",
            address,
            "--namespace",
            f"workerbee-{state_hash}-demo",
            "--data-root",
            str(tmp_path / "projects" / "demo" / "containerd-data"),
            "--cni-netconfpath",
            str(tmp_path / "projects" / "demo" / "containerd-cni-net.d"),
            "ps",
        ],
        state_root=tmp_path,
        address=address,
        allow_shared_k8s_containerd=True,
    )

    assert result["ok"] is True


def test_validate_helper_argv_denies_reserved_namespace(tmp_path: Path) -> None:
    with pytest.raises(WorkerBeeError) as exc:
        validate_helper_argv(
            [
                "--address",
                "unix:///run/containerd/containerd.sock",
                "--namespace",
                "k8s.io",
                "--data-root",
                str(tmp_path / "projects" / "demo" / "containerd-data"),
                "--cni-netconfpath",
                str(tmp_path / "projects" / "demo" / "containerd-cni-net.d"),
                "ps",
            ],
            state_root=tmp_path,
            address="unix:///run/containerd/containerd.sock",
        )

    assert exc.value.code == "CONTAINERD_HELPER_NAMESPACE_DENIED"


def test_validate_helper_argv_denies_data_root_outside_state(tmp_path: Path) -> None:
    state_hash = _state_hash(tmp_path)
    with pytest.raises(WorkerBeeError) as exc:
        validate_helper_argv(
            [
                "--address",
                "unix:///run/containerd/containerd.sock",
                "--namespace",
                f"workerbee-{state_hash}-demo",
                "--data-root",
                "/var/lib/nerdctl",
                "--cni-netconfpath",
                str(tmp_path / "projects" / "demo" / "containerd-cni-net.d"),
                "ps",
            ],
            state_root=tmp_path,
            address="unix:///run/containerd/containerd.sock",
        )

    assert exc.value.code == "CONTAINERD_HELPER_DATA_ROOT_DENIED"


def test_validate_helper_argv_allows_namespace_ls_diagnostic(tmp_path: Path) -> None:
    result = validate_helper_argv(
        ["--address", "unix:///run/containerd/containerd.sock", "namespace", "ls", "--quiet"],
        state_root=tmp_path,
        address="unix:///run/containerd/containerd.sock",
    )

    assert result["diagnostic"] is True


def test_validate_helper_argv_allows_version_diagnostic_without_socket_args(
    tmp_path: Path,
) -> None:
    result = validate_helper_argv(
        ["--version"],
        state_root=tmp_path,
        address="unix:///run/containerd/containerd.sock",
    )

    assert result["diagnostic"] is True


def test_helper_stream_runs_interactive_exec_on_pty(tmp_path: Path) -> None:
    fake_nerdctl = tmp_path / "nerdctl"
    fake_nerdctl.write_text(
        "#!/usr/bin/env sh\n"
        "if [ -t 0 ]; then echo tty=true; else echo tty=false; fi\n"
        "IFS= read -r line\n"
        "printf 'got:%s\\n' \"$line\"\n",
        encoding="utf-8",
    )
    fake_nerdctl.chmod(0o755)
    state_hash = _state_hash(tmp_path)
    argv = [
        "--address",
        "unix:///run/containerd/containerd.sock",
        "--namespace",
        f"workerbee-{state_hash}-demo",
        "--data-root",
        str(tmp_path / "projects" / "demo" / "containerd-data"),
        "--cni-netconfpath",
        str(tmp_path / "projects" / "demo" / "containerd-cni-net.d"),
        "exec",
        "--interactive",
        "--tty",
        "cid",
        "sh",
    ]
    client, server = socket.socketpair()
    result: list[dict[str, object]] = []

    def run_server() -> None:
        with server:
            result.append(
                containerd_helper._handle_helper_connection(  # noqa: SLF001
                    server,
                    state_root=tmp_path,
                    nerdctl=str(fake_nerdctl),
                    address="unix:///run/containerd/containerd.sock",
                )
            )

    thread = threading.Thread(target=run_server)
    thread.start()
    with client:
        client.settimeout(2)
        client.sendall(json.dumps({"action": "stream", "argv": argv}).encode("utf-8") + b"\n")
        client.sendall(b"hello\n")
        chunks: list[bytes] = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            chunk = client.recv(1024)
            if not chunk:
                break
            chunks.append(chunk)
            if b"got:hello" in b"".join(chunks):
                break
    thread.join(timeout=2)
    output = b"".join(chunks)

    assert b"tty=true" in output
    assert b"got:hello" in output
    assert result == [{"ok": True, "_stream_complete": True}]


def _state_hash(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:12]  # noqa: S324
