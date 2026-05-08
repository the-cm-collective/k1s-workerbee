from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_microk8s_guard_rollback_retries_transient_patch_failure(tmp_path: Path) -> None:
    script = Path("scripts/dev/microk8s-nvidia-guard").resolve()
    state = tmp_path / "guard.env"
    log = tmp_path / "microk8s.log"
    fail_once = tmp_path / "fail-once"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fail_once.write_text("1", encoding="utf-8")
    state.write_text(
        "\n".join(
            [
                "GUARD_APPLIED=1",
                "KUSTOMIZATION_RECORDS=flux-system/apps:false",
                "HELMRELEASE_PRESENT=0",
                "TOOLKIT_WAS_ENABLED=false",
            ]
        ),
        encoding="utf-8",
    )
    microk8s = fake_bin / "microk8s"
    microk8s.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {log}
if [[ "${{1:-}}" != "kubectl" ]]; then
  exit 1
fi
shift
if [[ "$*" == *"get nodes"* ]]; then
  exit 0
fi
if [[ "$*" == *"patch kustomization apps"* && -f {fail_once} ]]; then
  rm -f {fail_once}
  exit 1
fi
exit 0
""",
        encoding="utf-8",
    )
    microk8s.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "WORKERBEE_MK8S_GUARD_STATE": str(state),
        "WORKERBEE_MK8S_GUARD_ROLLBACK_ATTEMPTS": "2",
        "WORKERBEE_MK8S_GUARD_ROLLBACK_SLEEP": "0",
    }

    proc = subprocess.run(
        ["bash", str(script), "rollback"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    assert "rollback: kubectl failed on attempt 1/2" in proc.stdout
    patch_calls = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if "patch kustomization apps" in line
    ]
    assert len(patch_calls) == 2


def test_microk8s_guard_rollback_waits_for_initial_api_readiness(tmp_path: Path) -> None:
    script = Path("scripts/dev/microk8s-nvidia-guard").resolve()
    state = tmp_path / "guard.env"
    log = tmp_path / "microk8s.log"
    api_fails_once = tmp_path / "api-fails-once"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    api_fails_once.write_text("1", encoding="utf-8")
    state.write_text(
        "\n".join(
            [
                "GUARD_APPLIED=1",
                "KUSTOMIZATION_RECORDS=",
                "HELMRELEASE_PRESENT=0",
                "TOOLKIT_WAS_ENABLED=true",
            ]
        ),
        encoding="utf-8",
    )
    microk8s = fake_bin / "microk8s"
    microk8s.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {log}
if [[ "${{1:-}}" != "kubectl" ]]; then
  exit 1
fi
shift
if [[ "$*" == *"get nodes"* && -f {api_fails_once} ]]; then
  rm -f {api_fails_once}
  exit 1
fi
if [[ "$*" == *"get nodes"* ]]; then
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    microk8s.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "WORKERBEE_MK8S_GUARD_STATE": str(state),
        "WORKERBEE_MK8S_GUARD_ROLLBACK_ATTEMPTS": "3",
        "WORKERBEE_MK8S_GUARD_ROLLBACK_SLEEP": "0",
        "WORKERBEE_MK8S_GUARD_API_WAIT_SECONDS": "2",
    }

    proc = subprocess.run(
        ["bash", str(script), "rollback"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    assert "rollback: waiting up to 2s for MicroK8s API" in proc.stdout
    assert "rollback: MicroK8s API is not ready; retrying" in proc.stdout
    assert "rollback: re-enabling NVIDIA toolkit" in proc.stdout
    assert any(
        "patch clusterpolicy cluster-policy" in line
        for line in log.read_text(encoding="utf-8").splitlines()
    )


def test_microk8s_guard_api_wait_is_time_budget_not_attempt_budget(tmp_path: Path) -> None:
    script = Path("scripts/dev/microk8s-nvidia-guard").resolve()
    state = tmp_path / "guard.env"
    log = tmp_path / "microk8s.log"
    api_fail_count = tmp_path / "api-fail-count"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    api_fail_count.write_text("6", encoding="utf-8")
    state.write_text(
        "\n".join(
            [
                "GUARD_APPLIED=1",
                "KUSTOMIZATION_RECORDS=",
                "HELMRELEASE_PRESENT=0",
                "TOOLKIT_WAS_ENABLED=true",
            ]
        ),
        encoding="utf-8",
    )
    microk8s = fake_bin / "microk8s"
    microk8s.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {log}
if [[ "${{1:-}}" != "kubectl" ]]; then
  exit 1
fi
shift
if [[ "$*" == *"get nodes"* ]]; then
  count="$(cat {api_fail_count})"
  if (( count > 0 )); then
    printf '%s\\n' "$((count - 1))" > {api_fail_count}
    exit 1
  fi
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    microk8s.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "WORKERBEE_MK8S_GUARD_STATE": str(state),
        "WORKERBEE_MK8S_GUARD_ROLLBACK_ATTEMPTS": "2",
        "WORKERBEE_MK8S_GUARD_ROLLBACK_SLEEP": "0",
        "WORKERBEE_MK8S_GUARD_API_WAIT_SECONDS": "3",
    }

    proc = subprocess.run(
        ["bash", str(script), "rollback"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    assert "rollback: waiting up to 3s for MicroK8s API" in proc.stdout
    node_checks = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if "get nodes" in line
    ]
    assert len(node_checks) >= 7
    assert "rollback: re-enabling NVIDIA toolkit" in proc.stdout


def test_microk8s_guard_api_wait_fails_after_time_budget(tmp_path: Path) -> None:
    script = Path("scripts/dev/microk8s-nvidia-guard").resolve()
    state = tmp_path / "guard.env"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state.write_text(
        "\n".join(
            [
                "GUARD_APPLIED=1",
                "KUSTOMIZATION_RECORDS=",
                "HELMRELEASE_PRESENT=0",
                "TOOLKIT_WAS_ENABLED=true",
            ]
        ),
        encoding="utf-8",
    )
    microk8s = fake_bin / "microk8s"
    microk8s.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != "kubectl" ]]; then
  exit 1
fi
shift
if [[ "$*" == *"get nodes"* ]]; then
  exit 1
fi
exit 0
""",
        encoding="utf-8",
    )
    microk8s.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "WORKERBEE_MK8S_GUARD_STATE": str(state),
        "WORKERBEE_MK8S_GUARD_ROLLBACK_SLEEP": "1",
        "WORKERBEE_MK8S_GUARD_API_WAIT_SECONDS": "1",
    }

    proc = subprocess.run(
        ["bash", str(script), "rollback"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert proc.returncode == 1
    assert "rollback: waiting up to 1s for MicroK8s API" in proc.stdout
    assert "rollback: MicroK8s API did not become ready within 1s" in proc.stdout
    assert "error: MicroK8s is unavailable; cannot rollback guard" in proc.stdout
