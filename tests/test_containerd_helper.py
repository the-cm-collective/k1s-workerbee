from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from workerbee import containerd_helper
from workerbee.containerd_helper import (
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


def _state_hash(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:12]  # noqa: S324
