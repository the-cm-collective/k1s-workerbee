from pathlib import Path
from typing import Any

from workerbee import poc
from workerbee.poc import POC_NAMESPACE, validate_poc_urls, write_stack_files


def test_write_stack_files_contains_representative_features(tmp_path: Path) -> None:
    artifacts = write_stack_files(
        state_dir=tmp_path,
        project="demo",
        image_tags={
            "store": "store:test",
            "api": "api:test",
            "frontend": "frontend:test",
        },
        service_ports={"store": 19080, "api": 19081, "frontend": 19082},
    )

    text = "\n".join(path.read_text(encoding="utf-8") for path in artifacts.manifests)
    assert f"namespace: {POC_NAMESPACE}" in text
    assert "configRefs:" in text
    assert "secretRefs:" in text
    assert "host.containers.internal" in text
    assert "AE_CONFIG_ROOT" in text
    assert "file: mode.txt" in text
    assert "file: token" in text
    assert "emptyDirs:" in text
    assert "storage:" in text
    assert "readiness:" in text
    assert "ingress:" in text
    assert artifacts.urls["frontend"] == "http://127.0.0.1:19082"


def test_write_stack_files_can_scope_ingress_hosts(tmp_path: Path) -> None:
    artifacts = write_stack_files(
        state_dir=tmp_path,
        project="alpha",
        image_tags={
            "store": "store:test",
            "api": "api:test",
            "frontend": "frontend:test",
        },
        service_ports={"store": 19080, "api": 19081, "frontend": 19082},
        ingress_domain="alpha.workerbee.localhost",
    )

    text = "\n".join(path.read_text(encoding="utf-8") for path in artifacts.manifests)
    assert "host: api.alpha.workerbee.localhost" in text
    assert "host: app.alpha.workerbee.localhost" in text


def test_containerd_poc_images_use_localhost_registry() -> None:
    assert poc._poc_image_tag(runtime="containerd", name="api", project="demo") == (  # noqa: SLF001
        "localhost/workerbee-poc-api:demo"
    )
    assert poc._poc_image_tag(runtime="podman", name="api", project="demo") == (  # noqa: SLF001
        "workerbee-poc-api:demo"
    )


def test_validate_poc_urls_returns_top_level_ok(monkeypatch) -> None:
    class Response:
        def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
            self.status = status
            self.payload = payload or {"healthy": True}
            self.text = "<h1>WorkerBee POC</h1>"

        def json(self) -> dict[str, Any]:
            return self.payload

    def fake_request(url: str, **_: Any) -> Response:
        if url.endswith("/api/check"):
            return Response(200, {"ok": True})
        return Response(200)

    monkeypatch.setattr("workerbee.poc.request", fake_request)

    result = validate_poc_urls(
        {"store": "http://store", "api": "http://api", "frontend": "http://frontend"},
        timeout_seconds=1,
    )

    assert result["ok"] is True
    assert result["api_check"] == {"ok": True}
