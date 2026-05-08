"""POC app stack generation and validation."""

from __future__ import annotations

import hashlib
import importlib.resources as resources
import json
import re
import shutil
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workerbee.http import request
from workerbee.runtime_support import (
    CONTAINERD_RUNTIME,
    build_image_with_runtime,
    workerbee_runtime_labels,
)

POC_NAMESPACE = "workerbee-poc"
POC_APPS = ("store", "api", "frontend")


@dataclass(slots=True)
class POCArtifacts:
    manifests: list[Path]
    config_file: Path
    secret_file: Path
    image_tags: dict[str, str]
    urls: dict[str, str]


def _copy_asset_tree(name: str, target: Path) -> None:
    src = resources.files("workerbee.assets").joinpath("poc", name)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        dest = target / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            dest.write_bytes(item.read_bytes())


def build_images(*, runtime: str, state_dir: Path, project: str) -> dict[str, str]:
    build_root = state_dir / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    tags: dict[str, str] = {}
    state_root = state_dir.parent.parent
    for name in POC_APPS:
        ctx = build_root / name
        _copy_asset_tree(name, ctx)
        tag = _poc_image_tag(runtime=runtime, name=name, project=project)
        build_image_with_runtime(
            runtime=runtime,
            state_root=state_root,
            project=project,
            context=ctx,
            tag=tag,
            labels=workerbee_runtime_labels(state_root=state_root, project=project),
        )
        tags[name] = tag
    return tags


def _poc_image_tag(*, runtime: str, name: str, project: str) -> str:
    tag = f"workerbee-poc-{name}:{project}"
    if runtime == CONTAINERD_RUNTIME:
        return f"localhost/{tag}"
    return tag


def write_stack_files(
    *,
    state_dir: Path,
    project: str,
    image_tags: dict[str, str],
    service_ports: dict[str, int],
    runtime: str = "auto",
    ingress_domain: str | None = None,
) -> POCArtifacts:
    spec_dir = state_dir / "artifacts" / "poc-specs"
    data_dir = state_dir / "poc-data"
    spec_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    config_file = data_dir / "api-config.yaml"
    secret_file = data_dir / "api-secret.yaml"
    config_file.write_text("mode: poc\ncolor: amber\nfeature_flag: workerbee\n", encoding="utf-8")
    secret_file.write_text("token: workerbee-poc-token\n", encoding="utf-8")

    urls = {
        "store": f"http://127.0.0.1:{service_ports['store']}",
        "api": f"http://127.0.0.1:{service_ports['api']}",
        "frontend": f"http://127.0.0.1:{service_ports['frontend']}",
    }
    store_urls = ",".join(
        [
            *_runtime_peer_urls(runtime=runtime, app="store"),
            f"http://host.containers.internal:{service_ports['store']}",
            f"http://host.docker.internal:{service_ports['store']}",
        ]
    )
    api_urls = ",".join(
        [
            *_runtime_peer_urls(runtime=runtime, app="api"),
            f"http://host.containers.internal:{service_ports['api']}",
            f"http://host.docker.internal:{service_ports['api']}",
        ]
    )
    api_ingress_host = f"api.{ingress_domain}" if ingress_domain else "api.workerbee.local"
    frontend_ingress_host = (
        f"app.{ingress_domain}" if ingress_domain else "app.workerbee.local"
    )

    manifests: dict[str, str] = {
        "store": f"""
            apiVersion: ae.dev/v1alpha1
            kind: Deployment
            metadata:
              name: store
              namespace: {POC_NAMESPACE}
              labels:
                workerbee.k1s.dev/project: {project}
            spec:
              image: {image_tags["store"]}
              imagePullPolicy: Never
              replicas: 1
              env:
                - name: SEED_KEY
                  value: boot
                - name: SEED_VALUE
                  value: workerbee
              ports:
                - name: http
                  containerPort: 8080
              service:
                port: {service_ports["store"]}
                targetPort: 8080
              health:
                readiness:
                  httpGet: {{ path: /healthz, port: 8080 }}
                  initialDelaySeconds: 1
                  periodSeconds: 2
                liveness:
                  httpGet: {{ path: /healthz, port: 8080 }}
                  initialDelaySeconds: 3
                  periodSeconds: 5
              emptyDirs:
                - name: cache
                  mountPath: /var/cache/workerbee
              storage:
                - name: data
                  mountPath: /data
                  retention: Delete
              resources:
                requests:
                  cpu: 0.05
                  memory: 64Mi
                limits:
                  cpu: 0.25
                  memory: 128Mi
        """,
        "api": f"""
            apiVersion: ae.dev/v1alpha1
            kind: Deployment
            metadata:
              name: api
              namespace: {POC_NAMESPACE}
              labels:
                workerbee.k1s.dev/project: {project}
            spec:
              image: {image_tags["api"]}
              imagePullPolicy: Never
              replicas: 1
              env:
                - name: STORE_URLS
                  value: "{store_urls}"
                - name: APP_NAME
                  value: workerbee-api
                - name: AE_CONFIG_ROOT
                  value: /var/run/ae/config/{POC_NAMESPACE}--api
              ports:
                - name: http
                  containerPort: 8080
              service:
                port: {service_ports["api"]}
                targetPort: 8080
              configRefs:
                - name: api-config
                  path: {config_file}
                  envFrom: true
                  files:
                    - key: mode
                      file: mode.txt
                    - key: color
                      file: color.txt
              secretRefs:
                - name: api-secret
                  path: {secret_file}
                  envFrom: true
                  files:
                    - key: token
                      file: token
              health:
                readiness:
                  httpGet: {{ path: /healthz, port: 8080 }}
                  initialDelaySeconds: 1
                  periodSeconds: 2
                liveness:
                  httpGet: {{ path: /healthz, port: 8080 }}
                  initialDelaySeconds: 3
                  periodSeconds: 5
                startup:
                  httpGet: {{ path: /healthz, port: 8080 }}
                  failureThreshold: 20
                  periodSeconds: 2
              emptyDirs:
                - name: work
                  mountPath: /work
              ingress:
                host: {api_ingress_host}
                path: /
              resources:
                requests:
                  cpu: 0.05
                  memory: 96Mi
                limits:
                  cpu: 0.5
                  memory: 192Mi
              security:
                runAsUser: 1000
                readOnlyRootFilesystem: false
                dropCapabilities: ["NET_RAW"]
                seccompProfileType: RuntimeDefault
              exportHints:
                suppressImageMultiArchWarning: true
        """,
        "frontend": f"""
            apiVersion: ae.dev/v1alpha1
            kind: Deployment
            metadata:
              name: frontend
              namespace: {POC_NAMESPACE}
              labels:
                workerbee.k1s.dev/project: {project}
            spec:
              image: {image_tags["frontend"]}
              imagePullPolicy: Never
              replicas: 1
              env:
                - name: API_URLS
                  value: "{api_urls}"
              ports:
                - name: http
                  containerPort: 8080
              service:
                port: {service_ports["frontend"]}
                targetPort: 8080
              health:
                readiness:
                  httpGet: {{ path: /healthz, port: 8080 }}
                  initialDelaySeconds: 1
                  periodSeconds: 2
                liveness:
                  httpGet: {{ path: /healthz, port: 8080 }}
                  initialDelaySeconds: 3
                  periodSeconds: 5
              ingress:
                host: {frontend_ingress_host}
                paths:
                  - /
                  - /api
              resources:
                requests:
                  cpu: 0.05
                  memory: 64Mi
                limits:
                  cpu: 0.25
                  memory: 128Mi
              exportHints:
                suppressImageMultiArchWarning: true
        """,
    }

    manifest_paths: list[Path] = []
    for name, text in manifests.items():
        path = spec_dir / f"{name}.yaml"
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
        manifest_paths.append(path)
    return POCArtifacts(
        manifests=manifest_paths,
        config_file=config_file,
        secret_file=secret_file,
        image_tags=image_tags,
        urls=urls,
    )


def _runtime_peer_urls(*, runtime: str, app: str) -> list[str]:
    if runtime == CONTAINERD_RUNTIME:
        return [f"http://{_containerd_poc_container_name(app)}:8080"]
    app_key = f"{POC_NAMESPACE}--{app}"
    revision_container = f"ae-{app_key}-rev1-0"
    if runtime == "docker":
        hosts = [revision_container, f"app-{app_key}", f"app-{app_key}-rev1"]
    elif runtime == "podman":
        hosts = [revision_container, f"ae-{app_key}", f"ae-{app_key}-rev1"]
    else:
        hosts = [
            revision_container,
            f"ae-{app_key}",
            f"ae-{app_key}-rev1",
            f"app-{app_key}",
            f"app-{app_key}-rev1",
        ]
    seen: set[str] = set()
    return [f"http://{host}:8080" for host in hosts if not (host in seen or seen.add(host))]


def _containerd_peer_urls(*, runtime: str, app: str) -> list[str]:
    if runtime != CONTAINERD_RUNTIME:
        return []
    return [f"http://{_containerd_poc_container_name(app)}:8080"]


def _containerd_poc_container_name(app: str) -> str:
    raw = f"{POC_NAMESPACE}--{app}-rev1-0"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)
    safe = re.sub(r"[._-]+", "-", safe).strip("-._") or "item"
    if safe == raw:
        return f"ae-{safe}"
    digest = hashlib.blake2s(raw.encode("utf-8"), digest_size=5).hexdigest()
    return f"ae-{safe}-{digest}"


def validate_poc_urls(urls: dict[str, str], *, timeout_seconds: float = 90.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_errors: dict[str, str] = {}
    while time.monotonic() < deadline:
        result: dict[str, Any] = {}
        ok = True
        for name, base in urls.items():
            try:
                resp = request(f"{base}/healthz", timeout=2.0)
                if resp.status != 200:
                    ok = False
                    last_errors[name] = f"health status {resp.status}"
                    continue
                result[name] = resp.json()
            except Exception as exc:  # noqa: BLE001
                ok = False
                last_errors[name] = str(exc)
        if ok:
            api_resp = request(f"{urls['api']}/api/check", timeout=4.0)
            frontend_resp = request(f"{urls['frontend']}/", timeout=4.0)
            if api_resp.status == 200 and frontend_resp.status == 200:
                result["ok"] = True
                result["api_check"] = api_resp.json()
                result["frontend"] = frontend_resp.text[:300]
                return result
            last_errors["api_check"] = f"api={api_resp.status} frontend={frontend_resp.status}"
        time.sleep(1.0)
    raise TimeoutError(f"POC URLs did not become ready: {json.dumps(last_errors, indent=2)}")
