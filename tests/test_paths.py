from pathlib import Path

from workerbee.paths import default_state_dir
from workerbee.supervisor import WorkerBeeSupervisor


def test_default_state_dir_is_project_scoped(tmp_path: Path) -> None:
    assert default_state_dir("demo", cwd=tmp_path) == tmp_path / ".workerbee" / "demo"


def test_project_slug_and_state_dir(tmp_path: Path) -> None:
    sup = WorkerBeeSupervisor(
        project="My Project!",
        state_dir=tmp_path / "state",
        k1s_root=tmp_path,
        runtime="docker",
        cwd=tmp_path,
    )
    assert sup.project == "my-project"
    assert sup.state_dir == tmp_path / "state"

