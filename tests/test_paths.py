from pathlib import Path

from workerbee.k1s_runtime import K1sRuntime
from workerbee.paths import daemon_project_state_dir, default_state_dir, default_state_root
from workerbee.runtime_support import containerd_network_name
from workerbee.supervisor import StackInfo, WorkerBeeSupervisor


def test_default_state_dir_is_project_scoped(tmp_path: Path) -> None:
    assert default_state_dir("demo", cwd=tmp_path) == tmp_path / ".workerbee" / "demo"


def test_default_state_root_honors_workerbee_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKERBEE_HOME", str(tmp_path / "home"))
    assert default_state_root() == tmp_path / "home"
    assert daemon_project_state_dir("demo") == tmp_path / "home" / "projects" / "demo"


def test_project_slug_and_state_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    sup = WorkerBeeSupervisor(
        project="My Project!",
        state_dir=tmp_path / "state",
        runtime="docker",
        cwd=tmp_path,
    )
    assert sup.project == "my-project"
    assert sup.state_dir == tmp_path / "state"


def test_k1s_runtime_apply_env_prepends_pythonpath() -> None:
    runtime = K1sRuntime(
        source="sibling",
        python_executable="/venv/bin/python",
        k1s_root=Path("/repo/k1s"),
        pythonpath="/repo/k1s/src",
        ae_origin="/repo/k1s/src/ae/__init__.py",
    )
    assert runtime.apply_env({"PYTHONPATH": "/existing"})["PYTHONPATH"] == (
        "/repo/k1s/src:/existing"
    )


def test_containerd_supervisor_env_is_state_scoped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    state_dir = tmp_path / "projects" / "demo"
    sup = WorkerBeeSupervisor(project="demo", state_dir=state_dir, runtime="containerd")
    info = StackInfo(
        project="demo",
        state_dir=str(state_dir),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin="/site-packages/ae/__init__.py",
        runtime="containerd",
        network=containerd_network_name(tmp_path, "demo"),
        controller_port=19108,
        apishim_port=18445,
        dashboard_url="http://127.0.0.1:19108/dashboard",
        controller_url="http://127.0.0.1:19108",
        apishim_url="https://127.0.0.1:18445",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["shim", "token"]),
    )

    env = sup._base_env(info)  # noqa: SLF001 - verifies runtime isolation contract

    assert env["AE_CONTAINERD_NAMESPACE"].startswith("workerbee-")
    assert env["AE_CONTAINERD_NAMESPACE"].endswith("-demo")
    assert env["AE_CONTAINERD_NETWORK"] == env["AE_NETWORK_NAME"]
    assert env["AE_NERDCTL_BIN"] == "nerdctl"
    assert env["AE_CONTAINERD_CNI_CONF_DIR"] == str(state_dir / "containerd-cni-net.d")
    assert env["NETCONFPATH"] == env["AE_CONTAINERD_CNI_CONF_DIR"]


def test_containerd_supervisor_env_passes_helper_nerdctl_to_k1s(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKERBEE_NERDCTL_BIN", str(tmp_path / "workerbee-nerdctl"))
    monkeypatch.setattr(
        "workerbee.supervisor.resolve_k1s_runtime",
        lambda **_: K1sRuntime(
            source="installed",
            python_executable="/usr/bin/python",
            k1s_root=None,
            pythonpath=None,
            ae_origin="/site-packages/ae/__init__.py",
        ),
    )
    state_dir = tmp_path / "projects" / "demo"
    sup = WorkerBeeSupervisor(project="demo", state_dir=state_dir, runtime="containerd")
    info = StackInfo(
        project="demo",
        state_dir=str(state_dir),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin="/site-packages/ae/__init__.py",
        runtime="containerd",
        network=containerd_network_name(tmp_path, "demo"),
        controller_port=19108,
        apishim_port=18445,
        dashboard_url="http://127.0.0.1:19108/dashboard",
        controller_url="http://127.0.0.1:19108",
        apishim_url="https://127.0.0.1:18445",
        admin_token="-".join(["admin", "token"]),
        read_token="-".join(["read", "token"]),
        apishim_token="-".join(["shim", "token"]),
    )

    env = sup._base_env(info)  # noqa: SLF001 - verifies runtime isolation contract

    assert env["AE_NERDCTL_BIN"] == str(tmp_path / "workerbee-nerdctl")
    assert str(tmp_path / "workerbee-nerdctl") in Path(env["AE_CONTAINER_CLI"]).read_text(
        encoding="utf-8"
    )
