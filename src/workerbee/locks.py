"""Cross-process locks for WorkerBee state roots and projects."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

from workerbee import __version__
from workerbee.contract import WorkerBeeError

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - Windows install support is command-oriented for now
    fcntl = None  # type: ignore[assignment]


class FileLock:
    def __init__(self, path: Path, *, label: str) -> None:
        self.path = path
        self.label = label
        self._handle = None

    def acquire(self, *, metadata: dict[str, Any] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            if fcntl is None:
                if self.path.stat().st_size > 0:
                    raise self._locked_error()
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise self._locked_error() from exc
        except Exception:
            handle.close()
            raise
        payload = {
            "label": self.label,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "argv": sys.argv,
            "started_at": time.time(),
            "workerbee_version": __version__,
            **(metadata or {}),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(payload, indent=2, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def _locked_error(self) -> WorkerBeeError:
        metadata = _read_lock_metadata(self.path)
        return WorkerBeeError(
            code="STATE_LOCKED",
            message=f"WorkerBee {self.label} is locked",
            details={"lock_file": str(self.path), "owner": metadata},
            retryable=True,
            remediation="Use the existing WorkerBee daemon or stop it before starting another.",
        )


def state_root_lock_path(state_root: Path) -> Path:
    return state_root.resolve() / "global" / "daemon.lock"


def project_lock_path(state_root: Path, project: str) -> Path:
    return state_root.resolve() / "projects" / project / ".lock"


def _read_lock_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
