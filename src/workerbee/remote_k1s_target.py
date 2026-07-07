"""Standalone local k1s target used by WorkerBee live validation."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import textwrap
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from workerbee.contract import WorkerBeeError
from workerbee.http import wait_for_http
from workerbee.ingress import ProjectIngressConfig
from workerbee.paths import daemon_project_state_dir, resolve_k1s_root
from workerbee.ports import choose_port
from workerbee.runtime_support import (
    CONTAINERD_RUNTIME,
    containerd_address,
    containerd_cni_bin_dir,
    containerd_cni_conf_dir,
    containerd_data_root,
    containerd_namespace,
    containerd_network_name,
    containerd_network_subnet,
    runtime_command_args,
    workerbee_runtime_labels,
)
from workerbee.secrets import secret_env_for_project, write_private_json
from workerbee.supervisor import project_slug

DEFAULT_TARGET_IMAGE = "docker.io/library/python:3.12-slim"
TARGET_NAME = "remote-k1s"


@dataclass(slots=True)
class RemoteK1sTargetInfo:
    project: str
    state_root: str
    target_dir: str
    controller_url: str
    apishim_url: str
    dashboard_url: str
    admin_token: str
    read_token: str
    apishim_token: str
    cleanup_command: str
    components: list[dict[str, Any]] = field(default_factory=list)
    ingress_urls: dict[str, str] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("admin_token", "read_token", "apishim_token"):
            data[key] = "***"
        return data


class RemoteK1sTarget:
    """Start a standalone local k1s controller target for remote deploy testing."""

    def __init__(
        self,
        *,
        state_root: Path,
        project: str,
        cwd: Path,
        ingress: ProjectIngressConfig | None = None,
        k1s_root: Path | None = None,
    ) -> None:
        self.state_root = state_root.expanduser().resolve()
        self.project = project_slug(project)
        self.cwd = cwd.expanduser().resolve()
        self.ingress = ingress
        self.k1s_root = k1s_root.expanduser().resolve() if k1s_root else resolve_k1s_root(self.cwd)
        self.project_state = daemon_project_state_dir(self.project, state_root=self.state_root)
        self.target_dir = self.project_state / TARGET_NAME
        self.info_file = self.target_dir / "target.json"
        self.network = containerd_network_name(self.state_root, self.project)

    @property
    def cleanup_command(self) -> str:
        root = shlex.quote(str(self.state_root))
        return (
            f"workerbee --runtime containerd --state-root {root} "
            "cleanup --execute --purge-images"
        )

    def start(self, *, timeout: float = 180.0) -> RemoteK1sTargetInfo:
        self._ensure_layout()
        self._write_helper_bridge()
        self._ensure_network()
        tokens = self._tokens()
        controller_port = choose_port(19680, start=19680, end=19780)
        apishim_port = choose_port(18680, start=18680, end=18780)
        ingress_urls = self._write_ingress_sites(
            controller_port=controller_port,
            apishim_port=apishim_port,
        )
        self._reload_ingress()
        apishim = self._start_apishim(host_port=apishim_port, token=tokens["apishim_token"])
        controller = self._start_controller(
            host_port=controller_port,
            apishim_port=apishim_port,
            admin_token=tokens["admin_token"],
            read_token=tokens["read_token"],
            apishim_token=tokens["apishim_token"],
        )
        info = RemoteK1sTargetInfo(
            project=self.project,
            state_root=str(self.state_root),
            target_dir=str(self.target_dir),
            controller_url=f"http://127.0.0.1:{controller_port}",
            apishim_url=f"http://127.0.0.1:{apishim_port}",
            dashboard_url=ingress_urls.get(
                "dashboard",
                f"http://127.0.0.1:{controller_port}/dashboard",
            ),
            admin_token=tokens["admin_token"],
            read_token=tokens["read_token"],
            apishim_token=tokens["apishim_token"],
            cleanup_command=self.cleanup_command,
            components=[apishim, controller],
            ingress_urls=ingress_urls,
        )
        ready = self._wait_ready(info, timeout=timeout)
        if not ready["ok"]:
            raise WorkerBeeError(
                code="REMOTE_K1S_TARGET_NOT_READY",
                message="standalone remote k1s target did not become ready",
                details={"target": info.public_dict(), "ready": ready},
                remediation="Inspect target component logs or rerun with keep-alive enabled.",
                retryable=True,
            )
        self.info_file.write_text(json.dumps(info.public_dict(), indent=2), encoding="utf-8")
        return info

    def stop(self) -> dict[str, Any]:
        info = self.load()
        targets = [item.get("name") for item in (info.components if info else [])]
        if not targets:
            targets = [self._component_name("apishim"), self._component_name("controller")]
        removed = self._rm_containers([str(item) for item in targets if item])
        if self.ingress:
            with suppress(FileNotFoundError):
                (self.ingress.sites_dir / f"{TARGET_NAME}.caddy").unlink()
            self._reload_ingress()
        return {"ok": True, "removed": removed, "cleanup_command": self.cleanup_command}

    def load(self) -> RemoteK1sTargetInfo | None:
        if not self.info_file.is_file():
            return None
        try:
            data = json.loads(self.info_file.read_text(encoding="utf-8"))
            return RemoteK1sTargetInfo(
                project=str(data["project"]),
                state_root=str(data["state_root"]),
                target_dir=str(data["target_dir"]),
                controller_url=str(data["controller_url"]),
                apishim_url=str(data["apishim_url"]),
                dashboard_url=str(data["dashboard_url"]),
                admin_token="",
                read_token="",
                apishim_token="",
                cleanup_command=str(data["cleanup_command"]),
                components=list(data.get("components") or []),
                ingress_urls=dict(data.get("ingress_urls") or {}),
            )
        except Exception:
            return None

    def _ensure_layout(self) -> None:
        for rel in ("bin", "logs", "specs", "state"):
            (self.target_dir / rel).mkdir(parents=True, exist_ok=True)

    def _write_helper_bridge(self) -> Path:
        path = self.target_dir / "bin" / "workerbee-nerdctl"
        if not path.is_file():
            path.write_text(_helper_bridge_script(), encoding="utf-8")
            path.chmod(0o755)
        return path

    def _ensure_network(self) -> None:
        exists = subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=self.state_root,
                project=self.project,
                args=["network", "inspect", self.network],
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        if exists.returncode == 0:
            return
        self._run_nerdctl(
            [
                "network",
                "create",
                "--subnet",
                containerd_network_subnet(self.state_root, self.project),
                self.network,
            ],
            timeout=60,
        )

    def _tokens(self) -> dict[str, str]:
        token_file = self.target_dir / "tokens.json"
        if token_file.is_file():
            try:
                data = json.loads(token_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        "admin_token": str(data.get("admin_token") or secrets.token_urlsafe(24)),
                        "read_token": str(data.get("read_token") or secrets.token_urlsafe(24)),
                        "apishim_token": str(
                            data.get("apishim_token") or secrets.token_urlsafe(24)
                        ),
                    }
            except Exception:
                token_file.unlink(missing_ok=True)
        tokens = {
            "admin_token": secrets.token_urlsafe(24),
            "read_token": secrets.token_urlsafe(24),
            "apishim_token": secrets.token_urlsafe(24),
        }
        write_private_json(token_file, tokens)
        return tokens

    def _common_env(self) -> dict[str, str]:
        project_data_root = containerd_data_root(self.state_root, project=self.project)
        project_cni_conf = containerd_cni_conf_dir(self.state_root, project=self.project)
        system_data_root = containerd_data_root(self.state_root, system=True)
        system_cni_conf = containerd_cni_conf_dir(self.state_root, system=True)
        env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": "/workspace/src",
            "WORKERBEE_CONTAINERD_HELPER_SOCKET": os.getenv(
                "WORKERBEE_CONTAINERD_HELPER_SOCKET",
                str(self.state_root / "global" / "containerd-helper.sock"),
            ),
            "WORKERBEE_CONTAINERD_ADDRESS": containerd_address(),
            "WORKERBEE_CONTAINERD_NAMESPACE": containerd_namespace(
                self.state_root,
                self.project,
            ),
            "WORKERBEE_CONTAINERD_DATA_ROOT": str(project_data_root),
            "WORKERBEE_CONTAINERD_CNI_PATH": containerd_cni_bin_dir(),
            "WORKERBEE_CONTAINERD_CNI_NETCONFPATH": str(project_cni_conf),
            "WORKERBEE_CONTAINERD_SYSTEM_NAMESPACE": containerd_namespace(
                self.state_root,
                system=True,
            ),
            "WORKERBEE_CONTAINERD_SYSTEM_DATA_ROOT": str(system_data_root),
            "WORKERBEE_CONTAINERD_SYSTEM_CNI_NETCONFPATH": str(system_cni_conf),
            "AE_RUNTIME_BACKEND": CONTAINERD_RUNTIME,
            "AE_INFRA_BACKEND": CONTAINERD_RUNTIME,
            "AE_CONTAINER_CLI": str(self.target_dir / "bin" / "workerbee-nerdctl"),
            "AE_NERDCTL_BIN": str(self.target_dir / "bin" / "workerbee-nerdctl"),
            "AE_CONTAINERD_ADDRESS": containerd_address(),
            "AE_CRI_ENDPOINT": containerd_address(),
            "AE_CONTAINERD_NAMESPACE": containerd_namespace(self.state_root, self.project),
            "AE_CONTAINERD_DATA_ROOT": str(project_data_root),
            "AE_CONTAINERD_NETWORK": self.network,
            "AE_CONTAINERD_NETWORK_SUBNET": containerd_network_subnet(
                self.state_root,
                self.project,
            ),
            "AE_CONTAINERD_CNI_BIN_DIR": containerd_cni_bin_dir(),
            "AE_CONTAINERD_CNI_CONF_DIR": str(project_cni_conf),
            "NETCONFPATH": str(project_cni_conf),
            "AE_NETWORK_NAME": self.network,
            "AE_API_MUTATIONS": "1",
            "AE_LABS": "1",
            "AE_DASHBOARD": "1",
            "AE_DASHBOARD_INTERACTIVE_TOOLS": "1",
            "AE_REGISTER_LOCAL_NODE": "1",
            "AE_STATE_BACKEND": "sqlite",
            "AE_STATE_DB": str(self.target_dir / "state" / "controller.db"),
            "AE_SPECS_DIR": str(self.target_dir / "specs"),
            "AE_PROJECTION_ROOT": str(self.target_dir / "state" / "projections"),
            "DEV_PROFILE_DIR": str(self.target_dir / "state"),
        }
        env.update(secret_env_for_project(self.project_state))
        if self.ingress:
            env.update(
                {
                    "AE_CADDY_SITES": str(self.ingress.sites_dir),
                    "AE_CADDY_CONTAINER": self.ingress.caddy_container,
                    "AE_CADDY_FILE": self.ingress.caddy_file,
                    "AE_CADDY_HOST_ALIAS": self.ingress.host_alias,
                    "AE_CADDY_PREFER_HOST_PORT_UPSTREAMS": "1",
                    "AE_CADDY_RELOAD_TIMEOUT": "10",
                    "WORKERBEE_CONTAINERD_SYSTEM_CONTAINER": self.ingress.caddy_container,
                }
            )
        return env

    def _start_apishim(self, *, host_port: int, token: str) -> dict[str, Any]:
        name = self._component_name("apishim")
        self._rm_containers([name])
        env = self._common_env()
        env.update(
            {
                "AE_APISHIM_ENABLE": "1",
                "AE_APISHIM_ALLOW_ANON": "1",
                "AE_APISHIM_TOKEN": token,
                "AE_APISHIM_READ_TOKEN": token,
                "AE_APISHIM_RBAC": "1",
                "AE_APISHIM_RBAC_EVAL": "1",
                "AE_APISHIM_DB": str(self.target_dir / "state" / "apishim.db"),
                "AE_APISHIM_RUNTIME": CONTAINERD_RUNTIME,
            }
        )
        command = (
            "cd /workspace && "
            "python -m pip install --no-cache-dir -r /workspace/requirements.txt "
            ">/tmp/k1s-pip-install.log 2>&1 && "
            "exec python -m ae.apishim serve --host 0.0.0.0 --port 8445 --allow-anonymous"
        )
        return self._run_component(
            name=name,
            role="apishim",
            host_port=host_port,
            container_port=8445,
            env=env,
            command=command,
        )

    def _start_controller(
        self,
        *,
        host_port: int,
        apishim_port: int,
        admin_token: str,
        read_token: str,
        apishim_token: str,
    ) -> dict[str, Any]:
        name = self._component_name("controller")
        self._rm_containers([name])
        env = self._common_env()
        env.update(
            {
                "AE_CONTROLLER_ID": f"{TARGET_NAME}-{self.project}",
                "AE_CONTROLLER_ADVERTISE_ADDR": f"http://{name}:9108",
                "AE_API_ADMIN_TOKEN": admin_token,
                "AE_API_READ_TOKEN": read_token,
                "AE_API_SCALER_TOKEN": admin_token,
                "AE_APISHIM_SERVER": f"http://{self._component_name('apishim')}:8445",
                "AE_APISHIM_PUBLIC_BASE": self._api_public_base(apishim_port),
                "AE_APISHIM_TOKEN": apishim_token,
                "AE_APISHIM_READ_TOKEN": read_token,
                "AE_DASHBOARD_BOOTSTRAP_TOKEN": admin_token,
            }
        )
        command = (
            "cd /workspace && "
            "python -m pip install --no-cache-dir -r /workspace/requirements.txt "
            ">/tmp/k1s-pip-install.log 2>&1 && "
            "exec python -m ae.controller --loop --specs \"$AE_SPECS_DIR\" "
            "--metrics-port 9108 --watch"
        )
        return self._run_component(
            name=name,
            role="controller",
            host_port=host_port,
            container_port=9108,
            env=env,
            command=command,
        )

    def _run_component(
        self,
        *,
        name: str,
        role: str,
        host_port: int,
        container_port: int,
        env: dict[str, str],
        command: str,
    ) -> dict[str, Any]:
        image = os.getenv("WORKERBEE_REMOTE_K1S_TARGET_IMAGE", DEFAULT_TARGET_IMAGE)
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self.network,
            "-p",
            f"127.0.0.1:{host_port}:{container_port}",
            *self._mount_args(),
            *self._env_args(env),
            *self._label_args(),
            image,
            "/bin/sh",
            "-ec",
            command,
        ]
        proc = self._run_nerdctl(args, timeout=120)
        return {
            "name": name,
            "role": role,
            "image": image,
            "container_id": proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None,
            "host_port": host_port,
            "container_port": container_port,
            "url": f"http://127.0.0.1:{host_port}",
        }

    def _mount_args(self) -> list[str]:
        mounts = [
            f"{self.k1s_root}:/workspace:ro",
            f"{self.state_root}:{self.state_root}",
            f"{self.target_dir / 'specs'}:{self.target_dir / 'specs'}",
            f"{self.target_dir / 'state'}:{self.target_dir / 'state'}",
        ]
        helper_socket_mount = self._helper_socket_mount()
        if helper_socket_mount:
            mounts.append(helper_socket_mount)
        args: list[str] = []
        for mount in mounts:
            args.extend(["-v", mount])
        return args

    def _helper_socket_mount(self) -> str | None:
        raw = os.getenv("WORKERBEE_CONTAINERD_HELPER_SOCKET")
        if not raw:
            return None
        socket_path = Path(raw).expanduser().resolve()
        if not socket_path.exists() or socket_path.is_relative_to(self.state_root):
            return None
        return f"{socket_path}:{socket_path}"

    def _env_args(self, env: dict[str, str]) -> list[str]:
        args: list[str] = []
        for key, value in sorted(env.items()):
            args.extend(["-e", f"{key}={value}"])
        return args

    def _label_args(self) -> list[str]:
        args: list[str] = []
        for label in [
            *workerbee_runtime_labels(state_root=self.state_root, project=self.project),
            "workerbee.component=remote-k1s-target",
        ]:
            args.extend(["--label", label])
        return args

    def _write_ingress_sites(self, *, controller_port: int, apishim_port: int) -> dict[str, str]:
        if not self.ingress:
            return {}
        controller_host = self.ingress.host("k1s-remote")
        api_host = self.ingress.host("k1s-remote-api")
        site = self.ingress.sites_dir / f"{TARGET_NAME}.caddy"
        site.parent.mkdir(parents=True, exist_ok=True)
        content = f"""# Generated by WorkerBee remote k1s live target.
https://{controller_host} {{
    header -Strict-Transport-Security
    tls internal
    reverse_proxy {self.ingress.host_alias}:{controller_port}
}}

https://{api_host} {{
    header -Strict-Transport-Security
    tls internal
    reverse_proxy {self.ingress.host_alias}:{apishim_port}
}}
"""
        if not site.is_file() or site.read_text(encoding="utf-8") != content:
            site.write_text(content, encoding="utf-8")
        return {
            "controller": self.ingress.url(controller_host, "/"),
            "dashboard": self.ingress.url(controller_host, "/dashboard"),
            "controller_health": self.ingress.url(controller_host, "/health"),
            "apishim": self.ingress.url(api_host, "/"),
            "api_healthz": self.ingress.url(api_host, "/healthz"),
        }

    def _api_public_base(self, apishim_port: int) -> str:
        if self.ingress:
            return self.ingress.url(
                self.ingress.host("k1s-remote-api"),
                "/",
            ).rstrip("/")
        return f"http://127.0.0.1:{apishim_port}"

    def _reload_ingress(self) -> dict[str, Any]:
        if not self.ingress:
            return {"ok": False, "enabled": False, "reason": "ingress is not configured"}
        proc = subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=self.state_root,
                project=None,
                system=True,
                args=[
                    "exec",
                    self.ingress.caddy_container,
                    "caddy",
                    "reload",
                    "--config",
                    self.ingress.caddy_file,
                ],
            ),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout}

    def _wait_ready(self, info: RemoteK1sTargetInfo, *, timeout: float) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        try:
            wait_for_http(
                f"{info.controller_url}/health",
                token=info.read_token,
                timeout_seconds=timeout,
                ok_statuses={200},
            )
            checks.append({"name": "controller", "ok": True})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "controller", "ok": False, "error": str(exc)})
        try:
            wait_for_http(
                f"{info.apishim_url}/healthz",
                token=info.apishim_token,
                timeout_seconds=max(1.0, min(timeout, 30.0)),
                ok_statuses={200},
            )
            checks.append({"name": "apishim", "ok": True})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "apishim", "ok": False, "error": str(exc)})
        return {"ok": all(item["ok"] for item in checks), "checks": checks}

    def _run_nerdctl(
        self,
        args: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=self.state_root,
                project=self.project,
                args=args,
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout.strip() or f"nerdctl {' '.join(args)} failed")
        return proc

    def _rm_containers(self, names: list[str]) -> list[dict[str, Any]]:
        removed: list[dict[str, Any]] = []
        for name in names:
            proc = subprocess.run(
                runtime_command_args(
                    CONTAINERD_RUNTIME,
                    state_root=self.state_root,
                    project=self.project,
                    args=["rm", "-f", name],
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            removed.append({"name": name, "returncode": proc.returncode, "stdout": proc.stdout})
        return removed

    def _component_name(self, role: str) -> str:
        return f"{self.network}-{TARGET_NAME}-{role}"[:120].rstrip("-")


def _helper_bridge_script() -> str:
    script = r'''
import base64
import json
import os
import socket
import sys


def main() -> int:
    socket_path = os.environ.get("WORKERBEE_CONTAINERD_HELPER_SOCKET")
    if not socket_path:
        sys.stderr.write("WORKERBEE_CONTAINERD_HELPER_SOCKET is not set\n")
        return 127
    argv = sys.argv[1:]
    if not _has_global_args(argv):
        argv = [*_base_args(argv), *argv]
    stdin = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    payload = {"action": "run", "argv": argv}
    if stdin:
        payload["stdin_b64"] = base64.b64encode(stdin).decode("ascii")
    response = _request(socket_path, payload)
    stdout = base64.b64decode(str(response.get("stdout_b64") or ""))
    stderr = base64.b64decode(str(response.get("stderr_b64") or ""))
    stdout = _normalize_nerdctl_stdout(argv, stdout)
    if stdout:
        sys.stdout.buffer.write(stdout)
    if stderr:
        sys.stderr.buffer.write(stderr)
    return int(response.get("returncode") or 0)


def _has_global_args(argv: list[str]) -> bool:
    return "--address" in argv and "--namespace" in argv


def _base_args(argv: list[str]) -> list[str]:
    use_system = (
        len(argv) >= 2
        and argv[0] == "exec"
        and argv[1] == os.environ.get("WORKERBEE_CONTAINERD_SYSTEM_CONTAINER")
    )
    prefix = "WORKERBEE_CONTAINERD_SYSTEM_" if use_system else "WORKERBEE_CONTAINERD_"
    return [
        "--address",
        os.environ["WORKERBEE_CONTAINERD_ADDRESS"],
        "--namespace",
        os.environ[prefix + "NAMESPACE"],
        "--data-root",
        os.environ[prefix + "DATA_ROOT"],
        "--cni-path",
        os.environ["WORKERBEE_CONTAINERD_CNI_PATH"],
        "--cni-netconfpath",
        os.environ[prefix + "CNI_NETCONFPATH"],
    ]


def _normalize_nerdctl_stdout(argv: list[str], stdout: bytes) -> bytes:
    if not _is_images_json_command(argv) or not stdout.strip():
        return stdout
    text = stdout.decode("utf-8", errors="replace").strip()
    if text.startswith("["):
        return stdout
    try:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    except Exception:
        return stdout
    return (json.dumps(rows) + "\n").encode("utf-8")


def _is_images_json_command(argv: list[str]) -> bool:
    compact = [item for item in argv if item != "--"]
    try:
        command_index = compact.index("images")
    except ValueError:
        return False
    for index, item in enumerate(compact[command_index + 1 :], start=command_index + 1):
        if item == "--format" and index + 1 < len(compact):
            return compact[index + 1] == "json"
        if item.startswith("--format="):
            return item.split("=", 1)[1] == "json"
    return False


def _request(socket_path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(socket_path)
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


if __name__ == "__main__":
    raise SystemExit(main())
'''
    return "#!/usr/bin/env python3\n" + textwrap.dedent(script).lstrip()
