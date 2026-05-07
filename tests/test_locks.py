import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workerbee.contract import WorkerBeeError
from workerbee.locks import FileLock


def test_file_lock_writes_metadata(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "daemon.lock", label="state root")
    lock.acquire(metadata={"mcp_bind_url": "http://127.0.0.1:8765/mcp"})
    try:
        data = json.loads((tmp_path / "daemon.lock").read_text(encoding="utf-8"))
        assert data["label"] == "state root"
        assert data["mcp_bind_url"] == "http://127.0.0.1:8765/mcp"
        assert data["pid"] == os.getpid()
    finally:
        lock.release()


def test_file_lock_blocks_another_process(tmp_path: Path) -> None:
    lock_path = tmp_path / "daemon.lock"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import time; "
                "from pathlib import Path; "
                "from workerbee.locks import FileLock; "
                f"lock=FileLock(Path({str(lock_path)!r}), label='state root'); "
                "lock.acquire(); print('locked', flush=True); time.sleep(5)"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(WorkerBeeError) as exc:
            FileLock(lock_path, label="state root").acquire()
        assert exc.value.code == "STATE_LOCKED"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        time.sleep(0.1)
