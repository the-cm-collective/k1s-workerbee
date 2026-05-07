from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from workerbee.runtime_support import (
    build_image_with_runtime,
    cleanup_runtime,
    containerd_base_args,
    containerd_cni_bin_dir,
    containerd_cni_conf_dir,
    containerd_data_root,
    containerd_namespace,
    containerd_nerdctl_probe,
    containerd_network_name,
    containerd_network_subnet,
    containerd_safety_info,
    runtime_command_args,
    write_containerd_cli_wrapper,
)


def test_containerd_runtime_scopes_project_namespace_and_data_root(tmp_path: Path) -> None:
    args = containerd_base_args(state_root=tmp_path, project="My App")
    state_hash = _state_hash(tmp_path)

    assert "--namespace" in args
    assert args[args.index("--namespace") + 1] == f"workerbee-{state_hash}-my-app"
    assert args[args.index("--data-root") + 1] == str(
        tmp_path / "projects" / "my-app" / "containerd-data"
    )
    assert args[args.index("--cni-netconfpath") + 1] == str(
        tmp_path / "projects" / "my-app" / "containerd-cni-net.d"
    )
    assert containerd_namespace(tmp_path, system=True) == f"workerbee-{state_hash}-system"
    assert containerd_data_root(tmp_path, system=True) == tmp_path / "global" / "containerd-data"
    assert containerd_cni_conf_dir(tmp_path, system=True) == (
        tmp_path / "global" / "containerd-cni-net.d"
    )
    assert containerd_network_name(tmp_path, "My App") == f"workerbee-{state_hash}-my-app"
    assert containerd_network_subnet(tmp_path, "My App").startswith("10.")
    assert containerd_network_subnet(tmp_path, "My App").endswith(".0/24")


def test_runtime_command_args_uses_nerdctl_only_for_containerd(tmp_path: Path) -> None:
    assert runtime_command_args(
        "docker",
        state_root=tmp_path,
        project="demo",
        args=["ps"],
    ) == ["docker", "ps"]

    args = runtime_command_args(
        "containerd",
        state_root=tmp_path,
        project="demo",
        args=["ps"],
    )

    assert args[-1] == "ps"
    assert args[0] == "nerdctl"
    assert args[args.index("--namespace") + 1] == f"workerbee-{_state_hash(tmp_path)}-demo"


def test_docker_build_uses_containerfile_when_no_dockerfile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    containerfile = context / "Containerfile"
    containerfile.write_text("FROM scratch\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ok")

    monkeypatch.setattr("workerbee.runtime_support.subprocess.run", fake_run)

    result = build_image_with_runtime(
        runtime="docker",
        state_root=tmp_path,
        project="demo",
        context=context,
        tag="workerbee-demo:test",
    )

    assert result["ok"] is True
    assert calls[0][0:4] == ["docker", "build", "-t", "workerbee-demo:test"]
    assert calls[0][4:6] == ["-f", str(containerfile)]


def test_containerd_cli_wrapper_routes_caddy_exec_to_system_namespace(tmp_path: Path) -> None:
    wrapper = write_containerd_cli_wrapper(
        tmp_path / "bin" / "nerdctl-workerbee",
        state_root=tmp_path,
        project="demo",
        system_container="workerbee-caddy-abc",
    )

    text = wrapper.read_text(encoding="utf-8")
    state_hash = _state_hash(tmp_path)
    assert "workerbee-caddy-abc" in text
    assert f"workerbee-{state_hash}-system" in text
    assert f"workerbee-{state_hash}-demo" in text
    assert wrapper.stat().st_mode & 0o111


def test_containerd_safety_reports_non_workerbee_namespaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "workerbee.runtime_support._list_containerd_namespaces",
        lambda: (["ae", "k8s.io", f"workerbee-{_state_hash(tmp_path)}-demo"], None),
    )

    safety = containerd_safety_info(tmp_path, project="demo")

    assert safety["project_namespace"] == f"workerbee-{_state_hash(tmp_path)}-demo"
    assert safety["non_workerbee_namespaces"] == ["ae", "k8s.io"]
    assert safety["reserved_overlap"] == []
    assert safety["ok"] is True


def test_containerd_probe_uses_workerbee_helper_even_when_socket_inaccessible(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKERBEE_CONTAINERD_HELPER_SOCKET", str(tmp_path / "helper.sock"))
    monkeypatch.setattr(
        "workerbee.runtime_support.containerd_socket_access_info",
        lambda _address=None: {
            "accessible": False,
            "error_code": "CONTAINERD_SOCKET_PERMISSION_DENIED",
        },
    )
    monkeypatch.setattr("workerbee.runtime_support.shutil.which", lambda _name: "/bin/helper")

    def fake_run(cmd: list[str], **_kwargs):
        assert cmd[0] == "/bin/helper"
        return SimpleNamespace(returncode=0, stdout="k8s.io\nmoby\n")

    monkeypatch.setattr("workerbee.runtime_support.subprocess.run", fake_run)

    result = containerd_nerdctl_probe()

    assert result["ok"] is True
    assert result["namespaces"] == ["k8s.io", "moby"]


def test_containerd_cni_bin_dir_detects_complete_path(tmp_path: Path, monkeypatch) -> None:
    for name in ("WORKERBEE_CONTAINERD_CNI_BIN_DIR", "AE_CONTAINERD_CNI_BIN_DIR", "CNI_PATH"):
        monkeypatch.delenv(name, raising=False)
    plugins = tmp_path / "cni"
    plugins.mkdir()

    def fake_which(name: str) -> str:
        return str(plugins / name)

    def fake_complete(path: Path) -> bool:
        return path == plugins

    monkeypatch.setattr("workerbee.runtime_support.shutil.which", fake_which)
    monkeypatch.setattr("workerbee.runtime_support._cni_dir_complete", fake_complete)

    assert containerd_cni_bin_dir() == str(plugins)


def test_containerd_cleanup_does_not_target_reserved_namespaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        '{"projects": {"demo": {"state_dir": "ignored"}}}',
        encoding="utf-8",
    )
    calls: list[tuple[str, list[str]]] = []

    def fake_run(self, args: list[str], **_kwargs):
        calls.append((self.base_args[self.base_args.index("--namespace") + 1], args))

        class Result:
            stdout = ""
            returncode = 0

        return Result()

    monkeypatch.setattr("workerbee.runtime_support.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr("workerbee.runtime_support.RuntimeCommand.run", fake_run)
    monkeypatch.setattr("workerbee.runtime_support._list_containerd_namespaces", lambda: ([], None))

    result = cleanup_runtime(state_root=tmp_path, runtime="containerd")

    namespaces = {namespace for namespace, _args in calls}
    assert result["ok"] is True
    assert namespaces == {
        f"workerbee-{_state_hash(tmp_path)}-system",
        f"workerbee-{_state_hash(tmp_path)}-demo",
    }
    assert not namespaces.intersection({"ae", "k8s.io", "moby", "default"})


def _state_hash(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:12]  # noqa: S324
