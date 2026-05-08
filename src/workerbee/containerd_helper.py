"""Root helper for explicit WorkerBee direct-containerd development mode."""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError
from workerbee.runtime_support import (
    CONTAINERD_RESERVED_NAMESPACES,
    CONTAINERD_RUNTIME,
    containerd_address,
    containerd_nerdctl_probe,
    nerdctl_binary,
)

CONTAINERD_PRIVILEGE_MODES = ("auto", "sudo-helper", "unprivileged")
HELPER_SOCKET_FILE = "containerd-helper.sock"
HELPER_METADATA_FILE = "containerd-helper.json"
HELPER_LOG_FILE = "containerd-helper.log"
HELPER_WRAPPER_FILE = "workerbee-nerdctl"
HELPER_REQUEST_LIMIT = 512 * 1024 * 1024

_GLOBAL_VALUE_FLAGS = {
    "-H": "address",
    "--H": "address",
    "-a": "address",
    "--a": "address",
    "--address": "address",
    "--host": "address",
    "-n": "namespace",
    "--n": "namespace",
    "--namespace": "namespace",
    "--data-root": "data_root",
    "--cni-path": "cni_path",
    "--cni-netconfpath": "cni_netconfpath",
    "--snapshotter": "snapshotter",
    "--storage-driver": "snapshotter",
    "--hosts-dir": "hosts_dir",
    "--host-gateway-ip": "host_gateway_ip",
    "--cgroup-manager": "cgroup_manager",
}
_DENIED_COMMANDS = {"system"}
_DENIED_TOKENS = {"prune"}


def effective_containerd_privilege_mode(runtime: str, requested: str = "auto") -> str:
    mode = requested.lower()
    if mode not in CONTAINERD_PRIVILEGE_MODES:
        raise ValueError(f"unsupported containerd privilege mode: {requested}")
    if runtime.lower() != CONTAINERD_RUNTIME:
        return "off"
    return mode


def containerd_privilege_status(
    *,
    state_root: Path,
    runtime: str,
    mode: str = "auto",
) -> dict[str, Any]:
    effective = effective_containerd_privilege_mode(runtime, mode)
    root = state_root.expanduser().resolve()
    status: dict[str, Any] = {
        "enabled": effective != "off",
        "requested_mode": mode,
        "effective_mode": effective,
        "runtime": runtime,
        "state_root": str(root),
        "address": containerd_address(),
        "helper": containerd_helper_status(root),
    }
    if effective != "off":
        status["unprivileged_probe"] = containerd_nerdctl_probe(containerd_address())
    return status


def ensure_containerd_privilege(
    *,
    state_root: Path,
    runtime: str,
    mode: str = "auto",
    timeout: float = 20.0,
) -> dict[str, Any]:
    effective = effective_containerd_privilege_mode(runtime, mode)
    root = state_root.expanduser().resolve()
    if effective == "off":
        return {
            "ok": True,
            "enabled": False,
            "requested_mode": mode,
            "effective_mode": effective,
            "runtime": runtime,
            "env": {},
        }

    probe = containerd_nerdctl_probe(containerd_address())
    if probe.get("ok"):
        return {
            "ok": True,
            "enabled": True,
            "requested_mode": mode,
            "effective_mode": "unprivileged",
            "runtime": runtime,
            "helper": None,
            "env": {},
            "unprivileged_probe": probe,
        }
    if effective == "unprivileged":
        _raise_probe_error(probe)

    helper = ensure_containerd_helper(root, timeout=timeout)
    return {
        "ok": True,
        "enabled": True,
        "requested_mode": mode,
        "effective_mode": "sudo-helper",
        "runtime": runtime,
        "helper": helper,
        "env": helper.get("env") or {},
        "unprivileged_probe": probe,
    }


def ensure_containerd_helper(state_root: Path, *, timeout: float = 20.0) -> dict[str, Any]:
    root = state_root.expanduser().resolve()
    root.joinpath("global").mkdir(parents=True, exist_ok=True)
    wrapper = write_containerd_helper_client_wrapper(root)
    status = containerd_helper_status(root)
    if status.get("responsive"):
        probe = containerd_helper_nerdctl_probe(root)
        _raise_helper_probe_error(probe)
        return {
            "ok": True,
            "running": True,
            "started": False,
            "wrapper": str(wrapper),
            "socket": status.get("socket"),
            "pid": status.get("pid"),
            "env": _helper_env(root),
            "nerdctl_probe": probe,
            "status": status,
        }

    sudo = shutil.which("sudo")
    if sudo is None:
        raise WorkerBeeError(
            code="CONTAINERD_HELPER_SUDO_MISSING",
            message="sudo is required for WorkerBee direct root-containerd helper mode",
            remediation=(
                "Install sudo, use Podman/Docker, or use --containerd-privilege unprivileged."
            ),
        )
    paths = _helper_paths(root)
    nerdctl = shutil.which(nerdctl_binary()) or nerdctl_binary()
    argv = [
        sudo,
        "-b",
        sys.executable,
        "-c",
        _helper_bootstrap_code(),
        "serve",
        "--state-root",
        str(root),
        "--socket",
        str(paths["socket"]),
        "--metadata",
        str(paths["metadata"]),
        "--user-uid",
        str(os.getuid()),
        "--user-gid",
        str(os.getgid()),
        "--nerdctl",
        nerdctl,
        "--address",
        containerd_address(),
    ]
    log = open(paths["log"], "ab")  # noqa: SIM115 - child owns inherited descriptor
    try:
        proc = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    finally:
        log.close()
    _wait_for_helper(root, proc=proc, timeout=timeout)
    status = containerd_helper_status(root)
    probe = containerd_helper_nerdctl_probe(root)
    if not probe.get("ok"):
        with suppress(Exception):
            stop_containerd_helper(root)
        _raise_helper_probe_error(probe)
    return {
        "ok": True,
        "running": True,
        "started": True,
        "wrapper": str(wrapper),
        "socket": str(paths["socket"]),
        "pid": status.get("pid") or int(proc.pid),
        "argv": argv,
        "env": _helper_env(root),
        "nerdctl_probe": probe,
        "status": status,
    }


def stop_containerd_helper(state_root: Path, *, timeout: float = 10.0) -> dict[str, Any]:
    root = state_root.expanduser().resolve()
    status = containerd_helper_status(root)
    if not status.get("responsive"):
        return {
            "ok": True,
            "running": bool(status.get("running")),
            "stopped": False,
            "status": status,
        }
    try:
        response = helper_request(Path(str(status["socket"])), {"action": "shutdown"}, timeout=5)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stopped": False, "error": str(exc), "status": status}
    pid = int(status.get("pid") or 0)
    deadline = time.monotonic() + timeout
    while pid and time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    stopped = not pid or not _pid_alive(pid)
    if stopped:
        paths = _helper_paths(root)
        with suppress(OSError):
            paths["metadata"].unlink()
        with suppress(OSError):
            paths["socket"].unlink()
    return {
        "ok": stopped,
        "stopped": stopped,
        "response": response,
        "status": containerd_helper_status(root),
    }


def remove_containerd_helper_tree(
    state_root: Path,
    target: Path,
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    root = state_root.expanduser().resolve()
    status = containerd_helper_status(root)
    socket_path = Path(str(status.get("socket") or _helper_paths(root)["socket"]))
    if not socket_path.exists():
        return {
            "ok": False,
            "removed": False,
            "path": str(target),
            "code": "CONTAINERD_HELPER_SOCKET_MISSING",
            "message": "WorkerBee containerd helper socket is not available",
        }
    return helper_request(
        socket_path,
        {"action": "remove_tree", "path": str(target)},
        timeout=timeout,
    )


def containerd_helper_status(state_root: Path) -> dict[str, Any]:
    root = state_root.expanduser().resolve()
    paths = _helper_paths(root)
    metadata = _read_json(paths["metadata"])
    pid = _metadata_pid(metadata)
    running = bool(pid and _pid_alive(pid))
    responsive = False
    ping: dict[str, Any] | None = None
    if paths["socket"].exists():
        try:
            ping = helper_request(paths["socket"], {"action": "ping"}, timeout=2)
            responsive = bool(ping.get("ok"))
        except Exception as exc:  # noqa: BLE001
            ping = {"ok": False, "error": str(exc)}
    return {
        "running": running,
        "responsive": responsive,
        "pid": pid,
        "socket": str(paths["socket"]),
        "metadata_file": str(paths["metadata"]),
        "log_file": str(paths["log"]),
        "wrapper": str(_helper_wrapper_path(root)),
        "metadata": metadata,
        "ping": ping,
    }


def containerd_helper_nerdctl_probe(state_root: Path) -> dict[str, Any]:
    root = state_root.expanduser().resolve()
    paths = _helper_paths(root)
    if not paths["socket"].exists():
        return {"ok": False, "code": "CONTAINERD_HELPER_SOCKET_MISSING"}
    try:
        response = helper_request(
            paths["socket"],
            {
                "action": "run",
                "argv": ["--address", containerd_address(), "namespace", "ls", "--quiet"],
                "timeout": 10,
            },
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "CONTAINERD_HELPER_PROBE_FAILED", "message": str(exc)}
    stdout = base64.b64decode(str(response.get("stdout_b64") or "")).decode(
        "utf-8",
        errors="replace",
    )
    stderr = base64.b64decode(str(response.get("stderr_b64") or "")).decode(
        "utf-8",
        errors="replace",
    )
    namespaces = [line.strip() for line in stdout.splitlines() if line.strip()]
    return {
        "ok": bool(response.get("ok")),
        "code": None if response.get("ok") else "CONTAINERD_HELPER_NERDCTL_FAILED",
        "returncode": response.get("returncode"),
        "stdout": stdout,
        "stderr": stderr,
        "namespaces": namespaces,
        "error": response.get("error"),
    }


def write_containerd_helper_client_wrapper(state_root: Path) -> Path:
    root = state_root.expanduser().resolve()
    path = _helper_wrapper_path(root)
    socket_path = _helper_paths(root)["socket"]
    path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/usr/bin/env sh\n"
        f"exec {shlex.quote(sys.executable)} -m workerbee.containerd_helper client "
        f"--socket {shlex.quote(str(socket_path))} -- \"$@\"\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def containerd_privilege_env(privilege: dict[str, Any]) -> dict[str, str]:
    raw = privilege.get("env") if isinstance(privilege, dict) else None
    if isinstance(raw, dict) and raw:
        return {str(key): str(value) for key, value in raw.items() if value is not None}
    helper = privilege.get("helper") if isinstance(privilege, dict) else None
    if not isinstance(helper, dict) or not bool(helper.get("responsive", True)):
        return {}
    wrapper = str(helper.get("wrapper") or "")
    socket_path = str(helper.get("socket") or "")
    if not wrapper or not socket_path:
        return {}
    return {
        "WORKERBEE_NERDCTL_BIN": wrapper,
        "AE_NERDCTL_BIN": wrapper,
        "WORKERBEE_CONTAINERD_HELPER_SOCKET": socket_path,
    }


@contextmanager
def temporary_containerd_privilege_env(env: dict[str, str] | None):
    if not env:
        yield
        return
    old = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def helper_request(
    socket_path: Path,
    payload: dict[str, Any],
    *,
    stdin: bytes = b"",
    timeout: float = 600.0,
) -> dict[str, Any]:
    request_payload = dict(payload)
    if stdin:
        request_payload["stdin_b64"] = base64.b64encode(stdin).decode("ascii")
    data = json.dumps(request_payload).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        _connect_helper_socket(sock, socket_path, timeout=timeout)
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _connect_helper_socket(sock: socket.socket, socket_path: Path, *, timeout: float) -> None:
    deadline = time.monotonic() + max(0.1, timeout)
    retry_errnos = {
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        errno.ECONNREFUSED,
        errno.ENOENT,
    }
    while True:
        remaining = max(0.1, deadline - time.monotonic())
        sock.settimeout(remaining)
        try:
            sock.connect(str(socket_path))
            return
        except OSError as exc:
            if exc.errno not in retry_errnos or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.05, max(0.01, deadline - time.monotonic())))


def validate_helper_argv(
    argv: list[str],
    *,
    state_root: Path,
    address: str | None = None,
) -> dict[str, Any]:
    root = state_root.expanduser().resolve()
    parsed = _parse_nerdctl_argv(argv)
    command = parsed["command"]
    command_args = parsed["command_args"]
    values = parsed["values"]
    expected_address = address or containerd_address()
    if command is None:
        if _is_nerdctl_version_diagnostic(argv):
            return {"ok": True, "command": "version", "diagnostic": True}
        raise WorkerBeeError(
            code="CONTAINERD_HELPER_EMPTY_COMMAND",
            message="containerd helper received no nerdctl command",
        )
    if _is_nerdctl_version_diagnostic(argv):
        return {"ok": True, "command": command, "diagnostic": True}
    if values.get("address") != expected_address:
        raise WorkerBeeError(
            code="CONTAINERD_HELPER_ADDRESS_DENIED",
            message="containerd helper requires the configured containerd address",
            details={"expected": expected_address, "actual": values.get("address")},
        )
    if command == "namespace" and command_args[:1] == ["ls"]:
        return {"ok": True, "command": command, "diagnostic": True}
    if command in _DENIED_COMMANDS or any(item in _DENIED_TOKENS for item in command_args):
        raise WorkerBeeError(
            code="CONTAINERD_HELPER_COMMAND_DENIED",
            message=f"containerd helper denied nerdctl command `{command}`",
            details={"command": command, "argv": argv},
            remediation="WorkerBee helper only accepts state-scoped container operations.",
        )
    namespace = str(values.get("namespace") or "")
    prefix = f"workerbee-{_state_hash(root)}-"
    if namespace in CONTAINERD_RESERVED_NAMESPACES or not namespace.startswith(prefix):
        raise WorkerBeeError(
            code="CONTAINERD_HELPER_NAMESPACE_DENIED",
            message=f"containerd helper denied namespace `{namespace or '(missing)'}`",
            details={
                "namespace": namespace,
                "required_prefix": prefix,
                "reserved_namespaces": sorted(CONTAINERD_RESERVED_NAMESPACES),
            },
        )
    data_root = values.get("data_root")
    cni_netconfpath = values.get("cni_netconfpath")
    if not data_root or not _under_root(Path(str(data_root)), root):
        raise WorkerBeeError(
            code="CONTAINERD_HELPER_DATA_ROOT_DENIED",
            message="containerd helper requires --data-root under the WorkerBee state root",
            details={"data_root": data_root, "state_root": str(root)},
        )
    if not cni_netconfpath or not _under_root(Path(str(cni_netconfpath)), root):
        raise WorkerBeeError(
            code="CONTAINERD_HELPER_CNI_PATH_DENIED",
            message="containerd helper requires --cni-netconfpath under the WorkerBee state root",
            details={"cni_netconfpath": cni_netconfpath, "state_root": str(root)},
        )
    return {
        "ok": True,
        "command": command,
        "namespace": namespace,
        "data_root": str(data_root),
        "cni_netconfpath": str(cni_netconfpath),
    }


def serve_helper(
    *,
    state_root: Path,
    socket_path: Path,
    metadata_file: Path,
    user_uid: int,
    user_gid: int,
    nerdctl: str,
    address: str,
) -> None:
    if os.geteuid() != 0:
        raise SystemExit("workerbee containerd helper must run as root")
    root = state_root.expanduser().resolve()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileNotFoundError):
        socket_path.unlink()
    stop = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        server.bind(str(socket_path))
    finally:
        os.umask(old_umask)
    os.chown(socket_path, int(user_uid), int(user_gid))
    os.chmod(socket_path, 0o600)
    server.listen(16)
    server.settimeout(0.5)
    metadata = {
        "pid": os.getpid(),
        "state_root": str(root),
        "socket": str(socket_path),
        "nerdctl": nerdctl,
        "address": address,
        "user_uid": int(user_uid),
        "user_gid": int(user_gid),
        "started_at": time.time(),
    }
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")

    def handle_connection(conn: socket.socket) -> None:
        with conn:
            response = _handle_helper_connection(
                conn,
                state_root=root,
                nerdctl=nerdctl,
                address=address,
            )
            if response.pop("_shutdown", False):
                stop.set()
            _send_helper_response(conn, response)

    try:
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    break
                raise
            thread = threading.Thread(
                target=handle_connection,
                args=(conn,),
                name="workerbee-containerd-helper-request",
                daemon=True,
            )
            thread.start()
    finally:
        server.close()
        with suppress(FileNotFoundError):
            socket_path.unlink()
        with suppress(FileNotFoundError):
            metadata_file.unlink()


def _send_helper_response(conn: socket.socket, response: dict[str, Any]) -> None:
    with suppress(BrokenPipeError):
        conn.sendall(json.dumps(response).encode("utf-8"))


def client_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workerbee-containerd-helper-client")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.args)
    if command and command[0] == "--":
        command = command[1:]
    stdin = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    response = helper_request(
        args.socket,
        {"action": "run", "argv": command},
        stdin=stdin,
    )
    stdout = base64.b64decode(str(response.get("stdout_b64") or ""))
    stderr = base64.b64decode(str(response.get("stderr_b64") or ""))
    if stdout:
        sys.stdout.buffer.write(stdout)
    if stderr:
        sys.stderr.buffer.write(stderr)
    return int(response.get("returncode") or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m workerbee.containerd_helper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--state-root", type=Path, required=True)
    serve.add_argument("--socket", type=Path, required=True)
    serve.add_argument("--metadata", type=Path, required=True)
    serve.add_argument("--user-uid", type=int, required=True)
    serve.add_argument("--user-gid", type=int, required=True)
    serve.add_argument("--nerdctl", required=True)
    serve.add_argument("--address", required=True)
    client = sub.add_parser("client")
    client.add_argument("--socket", type=Path, required=True)
    client.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.cmd == "client":
        client_args = ["--socket", str(args.socket), *list(args.args)]
        return client_main(client_args)
    serve_helper(
        state_root=args.state_root,
        socket_path=args.socket,
        metadata_file=args.metadata,
        user_uid=args.user_uid,
        user_gid=args.user_gid,
        nerdctl=args.nerdctl,
        address=args.address,
    )
    return 0


def _handle_helper_connection(
    conn: socket.socket,
    *,
    state_root: Path,
    nerdctl: str,
    address: str,
) -> dict[str, Any]:
    try:
        raw = _recv_all(conn, limit=HELPER_REQUEST_LIMIT)
        payload = json.loads(raw.decode("utf-8"))
        action = str(payload.get("action") or "run")
        if action == "ping":
            return {"ok": True, "pid": os.getpid(), "euid": os.geteuid()}
        if action == "shutdown":
            return {"ok": True, "_shutdown": True}
        if action == "remove_tree":
            return _handle_remove_tree(payload, state_root=state_root)
        if action != "run":
            return _error_response("CONTAINERD_HELPER_BAD_ACTION", f"unsupported action {action}")
        argv = payload.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            return _error_response("CONTAINERD_HELPER_BAD_ARGV", "argv must be a string list")
        try:
            validate_helper_argv(argv, state_root=state_root, address=address)
        except WorkerBeeError as exc:
            return _error_response(exc.code, exc.message, details=exc.public_dict())
        stdin = base64.b64decode(str(payload.get("stdin_b64") or ""))
        proc = subprocess.run(
            [nerdctl, *argv],
            input=stdin,
            capture_output=True,
            timeout=int(payload.get("timeout") or 600),
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout_b64": base64.b64encode(proc.stdout or b"").decode("ascii"),
            "stderr_b64": base64.b64encode(proc.stderr or b"").decode("ascii"),
        }
    except Exception as exc:  # noqa: BLE001
        return _error_response("CONTAINERD_HELPER_INTERNAL_ERROR", str(exc))


def _handle_remove_tree(payload: dict[str, Any], *, state_root: Path) -> dict[str, Any]:
    raw = str(payload.get("path") or "").strip()
    if not raw:
        return _error_response("CONTAINERD_HELPER_REMOVE_PATH_MISSING", "path is required")
    root = state_root.expanduser().resolve()
    projects_root = root / "projects"
    target = Path(raw).expanduser().resolve()
    if target == projects_root or not _under_root(target, projects_root):
        return _error_response(
            "CONTAINERD_HELPER_REMOVE_PATH_DENIED",
            "containerd helper only removes WorkerBee project state paths",
            details={"path": str(target), "projects_root": str(projects_root)},
        )
    if not target.exists() and not target.is_symlink():
        return {"ok": True, "removed": False, "path": str(target)}
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    except Exception as exc:  # noqa: BLE001
        return _error_response(
            "CONTAINERD_HELPER_REMOVE_PATH_FAILED",
            str(exc),
            details={"path": str(target)},
        )
    return {"ok": True, "removed": True, "path": str(target)}


def _recv_all(conn: socket.socket, *, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = conn.recv(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise RuntimeError("containerd helper request exceeded size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _error_response(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stderr = f"{code}: {message}\n".encode()
    return {
        "ok": False,
        "returncode": 126,
        "stdout_b64": "",
        "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        "error": {"code": code, "message": message, "details": details or {}},
    }


def _parse_nerdctl_argv(argv: list[str]) -> dict[str, Any]:
    values: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if arg.startswith("-"):
            flag = arg
            value = None
            if "=" in arg:
                flag, value = arg.split("=", 1)
            normalized = _GLOBAL_VALUE_FLAGS.get(flag)
            if normalized:
                if value is None:
                    i += 1
                    if i >= len(argv):
                        break
                    value = argv[i]
                values[normalized] = value
            i += 1
            continue
        break
    command = argv[i] if i < len(argv) else None
    command_args = argv[i + 1 :] if command is not None else []
    return {"values": values, "command": command, "command_args": command_args}


def _is_nerdctl_version_diagnostic(argv: list[str]) -> bool:
    compact = [item for item in argv if item != "--"]
    return compact in (["--version"], ["-v"], ["version"])


def _helper_env(state_root: Path) -> dict[str, str]:
    wrapper = str(_helper_wrapper_path(state_root))
    return {
        "WORKERBEE_NERDCTL_BIN": wrapper,
        "AE_NERDCTL_BIN": wrapper,
        "WORKERBEE_CONTAINERD_HELPER_SOCKET": str(_helper_paths(state_root)["socket"]),
    }


def _helper_paths(state_root: Path) -> dict[str, Path]:
    root = state_root.expanduser().resolve()
    global_dir = root / "global"
    return {
        "socket": global_dir / HELPER_SOCKET_FILE,
        "metadata": global_dir / HELPER_METADATA_FILE,
        "log": global_dir / HELPER_LOG_FILE,
    }


def _helper_wrapper_path(state_root: Path) -> Path:
    return state_root.expanduser().resolve() / "global" / "bin" / HELPER_WRAPPER_FILE


def _helper_bootstrap_code() -> str:
    import_root = Path(__file__).resolve().parents[1]
    return (
        "import sys; "
        f"sys.path.insert(0, {str(import_root)!r}); "
        "from workerbee.containerd_helper import main; "
        "raise SystemExit(main())"
    )


def _wait_for_helper(state_root: Path, *, proc: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    socket_path = _helper_paths(state_root)["socket"]
    last_error = None
    launcher_returncode = None
    while time.monotonic() < deadline:
        launcher_returncode = proc.poll()
        if launcher_returncode is not None and launcher_returncode != 0:
            break
        try:
            response = helper_request(socket_path, {"action": "ping"}, timeout=1)
            if response.get("ok"):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(0.2)
    log_tail = _tail(_helper_paths(state_root)["log"])
    raise WorkerBeeError(
        code="CONTAINERD_HELPER_START_FAILED",
        message="WorkerBee containerd helper did not become ready",
        details={
            "last_error": str(last_error) if last_error else None,
            "launcher_returncode": launcher_returncode,
            "log_tail": log_tail,
        },
        remediation="Check sudo privileges, nerdctl, and containerd health, then retry.",
        retryable=True,
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _metadata_pid(metadata: dict[str, Any]) -> int | None:
    try:
        pid = int(metadata.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _under_root(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _state_hash(state_root: Path) -> str:
    return hashlib.sha1(str(state_root.resolve()).encode("utf-8")).hexdigest()[:12]  # noqa: S324


def _raise_probe_error(probe: dict[str, Any]) -> None:
    raise WorkerBeeError(
        code=str(probe.get("code") or "CONTAINERD_UNPRIVILEGED_PROBE_FAILED"),
        message=str(probe.get("message") or "nerdctl cannot access containerd unprivileged"),
        details={"probe": probe},
        remediation=(
            "Use --containerd-privilege sudo-helper for root containerd, or use Podman/Docker."
        ),
        retryable=True,
    )


def _raise_helper_probe_error(probe: dict[str, Any]) -> None:
    if probe.get("ok"):
        return
    raise WorkerBeeError(
        code=str(probe.get("code") or "CONTAINERD_HELPER_NERDCTL_FAILED"),
        message=str(probe.get("stderr") or probe.get("message") or "root helper nerdctl failed"),
        details={"probe": probe},
        remediation=(
            "Confirm `sudo nerdctl --address unix:///run/containerd/containerd.sock "
            "namespace ls` works."
        ),
        retryable=True,
    )


def _tail(path: Path, *, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
