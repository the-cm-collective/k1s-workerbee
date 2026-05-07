from pathlib import Path

from workerbee.k1s_runtime import K1sRuntime
from workerbee.paths import default_state_dir
from workerbee.supervisor import WorkerBeeSupervisor


def test_default_state_dir_is_project_scoped(tmp_path: Path) -> None:
    assert default_state_dir("demo", cwd=tmp_path) == tmp_path / ".workerbee" / "demo"


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
