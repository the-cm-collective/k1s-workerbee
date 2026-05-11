from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from workerbee.k1s_runtime import K1sRuntime
from workerbee.supervisor import StackInfo, WorkerBeeSupervisor


def test_run_ae_retries_remote_apply_read_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
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


def _stack(tmp_path: Path) -> StackInfo:
    return StackInfo(
        project="demo",
        state_dir=str(tmp_path / "state"),
        k1s_root=None,
        k1s_runtime_source="installed",
        python_executable="/usr/bin/python",
        ae_origin=None,
        runtime="docker",
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
