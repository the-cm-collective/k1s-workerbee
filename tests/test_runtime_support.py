from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workerbee.contract import WorkerBeeError
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
    raise_if_containerd_microk8s_conflict,
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
    bridge_config = (
        tmp_path
        / "projects"
        / "my-app"
        / "containerd-cni-net.d"
        / "nerdctl-bridge.conflist"
    )
    bridge = json.loads(bridge_config.read_text(encoding="utf-8"))
    project_subnet = containerd_network_subnet(tmp_path, "My App")
    bridge_subnet = containerd_network_subnet(tmp_path, "my-app-default-bridge")
    assert bridge["name"] == "bridge"
    assert bridge["plugins"][0]["bridge"].startswith("wb")
    assert bridge["plugins"][0]["bridge"] != "nerdctl0"
    assert len(bridge["plugins"][0]["bridge"]) <= 15
    assert bridge["plugins"][0]["ipam"]["ranges"][0][0]["subnet"] == bridge_subnet
    assert bridge["plugins"][0]["ipam"]["ranges"][0][0]["subnet"] != project_subnet
    assert bridge["plugins"][0]["ipam"]["ranges"][0][0]["gateway"] == (
        bridge_subnet.removesuffix(".0/24") + ".1"
    )
    assert containerd_network_name(tmp_path, "My App") == f"workerbee-{state_hash}-my-app"
    assert containerd_network_subnet(tmp_path, "My App").startswith("10.")
    assert containerd_network_subnet(tmp_path, "My App").endswith(".0/24")


def test_containerd_default_bridge_rewrites_stale_nerdctl0_config(tmp_path: Path) -> None:
    cni_dir = tmp_path / "projects" / "demo" / "containerd-cni-net.d"
    cni_dir.mkdir(parents=True)
    stale = cni_dir / "nerdctl-bridge.conflist"
    stale.write_text(
        json.dumps(
            {
                "cniVersion": "1.0.0",
                "name": "bridge",
                "plugins": [
                    {
                        "type": "bridge",
                        "bridge": "nerdctl0",
                        "ipam": {
                            "ranges": [[{"gateway": "10.4.0.1", "subnet": "10.4.0.0/24"}]],
                            "type": "host-local",
                        },
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    containerd_base_args(state_root=tmp_path, project="demo")

    rewritten = json.loads(stale.read_text(encoding="utf-8"))
    assert rewritten["plugins"][0]["bridge"] != "nerdctl0"
    assert rewritten["plugins"][0]["ipam"]["ranges"][0][0]["subnet"] == (
        containerd_network_subnet(tmp_path, "demo-default-bridge")
    )


def test_containerd_microk8s_socket_denied_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "WORKERBEE_CONTAINERD_ADDRESS",
        "unix:///var/snap/microk8s/common/run/containerd.sock",
    )

    with pytest.raises(WorkerBeeError) as exc:
        containerd_base_args(state_root=tmp_path, project="demo")

    assert exc.value.code == "CONTAINERD_MICROK8S_CONFLICT"


def test_containerd_microk8s_socket_allows_intentional_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "WORKERBEE_CONTAINERD_ADDRESS",
        "unix:///var/snap/microk8s/common/run/containerd.sock",
    )
    monkeypatch.setenv("WORKERBEE_ALLOW_SHARED_K8S_CONTAINERD", "1")

    args = containerd_base_args(state_root=tmp_path, project="demo")

    assert args[args.index("--address") + 1] == (
        "unix:///var/snap/microk8s/common/run/containerd.sock"
    )
    assert args[args.index("--cni-netconfpath") + 1].startswith(str(tmp_path))


def test_containerd_microk8s_cni_path_denied_even_with_override(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKERBEE_ALLOW_SHARED_K8S_CONTAINERD", "1")

    with pytest.raises(WorkerBeeError) as exc:
        raise_if_containerd_microk8s_conflict(
            address="unix:///run/containerd/containerd.sock",
            cni_netconfpath="/var/snap/microk8s/current/args/cni-network",
        )

    assert exc.value.code == "CONTAINERD_MICROK8S_CONFLICT"
    assert "microk8s_cni_netconfpath" in exc.value.details["reasons"]


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
        return SimpleNamespace(
            returncode=0,
            stdout="Step 1/2\nWARNING: cache disabled\nSuccessfully built\n",
        )

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
    assert "--no-cache" in calls[0]
    assert result["build_summary"]["backend"] == "docker"
    assert result["build_summary"]["line_count"] == 3
    assert result["build_summary"]["warning_count"] == 1
    assert result["build_summary"]["error_count"] == 0


def test_docker_build_accepts_explicit_dockerfile_with_repo_root_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = tmp_path / "repo"
    dockerfile = context / "backend" / "Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
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
        dockerfile=Path("backend/Dockerfile"),
        tag="workerbee-demo:test",
    )

    assert result["ok"] is True
    assert result["dockerfile"] == str(dockerfile)
    assert ["-f", str(dockerfile)] == calls[0][4:6]
    assert "--no-cache" in calls[0]
    assert calls[0][-1] == str(context)


def test_containerd_fallback_build_loads_image_from_state_local_tar(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    (context / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr("workerbee.runtime_support.shutil.which", lambda _name: "/bin/tool")

    def fake_run(cmd: list[str], **kwargs):
        calls.append(cmd)
        assert kwargs.get("stdin") is None
        if cmd[0] == "nerdctl" and "build" in cmd:
            return SimpleNamespace(returncode=1, stdout="nerdctl build failed")
        if cmd[:2] == ["podman", "build"]:
            return SimpleNamespace(returncode=0, stdout="podman build ok")
        if cmd[:3] == ["podman", "save", "-o"]:
            assert Path(cmd[3]).is_relative_to(tmp_path / "global" / "image-transfer")
            Path(cmd[3]).write_text("tar", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="podman save ok")
        if cmd[0] == "nerdctl" and cmd[-2] == "-i":
            assert cmd[-3] == "load"
            assert Path(cmd[-1]).is_relative_to(tmp_path / "global" / "image-transfer")
            return SimpleNamespace(returncode=0, stdout="nerdctl load ok")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("workerbee.runtime_support.subprocess.run", fake_run)

    result = build_image_with_runtime(
        runtime="containerd",
        state_root=tmp_path,
        project="demo",
        context=context,
        tag="workerbee-demo:test",
    )

    assert result["ok"] is True
    assert result["build_backend"] == "podman-save-load"
    assert result["build_summary"]["backend"] == "podman-save-load"
    assert result["build_summary"]["line_count"] == 3
    assert result["attempts"][0]["summary"]["error_count"] == 1
    assert "--no-cache" in calls[0]
    assert calls[-1][-3:] == ["load", "-i", calls[-1][-1]]
    assert not list((tmp_path / "global" / "image-transfer").glob("*.tar"))


def test_containerd_build_skips_nerdctl_when_buildctl_missing_and_fallback_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        if name == "buildctl":
            return None
        return f"/bin/{name}"

    def fake_run(cmd: list[str], **kwargs):
        calls.append(cmd)
        assert kwargs.get("stdin") is None
        if cmd[:2] == ["podman", "build"]:
            return SimpleNamespace(returncode=0, stdout="podman build ok")
        if cmd[:3] == ["podman", "save", "-o"]:
            Path(cmd[3]).write_text("tar", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="podman save ok")
        if cmd[0] == "nerdctl" and "load" in cmd:
            return SimpleNamespace(returncode=0, stdout="nerdctl load ok")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("workerbee.runtime_support.shutil.which", fake_which)
    monkeypatch.setattr("workerbee.runtime_support.subprocess.run", fake_run)

    result = build_image_with_runtime(
        runtime="containerd",
        state_root=tmp_path,
        project="demo",
        context=context,
        tag="workerbee-demo:test",
    )

    assert result["ok"] is True
    assert result["attempts"][0]["skipped"] is True
    assert any(cmd[:2] == ["podman", "build"] and "--no-cache" in cmd for cmd in calls)
    assert not any(cmd[0] == "nerdctl" and "build" in cmd for cmd in calls)


def test_docker_build_failure_includes_build_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = tmp_path / "context"
    context.mkdir()

    def fake_run(_cmd: list[str], **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="Step 1/2\nwarning: old base image\nERROR: build failed\n",
        )

    monkeypatch.setattr("workerbee.runtime_support.subprocess.run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        build_image_with_runtime(
            runtime="docker",
            state_root=tmp_path,
            project="demo",
            context=context,
            tag="workerbee-demo:test",
        )

    payload = json.loads(str(exc_info.value))
    assert payload["build_summary"]["line_count"] == 3
    assert payload["build_summary"]["warning_count"] == 1
    assert payload["build_summary"]["error_count"] == 1


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
