"""WorkerBee stack supervisor."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from workerbee.http import request, wait_for_http
from workerbee.ingress import ProjectIngressConfig
from workerbee.k1s_runtime import resolve_k1s_runtime
from workerbee.paths import default_state_dir
from workerbee.poc import (
    POC_APPS,
    POC_NAMESPACE,
    build_images,
    validate_poc_urls,
    write_stack_files,
)
from workerbee.ports import choose_port


@dataclass(slots=True)
class StackInfo:
    project: str
    state_dir: str
    k1s_root: str | None
    k1s_runtime_source: str
    python_executable: str
    ae_origin: str | None
    runtime: str
    network: str
    controller_port: int
    apishim_port: int
    dashboard_url: str
    controller_url: str
    apishim_url: str
    admin_token: str
    read_token: str
    apishim_token: str
    controller_pid: int | None = None
    apishim_pid: int | None = None
    service_ports: dict[str, int] = field(default_factory=dict)
    ingress: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("admin_token", "read_token", "apishim_token"):
            data[key] = "***"
        return data


class WorkerBeeSupervisor:
    def __init__(
        self,
        *,
        project: str = "default",
        state_dir: Path | None = None,
        k1s_root: Path | None = None,
        runtime: str = "auto",
        cwd: Path | None = None,
        ingress: ProjectIngressConfig | None = None,
    ) -> None:
        self.project = _slug(project)
        self.cwd = (cwd or Path.cwd()).resolve()
        self.state_dir = (state_dir or default_state_dir(self.project, cwd=self.cwd)).resolve()
        self.k1s_runtime = resolve_k1s_runtime(cwd=self.cwd, k1s_root=k1s_root)
        self.k1s_root = self.k1s_runtime.k1s_root
        self.python_executable = self.k1s_runtime.python_executable
        self.runtime_requested = runtime
        self.ingress = ingress
        self.stack_file = self.state_dir / "stack.json"

    # Lifecycle -----------------------------------------------------
    def start(self) -> StackInfo:
        existing = self.load_stack()
        if existing and self._controller_healthy(existing) and self._apishim_healthy(existing):
            return existing
        if existing:
            self.stop(purge=False)

        runtime = self._resolve_runtime()
        self._ensure_dirs()
        network = f"workerbee-{self.project}"
        self._ensure_network(runtime, network)

        controller_port = _env_int("WORKERBEE_API_PORT") or choose_port(
            19108, start=19108, end=19208
        )
        apishim_port = _env_int("WORKERBEE_APISHIM_PORT") or choose_port(
            18445, start=18445, end=18545
        )
        service_ports = {
            "store": choose_port(19080, start=19080, end=19107),
            "api": choose_port(19081, start=19280, end=19320),
            "frontend": choose_port(19082, start=19321, end=19360),
        }

        admin_token = _reuse_or_token(existing, "admin_token")
        read_token = _reuse_or_token(existing, "read_token")
        apishim_token = _reuse_or_token(existing, "apishim_token")

        info = StackInfo(
            project=self.project,
            state_dir=str(self.state_dir),
            k1s_root=str(self.k1s_root) if self.k1s_root else None,
            k1s_runtime_source=self.k1s_runtime.source,
            python_executable=self.python_executable,
            ae_origin=self.k1s_runtime.ae_origin,
            runtime=runtime,
            network=network,
            controller_port=controller_port,
            apishim_port=apishim_port,
            dashboard_url=f"http://127.0.0.1:{controller_port}/dashboard",
            controller_url=f"http://127.0.0.1:{controller_port}",
            apishim_url=f"https://127.0.0.1:{apishim_port}",
            admin_token=admin_token,
            read_token=read_token,
            apishim_token=apishim_token,
            service_ports=service_ports,
            ingress=self.ingress.public_dict() if self.ingress else {"enabled": False},
        )

        self._write_stack(info)
        apishim_pid = self._start_apishim(info)
        info.apishim_pid = apishim_pid
        self._write_stack(info)
        controller_pid = self._start_controller(info)
        info.controller_pid = controller_pid
        self._write_stack(info)

        wait_for_http(
            f"{info.controller_url}/health",
            token=info.read_token,
            timeout_seconds=45,
            ok_statuses={200},
        )
        wait_for_http(
            f"{info.apishim_url}/healthz",
            token=info.apishim_token,
            timeout_seconds=45,
            verify_tls=False,
            ok_statuses={200},
        )
        return info

    def stop(self, *, purge: bool = False) -> dict[str, Any]:
        info = self.load_stack()
        stopped: list[int] = []
        if info:
            for pid in (info.controller_pid, info.apishim_pid):
                if pid and _pid_alive(pid):
                    _terminate_pid(pid)
                    stopped.append(pid)
            self._cleanup_runtime(info, purge=purge)
        if purge and self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        elif self.stack_file.exists():
            self.stack_file.unlink()
        return {"stopped_pids": stopped, "purged": purge, "state_dir": str(self.state_dir)}

    def status(self) -> dict[str, Any]:
        info = self.load_stack()
        if not info:
            return {"running": False, "state_dir": str(self.state_dir)}
        return {
            "running": self._controller_healthy(info),
            "apishim_running": self._apishim_healthy(info),
            "stack": info.public_dict(),
        }

    def tls_info(self) -> dict[str, Any]:
        info = self.start()
        return {
            "ok": True,
            "project": self.project,
            "apishim_url": info.apishim_url,
            "ca_bundle": str(self.state_dir / "apishim.ca.crt"),
            "server_cert": str(self.state_dir / "apishim.crt"),
            "server_key": "***",
            "trust_guidance": (
                "For v0.1 WorkerBee exposes the local dev CA path but does not install it "
                "into the OS trust store. Use the CA bundle with client tools that need "
                "strict TLS verification."
            ),
        }

    def reset(self) -> dict[str, Any]:
        info = self.start()
        self._reset_poc(info)
        artifacts = self.state_dir / "artifacts"
        if artifacts.exists():
            shutil.rmtree(artifacts)
        artifacts.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "project": self.project, "state_dir": str(self.state_dir)}

    # POC -----------------------------------------------------------
    def deploy_poc_stack(self, *, timeout_seconds: float = 180.0) -> dict[str, Any]:
        info = self.start()
        self._reset_poc(info)
        image_tags = build_images(
            runtime=info.runtime,
            state_dir=self.state_dir,
            project=self.project,
        )
        artifacts = write_stack_files(
            state_dir=self.state_dir,
            project=self.project,
            image_tags=image_tags,
            service_ports=info.service_ports,
            ingress_domain=self.ingress.domain if self.ingress else None,
        )

        apply_results = []
        for manifest in artifacts.manifests:
            apply_results.append(
                self.run_ae(
                    [
                        "--server",
                        info.controller_url,
                        "--token",
                        info.admin_token,
                        "apply",
                        "-f",
                        str(manifest),
                        "--force-namespace",
                        "-n",
                        "workerbee-poc",
                    ],
                    info=info,
                    timeout=90,
                )
            )

        validation = validate_poc_urls(artifacts.urls, timeout_seconds=timeout_seconds)
        return {
            "stack": info.public_dict(),
            "manifests": [str(p) for p in artifacts.manifests],
            "images": artifacts.image_tags,
            "urls": artifacts.urls,
            "ingress_urls": self._ingress_urls_for_paths(artifacts.manifests),
            "apply": apply_results,
            "validation": validation,
        }

    def build_image(self, context: Path, *, tag: str | None = None) -> dict[str, Any]:
        runtime = self._resolve_runtime()
        build_context = context.expanduser().resolve()
        if not build_context.is_dir():
            raise FileNotFoundError(f"image build context not found: {build_context}")
        image_tag = tag or f"workerbee-{self.project}-{_slug(build_context.name)}:dev"
        proc = subprocess.run(
            [runtime, "build", "-t", image_tag, str(build_context)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
        result = {
            "ok": proc.returncode == 0,
            "project": self.project,
            "runtime": runtime,
            "tag": image_tag,
            "context": str(build_context),
            "stdout": proc.stdout,
        }
        if proc.returncode != 0:
            raise RuntimeError(json.dumps(result, indent=2))
        return result

    def deploy_manifest(
        self,
        manifest: Path,
        *,
        namespace: str | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        info = self.start()
        path = manifest.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"manifest not found: {path}")
        text = path.read_text(encoding="utf-8")
        if "apiVersion: ae.dev/v1alpha1" not in text:
            raise ValueError("v0.1 accepts native ae.dev/v1alpha1 k1s manifests only")
        args = [
            "--server",
            info.controller_url,
            "--token",
            info.admin_token,
            "apply",
            "-f",
            str(path),
        ]
        if namespace:
            args.extend(["--force-namespace", "-n", namespace])
        result = self.run_ae(args, info=info, timeout=timeout)
        return {
            "ok": True,
            "project": self.project,
            "manifest": str(path),
            "namespace": namespace,
            "ingress_urls": self._ingress_urls_for_paths([path]),
            "apply": result,
        }

    def export_k8s(self) -> dict[str, Any]:
        info = self.start()
        manifest_dir = self.state_dir / "specs"
        manifests = sorted(manifest_dir.glob("*.yaml"))
        if not manifests:
            raise RuntimeError("no POC manifests found; run workerbee deploy-poc first")
        out_dir = self.state_dir / "artifacts" / "k8s"
        out_dir.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        combined: list[str] = []
        for manifest in manifests:
            result = self.run_ae(
                [
                    "export-k8s",
                    "-f",
                    str(manifest),
                    "--namespace",
                    "workerbee-poc",
                    "--emit-configs",
                    "--emit-secrets",
                    "--emit-namespace",
                    "--validate",
                ],
                info=info,
                timeout=90,
            )
            out_file = out_dir / f"{manifest.stem}.k8s.yaml"
            out_file.write_text(result["stdout"], encoding="utf-8")
            files.append(str(out_file))
            combined.append(result["stdout"].strip())
        combined_file = out_dir / "workerbee-poc.all.yaml"
        combined_file.write_text(
            "\n---\n".join(part for part in combined if part) + "\n",
            encoding="utf-8",
        )
        files.append(str(combined_file))
        return {"output_dir": str(out_dir), "files": files}

    def apishim_smoke(self) -> dict[str, Any]:
        info = self.start()
        base = info.apishim_url.rstrip("/")
        results: dict[str, Any] = {}
        for name, path in {
            "pods": "/api/v1/namespaces/workerbee-poc/pods",
            "services": "/api/v1/namespaces/workerbee-poc/services",
            "deployments": "/apis/apps/v1/namespaces/workerbee-poc/deployments",
        }.items():
            resp = request(
                f"{base}{path}",
                token=info.apishim_token,
                timeout=5,
                verify_tls=False,
            )
            payload: Any
            try:
                payload = resp.json()
            except Exception:
                payload = resp.text[:500]
            count = len(payload.get("items", [])) if isinstance(payload, dict) else None
            results[name] = {"status": resp.status, "count": count}
        pod_count = int(results.get("pods", {}).get("count") or 0)
        return {
            "ok": pod_count >= len(POC_APPS),
            "apishim_url": info.apishim_url,
            "results": results,
            "note": "native k1s manifests currently mirror as pods through the API shim",
        }

    def poc_status(self) -> dict[str, Any]:
        info = self.start()
        apps: dict[str, Any] = {}
        ok = True
        for app in POC_APPS:
            try:
                result = self.run_ae(
                    [
                        "--server",
                        info.controller_url,
                        "--token",
                        info.read_token,
                        "status",
                        app,
                        "-n",
                        POC_NAMESPACE,
                    ],
                    info=info,
                    timeout=30,
                )
                ready = "ready=1" in result["stdout"] and "live=1" in result["stdout"]
                apps[app] = {"ready": ready, "stdout": result["stdout"].strip()}
                ok = ok and ready
            except Exception as exc:  # noqa: BLE001
                apps[app] = {"ready": False, "error": str(exc)}
                ok = False
        return {"ok": ok, "apps": apps}

    def logs(self, app: str = "api", *, tail: int = 80) -> dict[str, Any]:
        info = self.start()
        result = self.run_ae(
            [
                "--server",
                info.controller_url,
                "--token",
                info.read_token,
                "logs",
                f"workerbee-poc/{app}",
                "--tail",
                str(tail),
            ],
            info=info,
            timeout=30,
        )
        if result["stdout"].strip() or result["stderr"].strip():
            result["source"] = "k1s"
            return result
        return self._runtime_logs(info, app=app, tail=tail)

    def run_exec(self, app: str, command: list[str]) -> dict[str, Any]:
        info = self.start()
        return self._runtime_exec(info, app=app, command=command)

    def _ingress_urls_for_paths(self, manifests: list[Path]) -> list[str]:
        if not self.ingress:
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for manifest in manifests:
            try:
                text = manifest.read_text(encoding="utf-8")
            except OSError:
                continue
            for match in re.finditer(r"(?m)^\s*host:\s*([A-Za-z0-9_.-]+)\s*$", text):
                host = match.group(1).strip()
                if host and host not in seen:
                    seen.add(host)
                    urls.append(self.ingress.url(host))
        return urls

    # k1s command helpers ------------------------------------------
    def run_ae(
        self,
        args: list[str],
        *,
        info: StackInfo | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        stack = info or self.start()
        env = self._base_env(stack)
        proc = subprocess.run(
            [self.python_executable, "-m", "ae.cli", *args],
            cwd=self.cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        result = {
            "cmd": _mask_sensitive_args([self.python_executable, "-m", "ae.cli", *args]),
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        if proc.returncode != 0:
            raise RuntimeError(json.dumps(result, indent=2))
        return result

    def load_stack(self) -> StackInfo | None:
        if not self.stack_file.exists():
            return None
        try:
            data = json.loads(self.stack_file.read_text(encoding="utf-8"))
            data.setdefault("k1s_runtime_source", self.k1s_runtime.source)
            data.setdefault("python_executable", self.python_executable)
            data.setdefault("ae_origin", self.k1s_runtime.ae_origin)
            data.setdefault("ingress", {"enabled": False})
            return StackInfo(**data)
        except Exception:
            return None

    # Internal ------------------------------------------------------
    def _ensure_dirs(self) -> None:
        for rel in ("logs", "specs", "pids", "artifacts", "caddy"):
            (self.state_dir / rel).mkdir(parents=True, exist_ok=True)

    def _write_stack(self, info: StackInfo) -> None:
        self._ensure_dirs()
        tmp = self.stack_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(info), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.stack_file)

    def _resolve_runtime(self) -> str:
        requested = self.runtime_requested.lower()
        if requested in {"podman", "docker"}:
            if shutil.which(requested) is None:
                raise RuntimeError(f"{requested} not found on PATH")
            return requested
        for candidate in ("podman", "docker"):
            if shutil.which(candidate):
                return candidate
        raise RuntimeError("Podman or Docker is required")

    def _ensure_network(self, runtime: str, network: str) -> None:
        if runtime == "podman":
            exists = subprocess.run(
                ["podman", "network", "exists", network],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if exists.returncode != 0:
                subprocess.run(
                    ["podman", "network", "create", network],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            exists = subprocess.run(
                ["docker", "network", "inspect", network],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if exists.returncode != 0:
                subprocess.run(
                    ["docker", "network", "create", network],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    def _base_env(self, info: StackInfo) -> dict[str, str]:
        env = self.k1s_runtime.apply_env(os.environ.copy())
        env.update(
            {
                "AE_RUNTIME_BACKEND": info.runtime,
                "AE_CONTAINER_CLI": info.runtime,
                "AE_NETWORK_NAME": info.network,
                "AE_STATE_DB": str(self.state_dir / "controller.db"),
                "AE_STATE_BACKEND": "sqlite",
                "AE_SPECS_DIR": str(self.state_dir / "specs"),
                "AE_PROJECTION_ROOT": str(self.state_dir / "projections"),
                "AE_REGISTER_LOCAL_NODE": "1",
                "AE_LABS": "1",
                "AE_DASHBOARD": "1",
                "AE_DASHBOARD_INTERACTIVE_TOOLS": "1",
                "AE_API_MUTATIONS": "1",
                "AE_API_ADMIN_TOKEN": info.admin_token,
                "AE_API_READ_TOKEN": info.read_token,
                "AE_API_SCALER_TOKEN": info.admin_token,
                "AE_APISHIM_ENABLE": "1",
                "AE_APISHIM_TOKEN": info.apishim_token,
                "AE_APISHIM_READ_TOKEN": info.read_token,
                "AE_APISHIM_DB": str(self.state_dir / "apishim.db"),
                "AE_APISHIM_SERVER": info.apishim_url,
                "AE_APISHIM_TLS_CERT": str(self.state_dir / "apishim.crt"),
                "AE_APISHIM_TLS_KEY": str(self.state_dir / "apishim.key"),
                "AE_APISHIM_CA_BUNDLE": str(self.state_dir / "apishim.ca.crt"),
                "AE_APISHIM_SESSION_SECRET": _stable_secret(self.state_dir / "session.secret"),
                "AE_ALLOW_PLAINTEXT_SECRETS": "1",
            }
        )
        if self.ingress:
            env.update(
                {
                    "AE_CADDY_SITES": str(self.ingress.sites_dir),
                    "AE_CADDY_CONTAINER": self.ingress.caddy_container,
                    "AE_CADDY_FILE": self.ingress.caddy_file,
                    "AE_CADDY_HOST_ALIAS": self.ingress.host_alias,
                    "AE_CADDY_RELOAD_TIMEOUT": "10",
                }
            )
        if info.runtime == "podman":
            env["AE_PODMAN_NETWORK"] = info.network
        else:
            env["AE_DOCKER_NETWORK"] = info.network
        return env

    def _start_apishim(self, info: StackInfo) -> int:
        env = self._base_env(info)
        self._ensure_apishim_tls(env)
        log = open(self.state_dir / "logs" / "apishim.log", "ab")  # noqa: SIM115
        proc = subprocess.Popen(
            [
                self.python_executable,
                "-m",
                "ae.apishim",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(info.apishim_port),
                "--tls",
            ],
            cwd=self.cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return int(proc.pid)

    def _start_controller(self, info: StackInfo) -> int:
        env = self._base_env(info)
        log = open(self.state_dir / "logs" / "controller.log", "ab")  # noqa: SIM115
        proc = subprocess.Popen(
            [
                self.python_executable,
                "-m",
                "ae.controller",
                "--loop",
                "--specs",
                str(self.state_dir / "specs"),
                "--metrics-port",
                str(info.controller_port),
                "--watch",
            ],
            cwd=self.cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return int(proc.pid)

    def _ensure_apishim_tls(self, env: dict[str, str]) -> None:
        cert = Path(env["AE_APISHIM_TLS_CERT"])
        key = Path(env["AE_APISHIM_TLS_KEY"])
        ca = Path(env["AE_APISHIM_CA_BUNDLE"])
        if cert.exists() and key.exists() and ca.exists():
            return
        helper_env = env.copy()
        helper_env.update(
            {
                "APISHIM_ENV_FILE": str(self.state_dir / "apishim.env"),
                "APISHIM_CERT_FILE": str(cert),
                "APISHIM_KEY_FILE": str(key),
                "APISHIM_CA_FILE": str(ca),
                "APISHIM_CA_KEY_FILE": str(self.state_dir / "apishim.ca.key"),
            }
        )
        proc = self._run_packaged_apishim_env_helper(helper_env)
        if proc.returncode != 0 and self.k1s_root is not None:
            script = self.k1s_root / "scripts" / "ensure_apishim_env.sh"
            if script.exists():
                proc = subprocess.run(
                    [str(script)],
                    cwd=self.k1s_root,
                    env=helper_env,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
        if not cert.exists() or not key.exists():
            raise RuntimeError(
                "failed to generate local apishim TLS material\n"
                + (proc.stdout or "")[-2000:]
            )

    def _run_packaged_apishim_env_helper(
        self, helper_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        code = (
            "from ae.apishim.env import ensure_local_apishim_env; "
            "ensure_local_apishim_env()"
        )
        return subprocess.run(
            [self.python_executable, "-c", code],
            cwd=self.cwd,
            env=helper_env,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _reset_poc(self, info: StackInfo) -> None:
        spec_dir = self.state_dir / "specs"
        for app in POC_APPS:
            spec = spec_dir / f"{app}.yaml"
            if spec.exists():
                spec.unlink()
        for app in reversed(POC_APPS):
            with suppress(Exception):
                self.run_ae(
                    [
                        "--server",
                        info.controller_url,
                        "--token",
                        info.admin_token,
                        "delete",
                        app,
                        "-n",
                        POC_NAMESPACE,
                        "--purge",
                    ],
                    info=info,
                    timeout=30,
                )
        self._cleanup_runtime(info, purge=False)
        time.sleep(0.5)

    def _controller_healthy(self, info: StackInfo) -> bool:
        try:
            resp = request(f"{info.controller_url}/health", token=info.read_token, timeout=1.5)
            return resp.status == 200
        except Exception:
            return False

    def _apishim_healthy(self, info: StackInfo) -> bool:
        try:
            resp = request(
                f"{info.apishim_url}/healthz",
                token=info.apishim_token,
                timeout=1.5,
                verify_tls=False,
            )
            return resp.status == 200
        except Exception:
            return False

    def _cleanup_runtime(self, info: StackInfo, *, purge: bool) -> None:
        namespace_filter = f"ae.namespace={POC_NAMESPACE}"
        if info.runtime == "podman":
            ids = _split_lines(
                subprocess.run(
                    ["podman", "ps", "-aq", "--filter", f"label={namespace_filter}"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                ).stdout
            )
            if ids:
                subprocess.run(
                    ["podman", "rm", "-f", *ids],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if purge:
                subprocess.run(
                    ["podman", "network", "rm", info.network],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return

        ids = _split_lines(
            subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"label={namespace_filter}"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ).stdout
        )
        if ids:
            subprocess.run(
                ["docker", "rm", "-f", *ids],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if purge:
            subprocess.run(
                ["docker", "network", "rm", info.network],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _runtime_container_ids(self, info: StackInfo, app: str) -> list[str]:
        filters = [
            "--filter",
            f"label=ae.namespace={POC_NAMESPACE}",
            "--filter",
            f"label=app={app}",
        ]
        if info.runtime == "podman":
            cmd = ["podman", "ps", "-q", *filters]
        else:
            cmd = ["docker", "ps", "-q", *filters]
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"{info.runtime} ps failed")
        return _split_lines(proc.stdout)

    def _runtime_logs(self, info: StackInfo, *, app: str, tail: int) -> dict[str, Any]:
        ids = self._runtime_container_ids(info, app)
        if not ids:
            raise RuntimeError(f"no running POC container found for app {app!r}")
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        for cid in ids:
            cmd = [info.runtime, "logs", "--tail", str(tail), cid]
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=30,
            )
            stdout_parts.append(proc.stdout)
            stderr_parts.append(proc.stderr)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"{info.runtime} logs failed")
        return {
            "source": info.runtime,
            "cmd": [info.runtime, "logs", "--tail", str(tail), *ids],
            "returncode": 0,
            "stdout": "".join(stdout_parts),
            "stderr": "".join(stderr_parts),
        }

    def _runtime_exec(self, info: StackInfo, *, app: str, command: list[str]) -> dict[str, Any]:
        ids = self._runtime_container_ids(info, app)
        if not ids:
            raise RuntimeError(f"no running POC container found for app {app!r}")
        cmd = [info.runtime, "exec", ids[0], *command]
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=45,
        )
        result = {
            "source": info.runtime,
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        if proc.returncode != 0:
            raise RuntimeError(json.dumps(result, indent=2))
        return result


def _slug(value: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    out = "-".join(part for part in out.split("-") if part)
    return out or "default"


def project_slug(value: str) -> str:
    return _slug(value)


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _reuse_or_token(existing: StackInfo | None, field_name: str) -> str:
    if existing:
        value = getattr(existing, field_name, "")
        if value:
            return str(value)
    return secrets.token_urlsafe(32)


def _stable_secret(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return value


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _split_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _mask_sensitive_args(args: list[str]) -> list[str]:
    masked = list(args)
    for idx, value in enumerate(masked[:-1]):
        if value in {"--token", "--password"}:
            masked[idx + 1] = "***"
    return masked
