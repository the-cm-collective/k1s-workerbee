from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/qualify_installed_consumer.py"
DOC = ROOT / "docs/k1s-consumer-contract.md"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qualify_installed_consumer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


qualification = _load_module()


def test_clean_environment_is_allowlisted_and_forces_offline_install(tmp_path: Path) -> None:
    environment = qualification._clean_environment(
        tmp_path / "venv", tmp_path / "home", tmp_path / "tmp"
    )

    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PIP_CONFIG_FILE"] == os.devnull
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PATH"].startswith(str(tmp_path / "venv" / "bin"))
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert not any("TOKEN" in key or "SECRET" in key for key in environment)


def test_artifact_validation_binds_name_size_hash_and_wheelhouse(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    artifact = wheelhouse / qualification.WORKERBEE_WHEEL_NAME
    artifact.write_bytes(b"exact-wheel")
    digest = hashlib.sha256(b"exact-wheel").hexdigest()
    source = "1" * 40
    tree = "2" * 40

    identity = qualification._validate_artifact(
        artifact,
        wheelhouse=wheelhouse.resolve(),
        expected_name=artifact.name,
        expected_size=artifact.stat().st_size,
        expected_sha256=digest,
        source_commit=source,
        source_tree=tree,
    )

    assert identity.public() == {
        "filename": artifact.name,
        "sha256": digest,
        "size": len(b"exact-wheel"),
        "source_commit": source,
        "source_tree": tree,
    }
    with pytest.raises(qualification.QualificationError, match="artifact-sha256-mismatch"):
        qualification._validate_artifact(
            artifact,
            wheelhouse=wheelhouse.resolve(),
            expected_name=artifact.name,
            expected_size=artifact.stat().st_size,
            expected_sha256="0" * 64,
            source_commit=source,
            source_tree=tree,
        )


def test_artifact_validation_rejects_symlink_and_outside_wheelhouse(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    outside = tmp_path / qualification.WORKERBEE_WHEEL_NAME
    outside.write_bytes(b"wheel")
    digest = hashlib.sha256(b"wheel").hexdigest()
    linked = wheelhouse / qualification.WORKERBEE_WHEEL_NAME
    linked.symlink_to(outside)
    common = {
        "wheelhouse": wheelhouse.resolve(),
        "expected_name": qualification.WORKERBEE_WHEEL_NAME,
        "expected_size": len(b"wheel"),
        "expected_sha256": digest,
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
    }

    with pytest.raises(qualification.QualificationError, match="artifact-not-regular"):
        qualification._validate_artifact(linked, **common)
    with pytest.raises(qualification.QualificationError, match="artifact-outside-wheelhouse"):
        qualification._validate_artifact(outside, **common)


def test_wheelhouse_identity_seals_complete_set_and_rejects_legacy_provider(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / qualification.K1S_WHEEL_NAME).write_bytes(b"k1s")
    (wheelhouse / qualification.WORKERBEE_WHEEL_NAME).write_bytes(b"workerbee")

    first = qualification._wheelhouse_identity(wheelhouse)
    second = qualification._wheelhouse_identity(wheelhouse)

    assert first == second
    assert first.wheel_count == 2
    assert len(first.complete_set_sha256) == 64
    (wheelhouse / "k1s_workerbee_runtime-0.1.7-py3-none-any.whl").write_bytes(b"legacy")
    with pytest.raises(qualification.QualificationError, match="wheelhouse-legacy-provider"):
        qualification._wheelhouse_identity(wheelhouse)


def test_run_root_is_new_private_and_cannot_overlap_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_root = qualification._prepare_run_root(tmp_path / "evidence", (source.resolve(),))

    assert run_root.is_dir()
    assert run_root.stat().st_mode & 0o777 == 0o700
    assert run_root.stat().st_uid == os.getuid()
    with pytest.raises(qualification.QualificationError, match="run-root-overlaps-source"):
        qualification._prepare_run_root(source / "evidence", (source.resolve(),))


def test_command_summaries_require_read_only_and_same_runtime() -> None:
    direct = qualification._command_summary(
        {"apiVersion": "k1s.dev/v1alpha1", "kind": "ProfileCatalog", "mutating": False},
        operation="list",
        workerbee=False,
    )
    consumer = qualification._command_summary(
        {
            "evidence": {"kind": "WorkerBeeProfileEvidence", "operation": "list"},
            "diagnostics": {
                "provider_distribution": "k1s-core",
                "provider_version": "1.0.0",
                "same_runtime": True,
            },
        },
        operation="list",
        workerbee=True,
    )

    assert direct["mutating"] is False
    assert consumer["same_runtime"] is True
    with pytest.raises(qualification.QualificationError, match="k1s-list-not-read-only"):
        qualification._command_summary({"mutating": True}, operation="list", workerbee=False)
    with pytest.raises(qualification.QualificationError, match="workerbee-list-runtime-mismatch"):
        qualification._command_summary(
            {
                "evidence": {"operation": "list"},
                "diagnostics": {
                    "provider_distribution": "k1s-core",
                    "provider_version": "1.0.0",
                    "same_runtime": False,
                },
            },
            operation="list",
            workerbee=True,
        )


def test_aggregate_requires_two_byte_identical_canonical_receipts() -> None:
    receipt = {
        "schema": qualification.RECEIPT_SCHEMA,
        "status": "qualified",
        "source_isolation": {"source_checkouts_on_sys_path": False},
    }
    aggregate = qualification._aggregate((receipt, dict(reversed(tuple(receipt.items())))))

    assert aggregate["deterministic"] is True
    assert aggregate["runs"] == 2
    assert aggregate["run_receipts"] == ["run-1/receipt.json", "run-2/receipt.json"]
    assert (
        aggregate["receipt_sha256"]
        == hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with pytest.raises(qualification.QualificationError, match="at-least-two-runs-required"):
        qualification._aggregate((receipt,))
    with pytest.raises(qualification.QualificationError, match="qualification-receipts-differ"):
        qualification._aggregate((receipt, {**receipt, "status": "failed"}))


def test_script_has_no_live_or_arbitrary_execution_surface() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "shell=True" not in source
    assert "--command" not in source
    assert "--environment" not in source
    assert 'operations = (("list", None), ("doctor", "core"), ("plan", "core"))' in source
    for forbidden in (
        '"apply"',
        '"deploy"',
        '"profile", "start"',
        '"profile", "stop"',
        "PIP_EXTRA_INDEX_URL",
        "PIP_INDEX_URL",
    ):
        assert forbidden not in source


def test_public_doc_defines_installed_artifact_boundary_without_live_claims() -> None:
    doc = DOC.read_text(encoding="utf-8")
    normalized = " ".join(doc.split())

    for value in (
        "scripts/qualify_installed_consumer.py",
        "PIP_NO_INDEX=1",
        "two fresh virtual environments",
        "neither source checkout appears on `sys.path`",
        "list`, `doctor`, and `plan`",
        "does not start a profile",
    ):
        assert value in normalized
