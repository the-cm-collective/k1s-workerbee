from pathlib import Path

from workerbee.poc import POC_NAMESPACE, write_stack_files


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
