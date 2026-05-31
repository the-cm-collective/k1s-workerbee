"""WorkerBee stack supervisor."""

from __future__ import annotations

import hashlib
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

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is provided by the k1s runtime package
    yaml = None  # type: ignore[assignment]

from workerbee.containerd_helper import remove_containerd_helper_tree
from workerbee.contract import WorkerBeeError
from workerbee.http import request, wait_for_http
from workerbee.ingress import ProjectIngressConfig
from workerbee.k1s_runtime import K1sRuntime, resolve_k1s_runtime
from workerbee.paths import default_state_dir
from workerbee.poc import (
    POC_APPS,
    POC_NAMESPACE,
    build_images,
    validate_poc_urls,
    write_stack_files,
)
from workerbee.ports import choose_port
from workerbee.runtime_support import (
    CONTAINERD_RUNTIME,
    build_image_with_runtime,
    containerd_address,
    containerd_cni_bin_dir,
    containerd_cni_conf_dir,
    containerd_data_root,
    containerd_namespace,
    containerd_network_name,
    containerd_network_subnet,
    nerdctl_binary,
    resolve_runtime,
    runtime_command_args,
    workerbee_runtime_labels,
    write_containerd_cli_wrapper,
)
from workerbee.secrets import secret_env_for_project, write_private_json


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
    ingress_urls: dict[str, str] = field(default_factory=dict)

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
        self._requested_k1s_root = k1s_root
        self._k1s_runtime: K1sRuntime | None = None
        self.runtime_requested = runtime
        self.ingress = ingress
        self.stack_file = self.state_dir / "stack.json"

    @property
    def k1s_runtime(self) -> K1sRuntime:
        if self._k1s_runtime is None:
            self._k1s_runtime = resolve_k1s_runtime(
                cwd=self.cwd,
                k1s_root=self._requested_k1s_root,
            )
        return self._k1s_runtime

    @property
    def k1s_root(self) -> Path | None:
        return self.k1s_runtime.k1s_root

    @property
    def python_executable(self) -> str:
        return self.k1s_runtime.python_executable

    # Lifecycle -----------------------------------------------------
    def start(self) -> StackInfo:
        existing = self.load_stack()
        if existing and self._controller_healthy(existing) and self._apishim_healthy(existing):
            if self._stack_requires_ingress_restart(existing):
                self.stop(purge=False)
            else:
                if self.ingress:
                    self._refresh_stack_ingress_info(existing)
                return existing
        elif existing:
            self.stop(purge=False)

        runtime = self._resolve_runtime()
        self._ensure_dirs()
        self._cleanup_project_runtime_containers(runtime, include_namespaces=False)
        state_root = self.state_dir.parent.parent
        network = (
            containerd_network_name(state_root, self.project)
            if runtime == CONTAINERD_RUNTIME
            else f"workerbee-{self.project}"
        )
        self._ensure_network(runtime, network)

        controller_port = _env_int("WORKERBEE_API_PORT") or choose_port(
            19108, start=19108, end=19208
        )
        apishim_port = _env_int("WORKERBEE_APISHIM_PORT") or choose_port(
            18445, start=18445, end=18545
        )
        service_ports = self._allocate_poc_service_ports(runtime)

        admin_token = _reuse_or_token(existing, "admin_token")
        read_token = _reuse_or_token(existing, "read_token")
        apishim_token = _reuse_or_token(existing, "apishim_token")
        ingress_urls = self._write_stack_ingress_sites(
            controller_port=controller_port,
            apishim_port=apishim_port,
        )
        dashboard_url = ingress_urls.get("dashboard") or f"http://127.0.0.1:{controller_port}/dashboard"

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
            dashboard_url=dashboard_url,
            controller_url=f"http://127.0.0.1:{controller_port}",
            apishim_url=f"https://127.0.0.1:{apishim_port}",
            admin_token=admin_token,
            read_token=read_token,
            apishim_token=apishim_token,
            service_ports=service_ports,
            ingress=self.ingress.public_dict() if self.ingress else {"enabled": False},
            ingress_urls=ingress_urls,
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
        if self.ingress:
            self._reload_ingress()
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
            self._remove_stack_ingress_site()
        elif purge and self._resolve_runtime() == CONTAINERD_RUNTIME:
            self._cleanup_containerd_runtime(
                network=containerd_network_name(self.state_dir.parent.parent, self.project),
                purge=True,
            )
        purge_result = None
        if purge and self.state_dir.exists():
            purge_result = self._purge_state_dir(info)
        elif self.stack_file.exists():
            self.stack_file.unlink()
        return {
            "ok": not (isinstance(purge_result, dict) and purge_result.get("ok") is False),
            "stopped_pids": stopped,
            "purged": purge,
            "state_dir": str(self.state_dir),
            "purge_result": purge_result,
        }

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
            runtime=info.runtime,
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

        validation = self._validate_poc_runtime(
            info,
            urls=artifacts.urls,
            timeout_seconds=timeout_seconds,
        )
        return {
            "stack": info.public_dict(),
            "manifests": [str(p) for p in artifacts.manifests],
            "images": artifacts.image_tags,
            "urls": artifacts.urls,
            "ingress_urls": self._ingress_urls_for_paths(artifacts.manifests),
            "apply": apply_results,
            "validation": validation,
        }

    def build_image(
        self,
        context: Path,
        *,
        tag: str | None = None,
        dockerfile: Path | None = None,
    ) -> dict[str, Any]:
        runtime = self._resolve_runtime()
        build_context = context.expanduser().resolve()
        if not build_context.is_dir():
            raise FileNotFoundError(f"image build context not found: {build_context}")
        image_tag = tag or f"workerbee-{self.project}-{_slug(build_context.name)}:dev"
        labels = workerbee_runtime_labels(
            state_root=self.state_dir.parent.parent,
            project=self.project,
        )
        result = build_image_with_runtime(
            runtime=runtime,
            state_root=self.state_dir.parent.parent,
            project=self.project,
            context=build_context,
            dockerfile=dockerfile,
            tag=image_tag,
            labels=labels,
        )
        result = {
            **result,
            "project": self.project,
        }
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
        result = self._apply_manifest_direct(
            info=info,
            manifest=path,
            namespace=namespace,
            timeout=timeout,
            fallback_args=args,
        )
        return {
            "ok": True,
            "project": self.project,
            "manifest": str(path),
            "namespace": namespace,
            "ingress_urls": self._ingress_urls_for_paths([path]),
            "apply": result,
        }

    def deploy_k8s_manifest(
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
        args = [
            "--server",
            info.controller_url,
            "--token",
            info.admin_token,
            "apply",
            "--k8s",
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
            "input_kind": "kubernetes",
            "namespace": namespace,
            "ingress_urls": self._ingress_urls_for_paths([path]),
            "apply": result,
        }

    def _apply_manifest_direct(
        self,
        *,
        info: StackInfo,
        manifest: Path,
        namespace: str | None,
        timeout: int,
        fallback_args: list[str],
    ) -> dict[str, Any]:
        if yaml is None:
            return self.run_ae(fallback_args, info=info, timeout=timeout)
        docs = [
            doc
            for doc in yaml.safe_load_all(manifest.read_text(encoding="utf-8"))
            if isinstance(doc, dict)
        ]
        if len(docs) != 1:
            raise ValueError("expected a single Deployment manifest document")
        payload = docs[0]
        if namespace:
            metadata = payload.setdefault("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("manifest metadata must be a mapping")
            metadata["namespace"] = namespace

        cmd = _mask_sensitive_args(
            [
                self.python_executable,
                "-m",
                "ae.cli",
                "--server",
                info.controller_url,
                "--token",
                info.admin_token,
                "apply",
                "-f",
                str(manifest),
                *(["--force-namespace", "-n", namespace] if namespace else []),
            ]
        )
        try:
            response = request(
                f"{info.controller_url.rstrip('/')}/apply",
                method="POST",
                token=info.admin_token,
                headers={"Accept": "application/json"},
                json_body=payload,
                timeout=float(_bounded_cli_http_timeout(timeout)),
            )
            body = response.text
        except Exception as exc:  # noqa: BLE001 - preserve ae.cli-style deploy diagnostics
            raise RuntimeError(
                json.dumps(
                    {
                        "cmd": cmd,
                        "returncode": 1,
                        "stdout": f"remote apply failed: {exc}\n",
                        "stderr": "",
                    },
                    indent=2,
                )
            ) from exc
        if response.status >= 400:
            raise RuntimeError(
                json.dumps(
                    {
                        "cmd": cmd,
                        "returncode": 1,
                        "stdout": f"remote apply failed: HTTP {response.status}\n{body}",
                        "stderr": "",
                    },
                    indent=2,
                )
            )
        try:
            data = response.json() or {}
        except Exception as exc:  # noqa: BLE001 - preserve ae.cli-style deploy diagnostics
            raise RuntimeError(
                json.dumps(
                    {
                        "cmd": cmd,
                        "returncode": 1,
                        "stdout": f"remote apply failed: invalid JSON response: {exc}\n{body}",
                        "stderr": "",
                    },
                    indent=2,
                )
            ) from exc
        return {
            "cmd": cmd,
            "returncode": 0,
            "stdout": _remote_apply_stdout(data),
            "stderr": "",
            "http_status": response.status,
            "response": data,
            "transport": "direct-controller",
        }

    def deploy_remote_manifest(
        self,
        manifest: Path,
        *,
        server: str,
        token: str,
        namespace: str | None = None,
        timeout: int = 180,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        path = manifest.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"manifest not found: {path}")
        args = ["--server", server, "--token", token, "apply", "-f", str(path)]
        if namespace:
            args.extend(["--force-namespace", "-n", namespace])
        result = self.run_ae_cli(args, timeout=timeout, env_overrides=env_overrides)
        return {
            "ok": True,
            "project": self.project,
            "server": server,
            "manifest": str(path),
            "namespace": namespace,
            "apply": result,
        }

    def deploy_remote_k8s_manifest(
        self,
        manifest: Path,
        *,
        server: str,
        token: str,
        namespace: str | None = None,
        timeout: int = 180,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        path = manifest.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"manifest not found: {path}")
        args = ["--server", server, "--token", token, "apply", "--k8s", "-f", str(path)]
        if namespace:
            args.extend(["--force-namespace", "-n", namespace])
        result = self.run_ae_cli(args, timeout=timeout, env_overrides=env_overrides)
        return {
            "ok": True,
            "project": self.project,
            "server": server,
            "manifest": str(path),
            "input_kind": "kubernetes",
            "namespace": namespace,
            "apply": result,
        }

    def export_k8s(self) -> dict[str, Any]:
        info = self.start()
        manifests = self._poc_manifest_paths()
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

    def _poc_manifest_paths(self) -> list[Path]:
        artifact_dir = self.state_dir / "artifacts" / "poc-specs"
        manifests = sorted(artifact_dir.glob("*.yaml"))
        if manifests:
            return manifests
        legacy_dir = self.state_dir / "specs"
        return sorted(legacy_dir.glob("*.yaml"))

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
        if not self._poc_manifest_paths():
            return self._poc_status_once(info)
        deadline = time.monotonic() + _env_float("WORKERBEE_POC_STATUS_TIMEOUT", 30.0)
        last = self._poc_status_once(info)
        while not last.get("ok") and time.monotonic() < deadline:
            time.sleep(2.0)
            last = self._poc_status_once(info)
        return last

    def _poc_status_once(self, info: StackInfo) -> dict[str, Any]:
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

    def logs(
        self,
        app: str = "api",
        *,
        namespace: str | None = None,
        tail: int = 80,
        include_exited: bool = True,
    ) -> dict[str, Any]:
        info = self.start()
        resolved_namespace, resolved_app = _resolve_app_ref(
            app,
            namespace=namespace,
            default_namespace=project_slug(self.project),
        )
        k1s_error = None
        try:
            result = self.run_ae(
                [
                    "--server",
                    info.controller_url,
                    "--token",
                    info.read_token,
                    "logs",
                    f"{resolved_namespace}/{resolved_app}",
                    "--tail",
                    str(tail),
                ],
                info=info,
                timeout=30,
            )
            if result["stdout"].strip() or result["stderr"].strip():
                result["source"] = "k1s"
                result["resolved_namespace"] = resolved_namespace
                result["resolved_app"] = resolved_app
                return result
        except Exception as exc:  # noqa: BLE001
            k1s_error = str(exc)
        result = self._runtime_logs(
            info,
            app=resolved_app,
            namespace=resolved_namespace,
            tail=tail,
            include_exited=include_exited,
        )
        if k1s_error:
            result["k1s_error"] = k1s_error
        return result

    def run_exec(
        self,
        app: str,
        command: list[str],
        *,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        info = self.start()
        resolved_namespace, resolved_app = _resolve_app_ref(
            app,
            namespace=namespace,
            default_namespace=project_slug(self.project),
        )
        return self._runtime_exec(
            info,
            app=resolved_app,
            namespace=resolved_namespace,
            command=command,
        )

    def _ingress_urls_for_paths(self, manifests: list[Path]) -> list[str]:
        if not self.ingress:
            return []
        urls: list[str] = []
        seen: set[str] = set()

        def add_host(host: object) -> None:
            if not isinstance(host, str):
                return
            host = host.strip().strip("\"'")
            if host and host not in seen:
                seen.add(host)
                urls.append(self.ingress.url(host))

        for manifest in manifests:
            try:
                text = manifest.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed_docs: list[dict[str, Any]] | None = None
            if yaml is not None:
                try:
                    parsed_docs = [
                        doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)
                    ]
                except Exception:
                    parsed_docs = None
            if parsed_docs is not None:
                for doc in parsed_docs:
                    if str(doc.get("apiVersion") or "") == "ae.dev/v1alpha1":
                        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
                        ingress = (
                            spec.get("ingress")
                            if isinstance(spec.get("ingress"), dict)
                            else {}
                        )
                        add_host(ingress.get("host"))
                    if str(doc.get("kind") or "") == "Ingress":
                        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
                        for rule in spec.get("rules") or []:
                            if isinstance(rule, dict):
                                add_host(rule.get("host"))
                        for tls in spec.get("tls") or []:
                            if not isinstance(tls, dict):
                                continue
                            for host in tls.get("hosts") or []:
                                add_host(host)
                continue
            for match in re.finditer(r"(?m)^\s*-?\s*host:\s*[\"']?([^\"'\s#]+)", text):
                add_host(match.group(1))
        return urls

    def _stack_requires_ingress_restart(self, info: StackInfo) -> bool:
        if not self.ingress:
            return False
        expected = self._stack_ingress_urls(
            controller_port=info.controller_port,
            apishim_port=info.apishim_port,
        )
        if not expected:
            return False
        return (
            info.dashboard_url != expected.get("dashboard")
            or info.ingress_urls.get("api") != expected.get("api")
        )

    def _refresh_stack_ingress_info(self, info: StackInfo) -> StackInfo:
        if not self.ingress:
            return info
        previous_urls = dict(info.ingress_urls)
        previous_dashboard = info.dashboard_url
        previous_ingress = dict(info.ingress)
        ingress_urls = self._write_stack_ingress_sites(
            controller_port=info.controller_port,
            apishim_port=info.apishim_port,
        )
        if not ingress_urls:
            return info
        info.ingress = self.ingress.public_dict()
        info.ingress_urls = ingress_urls
        info.dashboard_url = ingress_urls.get("dashboard") or info.dashboard_url
        if (
            previous_urls != info.ingress_urls
            or previous_dashboard != info.dashboard_url
            or previous_ingress != info.ingress
        ):
            self._write_stack(info)
        self._reload_ingress()
        return info

    def _write_stack_ingress_sites(
        self,
        *,
        controller_port: int,
        apishim_port: int,
    ) -> dict[str, str]:
        if not self.ingress:
            return {}
        urls = self._stack_ingress_urls(
            controller_port=controller_port,
            apishim_port=apishim_port,
        )
        controller_host = self.ingress.host("k1s")
        legacy_dash_host = self.ingress.host("k1s-dash")
        api_host = self.ingress.host("k1s-api")
        site = self.ingress.sites_dir / "k1s-stack.caddy"
        site.parent.mkdir(parents=True, exist_ok=True)
        asset_proxy = ""
        if self.ingress.dashboard_port:
            asset_proxy = f"""
    handle /static/dash-assets/* {{
        reverse_proxy {self.ingress.host_alias}:{self.ingress.dashboard_port}
    }}
"""
        content = f"""# Generated by WorkerBee stack supervisor.
https://{controller_host}, https://{legacy_dash_host} {{
    header -Strict-Transport-Security
    tls internal
{asset_proxy.rstrip()}
    handle /api/v1* {{
        reverse_proxy https://{self.ingress.host_alias}:{apishim_port} {{
            transport http {{
                tls_insecure_skip_verify
            }}
        }}
    }}
    handle {{
        reverse_proxy {self.ingress.host_alias}:{controller_port}
    }}
}}

https://{api_host} {{
    header -Strict-Transport-Security
    tls internal
    reverse_proxy https://{self.ingress.host_alias}:{apishim_port} {{
        transport http {{
            tls_insecure_skip_verify
        }}
    }}
}}
"""
        if not site.is_file() or site.read_text(encoding="utf-8") != content:
            site.write_text(content, encoding="utf-8")
        return urls

    def _stack_ingress_urls(
        self,
        *,
        controller_port: int,
        apishim_port: int,
    ) -> dict[str, str]:
        _ = (controller_port, apishim_port)
        if not self.ingress:
            return {}
        controller_host = self.ingress.host("k1s")
        legacy_dash_host = self.ingress.host("k1s-dash")
        api_host = self.ingress.host("k1s-api")
        return {
            "controller": self.ingress.url(controller_host, "/"),
            "dashboard": self.ingress.url(controller_host, "/dashboard"),
            "docs": self.ingress.url(controller_host, "/docs"),
            "swagger": self.ingress.url(controller_host, "/swagger"),
            "redoc": self.ingress.url(controller_host, "/redoc"),
            "controller_openapi": self.ingress.url(controller_host, "/openapi.json"),
            "controller_health": self.ingress.url(controller_host, "/health"),
            "apishim": self.ingress.url(api_host, "/"),
            "api": self.ingress.url(api_host, "/"),
            "api_healthz": self.ingress.url(api_host, "/healthz"),
            "api_openapi_v2": self.ingress.url(api_host, "/openapi/v2"),
            "api_openapi_v3": self.ingress.url(api_host, "/openapi/v3"),
            "api_swagger_json": self.ingress.url(api_host, "/swagger.json"),
            "legacy_dashboard": self.ingress.url(legacy_dash_host, "/dashboard"),
        }

    def _reload_ingress(self) -> dict[str, Any]:
        if not self.ingress:
            return {"ok": False, "enabled": False, "reason": "ingress is not configured"}
        proc = subprocess.run(
            runtime_command_args(
                self._resolve_runtime(),
                state_root=self.state_dir.parent.parent,
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
        )
        return {
            "ok": proc.returncode == 0,
            "enabled": True,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
        }

    def _remove_stack_ingress_site(self) -> None:
        if not self.ingress:
            return
        with suppress(OSError):
            (self.ingress.sites_dir / "k1s-stack.caddy").unlink()

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
        _set_cli_http_timeout(env, timeout)
        cmd_args = _normalize_cli_option_args(args)
        return self._run_ae_command(cmd_args, env=env, timeout=timeout)

    def run_ae_cli(
        self,
        args: list[str],
        *,
        timeout: int = 60,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        env = self.k1s_runtime.apply_env(os.environ.copy())
        if env_overrides:
            env.update(env_overrides)
        _set_cli_http_timeout(env, timeout)
        cmd_args = _normalize_cli_option_args(args)
        return self._run_ae_command(cmd_args, env=env, timeout=timeout)

    def _run_ae_command(
        self,
        cmd_args: list[str],
        *,
        env: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        attempts = _remote_apply_retry_attempts(cmd_args, timeout)
        result: dict[str, Any] = {}
        for attempt in range(1, attempts + 1):
            proc = subprocess.run(
                [self.python_executable, "-m", "ae.cli", *cmd_args],
                cwd=self.cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            result = {
                "cmd": _mask_sensitive_args([self.python_executable, "-m", "ae.cli", *cmd_args]),
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
            if attempt > 1:
                result["attempts"] = attempt
                result["retried"] = True
            if proc.returncode == 0:
                return result
            if attempt < attempts and _retryable_remote_apply_timeout(cmd_args, result):
                time.sleep(_env_float("WORKERBEE_AE_APPLY_RETRY_DELAY", 3.0))
                continue
            raise RuntimeError(json.dumps(result, indent=2))
        raise RuntimeError(json.dumps(result, indent=2))

    def load_stack(self) -> StackInfo | None:
        try:
            if not self.stack_file.exists():
                return None
            data = json.loads(self.stack_file.read_text(encoding="utf-8"))
            data.setdefault("k1s_runtime_source", "unknown")
            data.setdefault("python_executable", "")
            data.setdefault("ae_origin", None)
            data.setdefault("ingress", {"enabled": False})
            data.setdefault("ingress_urls", {})
            return StackInfo(**data)
        except Exception:
            return None

    # Internal ------------------------------------------------------
    def _ensure_dirs(self) -> None:
        for rel in ("logs", "specs", "pids", "artifacts", "caddy"):
            (self.state_dir / rel).mkdir(parents=True, exist_ok=True)

    def _write_stack(self, info: StackInfo) -> None:
        self._ensure_dirs()
        write_private_json(self.stack_file, asdict(info))

    def _resolve_runtime(self) -> str:
        return resolve_runtime(self.runtime_requested)

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
        elif runtime == CONTAINERD_RUNTIME:
            state_root = self.state_dir.parent.parent
            exists = subprocess.run(
                runtime_command_args(
                    runtime,
                    state_root=state_root,
                    project=self.project,
                    args=["network", "inspect", network],
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if exists.returncode != 0:
                args = [
                    "network",
                    "create",
                    "--subnet",
                    containerd_network_subnet(state_root, self.project),
                    network,
                ]
                subprocess.run(
                    runtime_command_args(
                        runtime,
                        state_root=state_root,
                        project=self.project,
                        args=args,
                    ),
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

    def _allocate_poc_service_ports(self, runtime: str) -> dict[str, int]:
        start = 22000
        end = 29999
        base = _poc_service_port_base(
            state_root=self.state_dir.parent.parent,
            project=self.project,
            start=start,
            end=end,
        )
        blocked = self._runtime_published_host_ports(runtime)
        reserved: set[int] = set()
        return {
            "store": choose_port(
                base,
                start=start,
                end=end,
                reserved=reserved,
                blocked=blocked,
            ),
            "api": choose_port(
                base + 1,
                start=start,
                end=end,
                reserved=reserved,
                blocked=blocked,
            ),
            "frontend": choose_port(
                base + 2,
                start=start,
                end=end,
                reserved=reserved,
                blocked=blocked,
            ),
        }

    def _runtime_published_host_ports(self, runtime: str) -> set[int]:
        if runtime == "podman":
            cmd = ["podman", "ps", "-a", "--format", "{{.Ports}}"]
        elif runtime == "docker":
            cmd = ["docker", "ps", "-a", "--format", "{{.Ports}}"]
        elif runtime == CONTAINERD_RUNTIME:
            cmd = runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=self.state_dir.parent.parent,
                project=self.project,
                args=["ps", "-a", "--format", "{{.Ports}}"],
            )
        else:
            return set()
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
        except Exception:
            return set()
        if proc.returncode != 0:
            return set()
        return _parse_published_host_ports(proc.stdout)

    def _base_env(self, info: StackInfo) -> dict[str, str]:
        env = self.k1s_runtime.apply_env(os.environ.copy())
        state_root = self.state_dir.parent.parent
        container_cli = info.runtime
        nerdctl_cli = nerdctl_binary()
        if info.runtime == CONTAINERD_RUNTIME:
            nerdctl_cli = os.getenv("WORKERBEE_NERDCTL_BIN") or nerdctl_cli
            container_cli = str(
                write_containerd_cli_wrapper(
                    self.state_dir / "bin" / "nerdctl-workerbee",
                    state_root=state_root,
                    project=self.project,
                    system_container=self.ingress.caddy_container if self.ingress else None,
                )
            )
        env.update(
            {
                "AE_RUNTIME_BACKEND": info.runtime,
                "AE_CONTAINER_CLI": container_cli,
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
                "AE_APISHIM_PUBLIC_BASE": self._public_apishim_base(info),
                "AE_APISHIM_TLS_CERT": str(self.state_dir / "apishim.crt"),
                "AE_APISHIM_TLS_KEY": str(self.state_dir / "apishim.key"),
                "AE_APISHIM_CA_BUNDLE": str(self.state_dir / "apishim.ca.crt"),
                "AE_APISHIM_SESSION_SECRET": _stable_secret(self.state_dir / "session.secret"),
                "AE_DASHBOARD_BOOTSTRAP_TOKEN": info.admin_token,
            }
        )
        env.update(secret_env_for_project(self.state_dir))
        if info.runtime == CONTAINERD_RUNTIME:
            env.update(
                {
                    "AE_CONTAINERD_ADDRESS": containerd_address(),
                    "AE_CRI_ENDPOINT": containerd_address(),
                    "AE_NERDCTL_BIN": nerdctl_cli,
                    "AE_CONTAINERD_NAMESPACE": containerd_namespace(state_root, self.project),
                    "AE_CONTAINERD_DATA_ROOT": str(
                        containerd_data_root(state_root, project=self.project)
                    ),
                    "AE_CONTAINERD_NETWORK": info.network,
                    "AE_CONTAINERD_NETWORK_SUBNET": containerd_network_subnet(
                        state_root,
                        self.project,
                    ),
                    "AE_CONTAINERD_CNI_BIN_DIR": containerd_cni_bin_dir(),
                    "AE_CONTAINERD_CNI_CONF_DIR": str(
                        containerd_cni_conf_dir(state_root, project=self.project)
                    ),
                    "NETCONFPATH": str(
                        containerd_cni_conf_dir(state_root, project=self.project)
                    ),
                }
            )
        if self.ingress:
            env.update(
                {
                    "AE_CADDY_SITES": str(self.ingress.sites_dir),
                    "AE_CADDY_CONTAINER": self.ingress.caddy_container,
                    "AE_CADDY_FILE": self.ingress.caddy_file,
                    "AE_CADDY_HOST_ALIAS": self.ingress.host_alias,
                    "AE_CADDY_PREFER_HOST_PORT_UPSTREAMS": "1",
                    "AE_CADDY_RELOAD_TIMEOUT": "10",
                }
            )
        if info.runtime == "podman":
            env["AE_PODMAN_NETWORK"] = info.network
        elif info.runtime == "docker":
            env["AE_DOCKER_NETWORK"] = info.network
        return env

    def _public_apishim_base(self, info: StackInfo) -> str:
        if self.ingress:
            urls = info.ingress_urls or self._stack_ingress_urls(
                controller_port=info.controller_port,
                apishim_port=info.apishim_port,
            )
            public = urls.get("controller") or urls.get("dashboard") or urls.get("api")
            if public:
                return public.rstrip("/")
        return info.apishim_url.rstrip("/")

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
        for spec_dir in (self.state_dir / "specs", self.state_dir / "artifacts" / "poc-specs"):
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
        if info.runtime == "podman":
            self._cleanup_project_runtime_containers(info.runtime, include_namespaces=True)
            if purge:
                subprocess.run(
                    ["podman", "network", "rm", info.network],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return
        if info.runtime == CONTAINERD_RUNTIME:
            self._cleanup_containerd_runtime(network=info.network, purge=purge)
            return

        self._cleanup_project_runtime_containers(info.runtime, include_namespaces=True)
        if purge:
            subprocess.run(
                ["docker", "network", "rm", info.network],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _cleanup_project_runtime_containers(
        self,
        runtime: str,
        *,
        include_namespaces: bool,
    ) -> None:
        if runtime not in {"docker", "podman"}:
            return
        ids: list[str] = []
        seen: set[str] = set()
        for label_filter in _project_cleanup_label_filters(
            self.project,
            include_namespaces=include_namespaces,
        ):
            found = _split_lines(
                subprocess.run(
                    [runtime, "ps", "-aq", "--filter", f"label={label_filter}"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                ).stdout
            )
            for item in found:
                if item not in seen:
                    seen.add(item)
                    ids.append(item)
        if ids:
            subprocess.run(
                [runtime, "rm", "-f", *ids],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _cleanup_containerd_runtime(self, *, network: str, purge: bool) -> None:
        state_root = self.state_dir.parent.parent
        ids = _split_lines(
            subprocess.run(
                runtime_command_args(
                    CONTAINERD_RUNTIME,
                    state_root=state_root,
                    project=self.project,
                    args=["ps", "-aq"],
                    ensure_dirs=not purge,
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ).stdout
        )
        if ids:
            rm_cmd = runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=state_root,
                project=self.project,
                args=["rm", "-f", *ids],
                ensure_dirs=not purge,
            )
            proc = subprocess.run(
                rm_cmd,
                check=False,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if proc.returncode != 0 and "no such network" in (proc.stderr or ""):
                self._ensure_containerd_cleanup_network(network)
                subprocess.run(
                    rm_cmd,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        if not purge:
            return

        images = [
            image
            for image in _split_lines(
                subprocess.run(
                    runtime_command_args(
                        CONTAINERD_RUNTIME,
                        state_root=state_root,
                        project=self.project,
                        args=["images", "--format", "{{.Repository}}:{{.Tag}}"],
                        ensure_dirs=False,
                    ),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                ).stdout
            )
            if image and not image.startswith("<none>")
        ]
        if images:
            subprocess.run(
                runtime_command_args(
                    CONTAINERD_RUNTIME,
                    state_root=state_root,
                    project=self.project,
                    args=["rmi", "-f", *images],
                    ensure_dirs=False,
                ),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=state_root,
                project=self.project,
                args=["network", "rm", network],
                ensure_dirs=False,
            ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            runtime_command_args(
                CONTAINERD_RUNTIME,
                state_root=state_root,
                project=self.project,
                args=[
                    "namespace",
                    "remove",
                    containerd_namespace(state_root, self.project),
                ],
                ensure_dirs=False,
            ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _ensure_containerd_cleanup_network(self, network: str) -> None:
        state_root = self.state_dir.parent.parent
        base = {
            "runtime": CONTAINERD_RUNTIME,
            "state_root": state_root,
            "project": self.project,
            "ensure_dirs": False,
        }
        exists = subprocess.run(
            runtime_command_args(args=["network", "inspect", network], **base),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if exists.returncode == 0:
            return
        create = subprocess.run(
            runtime_command_args(
                args=[
                    "network",
                    "create",
                    "--subnet",
                    containerd_network_subnet(state_root, self.project),
                    network,
                ],
                **base,
            ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if create.returncode != 0:
            subprocess.run(
                runtime_command_args(args=["network", "create", network], **base),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _purge_state_dir(self, info: StackInfo | None) -> dict[str, Any]:
        runtime = info.runtime if info else self._resolve_runtime()
        if runtime == CONTAINERD_RUNTIME:
            result = remove_containerd_helper_tree(
                self.state_dir.parent.parent,
                self.state_dir,
            )
            if result.get("ok"):
                return result
            try:
                shutil.rmtree(self.state_dir)
                return {
                    "ok": True,
                    "removed": True,
                    "path": str(self.state_dir),
                    "fallback": "shutil",
                    "helper": result,
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "removed": False,
                    "path": str(self.state_dir),
                    "helper": result,
                    "error": str(exc),
                }
        shutil.rmtree(self.state_dir)
        return {"ok": True, "removed": True, "path": str(self.state_dir)}

    def _runtime_container_ids(
        self,
        info: StackInfo,
        *,
        app: str,
        namespace: str,
        all_containers: bool = False,
    ) -> list[str]:
        filters = [
            "--filter",
            f"label=ae.namespace={namespace}",
            "--filter",
            f"label=app={app}",
        ]
        ps_args = ["ps", "-aq" if all_containers else "-q", *filters]
        if info.runtime == "podman":
            cmd = ["podman", *ps_args]
        elif info.runtime == CONTAINERD_RUNTIME:
            cmd = runtime_command_args(
                info.runtime,
                state_root=self.state_dir.parent.parent,
                project=self.project,
                args=ps_args,
            )
        else:
            cmd = ["docker", *ps_args]
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"{info.runtime} ps failed")
        return _split_lines(proc.stdout)

    def _runtime_logs(
        self,
        info: StackInfo,
        *,
        app: str,
        namespace: str,
        tail: int,
        include_exited: bool,
    ) -> dict[str, Any]:
        ids = self._runtime_container_ids(info, app=app, namespace=namespace)
        container_state = "running"
        if not ids:
            if include_exited:
                ids = self._runtime_container_ids(
                    info,
                    app=app,
                    namespace=namespace,
                    all_containers=True,
                )
                container_state = "exited"
            if not ids:
                qualifier = "running or exited" if include_exited else "running"
                raise RuntimeError(f"no {qualifier} container found for app {namespace}/{app}")
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        for cid in ids:
            cmd = runtime_command_args(
                info.runtime,
                state_root=self.state_dir.parent.parent,
                project=self.project,
                args=["logs", "--tail", str(tail), cid],
            )
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
            "container_state": container_state,
            "resolved_namespace": namespace,
            "resolved_app": app,
            "cmd": runtime_command_args(
                info.runtime,
                state_root=self.state_dir.parent.parent,
                project=self.project,
                args=["logs", "--tail", str(tail), *ids],
            ),
            "returncode": 0,
            "stdout": "".join(stdout_parts),
            "stderr": "".join(stderr_parts),
        }

    def _runtime_exec(
        self,
        info: StackInfo,
        *,
        app: str,
        namespace: str,
        command: list[str],
    ) -> dict[str, Any]:
        ids = self._runtime_container_ids(info, app=app, namespace=namespace)
        if not ids:
            raise RuntimeError(f"no running container found for app {namespace}/{app}")
        cmd = runtime_command_args(
            info.runtime,
            state_root=self.state_dir.parent.parent,
            project=self.project,
            args=["exec", ids[0], *command],
        )
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=45,
        )
        result = {
            "source": info.runtime,
            "resolved_namespace": namespace,
            "resolved_app": app,
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        if proc.returncode != 0:
            stderr = " ".join(proc.stderr.split())
            stdout = " ".join(proc.stdout.split())
            detail = stderr or stdout
            suffix = f": {detail[:220]}" if detail else ""
            raise WorkerBeeError(
                code="EXEC_COMMAND_FAILED",
                message=(
                    f"command failed in {namespace}/{app} with exit code "
                    f"{proc.returncode}{suffix}"
                ),
                details=result,
                remediation=(
                    "Inspect command stderr/stdout, fix the command or workload, and retry."
                ),
            )
        return result

    def _validate_poc_containerd(
        self,
        info: StackInfo,
        *,
        timeout_seconds: float = 90.0,
    ) -> dict[str, Any]:
        return self._validate_poc_runtime(info, timeout_seconds=timeout_seconds)

    def _validate_poc_runtime(
        self,
        info: StackInfo,
        *,
        urls: dict[str, str] | None = None,
        timeout_seconds: float = 90.0,
    ) -> dict[str, Any]:
        checks = {
            "store": ("store", "/healthz", "json"),
            "api": ("api", "/healthz", "json"),
            "frontend_health": ("frontend", "/healthz", "json"),
            "api_check": ("api", "/api/check", "json"),
            "frontend": ("frontend", "/", "text"),
        }
        deadline = time.monotonic() + timeout_seconds
        last_errors: dict[str, str] = {}
        while time.monotonic() < deadline:
            result: dict[str, Any] = {"mode": f"{info.runtime}-exec"}
            ok = True
            for name, (app, path, output) in checks.items():
                try:
                    probe = self._runtime_http_probe(info, app=app, path=path)
                except Exception as exc:  # noqa: BLE001
                    last_errors[name] = str(exc)
                    ok = False
                    continue
                status = int(probe.get("status") or 0)
                if status != 200:
                    last_errors[name] = f"status {status}: {probe.get('body', '')[:300]}"
                    ok = False
                    continue
                result[name] = (
                    probe.get("json", {"body": probe.get("body", "")})
                    if output == "json"
                    else str(probe.get("body", ""))[:300]
                )
            if ok:
                result["ok"] = True
                if urls:
                    result["host_urls"] = self._best_effort_host_url_validation(urls)
                return result
            time.sleep(1.0)
        raise TimeoutError(
            f"{info.runtime} POC containers did not become ready: "
            f"{json.dumps(last_errors, indent=2)}"
        )

    def _containerd_http_probe(
        self,
        info: StackInfo,
        *,
        app: str,
        path: str,
    ) -> dict[str, Any]:
        return self._runtime_http_probe(info, app=app, path=path)

    def _runtime_http_probe(
        self,
        info: StackInfo,
        *,
        app: str,
        path: str,
        namespace: str = POC_NAMESPACE,
    ) -> dict[str, Any]:
        code = (
            "import json, sys, urllib.error, urllib.request\n"
            "url = sys.argv[1]\n"
            "try:\n"
            "    try:\n"
            "        with urllib.request.urlopen(url, timeout=4.0) as resp:\n"
            "            status = int(resp.status)\n"
            "            body = resp.read()\n"
            "    except urllib.error.HTTPError as exc:\n"
            "        status = int(exc.code)\n"
            "        body = exc.read()\n"
            "    text = body.decode('utf-8', errors='replace')\n"
            "    payload = {'status': status, 'body': text[:1000]}\n"
            "    try:\n"
            "        payload['json'] = json.loads(text)\n"
            "    except Exception:\n"
            "        pass\n"
            "    print(json.dumps(payload, sort_keys=True))\n"
            "except Exception as exc:\n"
            "    print(json.dumps({'error': str(exc)}, sort_keys=True))\n"
            "    raise SystemExit(1)\n"
        )
        exec_result = self._runtime_exec(
            info,
            app=app,
            namespace=namespace,
            command=["python", "-c", code, f"http://127.0.0.1:8080{path}"],
        )
        stdout = exec_result["stdout"].strip()
        if not stdout:
            raise RuntimeError(f"empty probe output for app {app!r}")
        probe = json.loads(stdout.splitlines()[-1])
        if probe.get("error"):
            raise RuntimeError(str(probe["error"]))
        return probe

    def _best_effort_host_url_validation(self, urls: dict[str, str]) -> dict[str, Any]:
        try:
            result = validate_poc_urls(urls, timeout_seconds=5.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "urls": urls}
        return {"ok": True, "result": result, "urls": urls}


def _slug(value: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    out = "-".join(part for part in out.split("-") if part)
    return out or "default"


def _poc_service_port_base(*, state_root: Path, project: str, start: int, end: int) -> int:
    span = max(1, int(end) - int(start) - 2)
    slots = max(1, span // 10)
    digest = hashlib.blake2s(
        f"{state_root.resolve()}:{project}".encode(),
        digest_size=4,
    ).hexdigest()
    return int(start) + (int(digest, 16) % slots) * 10


def _parse_published_host_ports(text: str) -> set[int]:
    ports: set[int] = set()
    for match in re.finditer(r"(?:^|[\s,])(?:[^,\s]*:)?(\d+)->\d+/(?:tcp|udp)", text):
        with suppress(ValueError):
            ports.add(int(match.group(1)))
    return ports


def project_slug(value: str) -> str:
    return _slug(value)


def _resolve_app_ref(
    app: str,
    *,
    namespace: str | None,
    default_namespace: str,
) -> tuple[str, str]:
    raw_app = str(app or "").strip()
    raw_namespace = str(namespace or "").strip()
    if not raw_app:
        raise ValueError("app is required")
    if "/" in raw_app:
        parts = raw_app.split("/")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ValueError("app must be `name` or `namespace/name`")
        app_namespace = parts[0].strip()
        app_name = parts[1].strip()
        if raw_namespace and raw_namespace != app_namespace:
            raise ValueError(
                f"namespace mismatch: app references {app_namespace!r} but namespace is "
                f"{raw_namespace!r}"
            )
        return app_namespace, app_name
    resolved_namespace = raw_namespace or default_namespace
    if not resolved_namespace:
        raise ValueError("namespace is required")
    return resolved_namespace, raw_app


def _set_cli_http_timeout(env: dict[str, str], timeout: int) -> None:
    env["AE_CLI_HTTP_TIMEOUT"] = str(_bounded_cli_http_timeout(timeout))


def _bounded_cli_http_timeout(timeout: int) -> int:
    return max(10, min(int(timeout), 600))


def _remote_apply_retry_attempts(args: list[str], timeout: int) -> int:
    if timeout <= 10 or not _is_remote_apply(args):
        return 1
    return min(4, max(2, int(timeout) // 30 + 1))


def _is_remote_apply(args: list[str]) -> bool:
    return "apply" in args and "--server" in args


def _retryable_remote_apply_timeout(args: list[str], result: dict[str, Any]) -> bool:
    if not _is_remote_apply(args):
        return False
    combined = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()
    return "read timed out" in combined or "read timeout" in combined


def _project_cleanup_label_filters(project: str, *, include_namespaces: bool) -> list[str]:
    filters = [
        f"workerbee.project={project}",
        f"workerbee.k1s.dev/project={project}",
    ]
    if include_namespaces:
        filters.extend(
            [
                f"ae.namespace={POC_NAMESPACE}",
                f"ae.namespace={project}",
            ]
        )
    return list(dict.fromkeys(filters))


def _remote_apply_stdout(data: dict[str, Any]) -> str:
    if str(data.get("status", "")).lower() == "accepted":
        return (
            f"applied desired state for {data.get('app')} "
            f"resourceVersion={data.get('resourceVersion')}\n"
        )
    return (
        f"applied {data.get('app')} rev={data.get('revision')}({data.get('status')}) "
        f"ops=+{data.get('created')}/~{data.get('updated')}/-{data.get('removed')}\n"
    )


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
    return _safe_cli_token(32)


def _stable_secret(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = _safe_cli_token(48)
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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _safe_cli_token(nbytes: int) -> str:
    value = secrets.token_urlsafe(nbytes)
    if value and (value[0].isalnum()):
        return value
    return f"t{value}"


def _normalize_cli_option_args(args: list[str]) -> list[str]:
    normalized: list[str] = []
    idx = 0
    while idx < len(args):
        value = args[idx]
        if value in {"--token", "--password"} and idx + 1 < len(args):
            normalized.append(f"{value}={args[idx + 1]}")
            idx += 2
            continue
        normalized.append(value)
        idx += 1
    return normalized


def _mask_sensitive_args(args: list[str]) -> list[str]:
    masked = list(args)
    for idx, value in enumerate(masked[:-1]):
        if value in {"--token", "--password"}:
            masked[idx + 1] = "***"
    for idx, value in enumerate(masked):
        for option in ("--token=", "--password="):
            if value.startswith(option):
                masked[idx] = f"{option}***"
    return masked
