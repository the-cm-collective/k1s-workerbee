from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from workerbee.contract import MCP_TOOL_NAMES, WorkerBeeError
from workerbee.controlled_runs import CONTROLLED_RUN_TOOL_NAMES
from workerbee.readiness import (
    MAX_EVIDENCE_BYTES,
    READINESS_EVIDENCE_SCHEMA,
    READINESS_RECEIPT_SCHEMA,
    read_readiness_receipt,
)
from workerbee.runtime_policy import runtime_state_readback, tool_policy_descriptors


def _evidence(*, now: float = 1000.0, generation: str = "generation-1") -> dict[str, Any]:
    return {
        "api_version": READINESS_EVIDENCE_SCHEMA,
        "generation": generation,
        "observed_at": now,
        "state": "ready",
        "runtime": {"configured": "containerd", "measured": "containerd"},
        "dependencies": [
            {
                "name": "k1s-core",
                "state": "ready",
                "consumers": [{"name": "workerbee", "state": "ready"}],
            }
        ],
        "evidence_refs": [{"kind": "probe", "sha256": "a" * 64}],
        "details": {"summary": "bounded", "api_token": "do-not-leak"},
    }


def _write(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)


def _read(path: Path, **overrides: Any) -> dict[str, Any]:
    return read_readiness_receipt(
        path=path,
        project="demo",
        generation="generation-1",
        evidence_name="readiness",
        required_consumers={"k1s-core": ["workerbee"]},
        configured_runtime="containerd",
        max_age_seconds=60,
        settle_timeout_seconds=0.05,
        settle_poll_seconds=0.001,
        now=lambda: 1000.0,
        **overrides,
    )


def test_readiness_receipt_is_bounded_sanitized_and_provenanced(tmp_path: Path) -> None:
    evidence = tmp_path / "readiness.json"
    _write(evidence, _evidence())

    receipt = _read(evidence)

    assert receipt["schema"] == READINESS_RECEIPT_SCHEMA
    assert receipt["ready"] is True
    assert receipt["dependency_gate"]["ready"] is True
    assert receipt["runtime_state"]["configured"]["state"] == "configured"
    assert receipt["runtime_state"]["measured"]["state"] == "measured"
    assert receipt["evidence"]["details"]["api_token"] == "[redacted]"  # noqa: S105
    assert receipt["provenance"]["nofollow"] is True
    assert receipt["provenance"]["settled"] is True
    assert receipt["provenance"]["mode"] == "0600"
    assert len(receipt["receipt_sha256"]) == 64


def test_readiness_consumer_gate_fails_closed_without_mutating_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "readiness.json"
    payload = _evidence()
    payload["dependencies"][0]["consumers"] = []
    _write(evidence, payload)

    receipt = _read(evidence)

    assert receipt["state"] == "ready"
    assert receipt["ready"] is False
    assert receipt["dependency_gate"]["checks"][0]["missing_consumers"] == ["workerbee"]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda payload: payload.update(generation="old"), "READINESS_GENERATION_STALE"),
        (lambda payload: payload.update(observed_at=800.0), "READINESS_EVIDENCE_STALE"),
        (
            lambda payload: payload.update(api_version="unknown/v9"),
            "READINESS_EVIDENCE_SCHEMA_INVALID",
        ),
    ],
)
def test_readiness_rejects_stale_or_unrecognized_evidence(
    tmp_path: Path, mutate: Any, code: str
) -> None:
    evidence = tmp_path / "readiness.json"
    payload = _evidence()
    mutate(payload)
    _write(evidence, payload)

    with pytest.raises(WorkerBeeError) as error:
        _read(evidence)

    assert error.value.code == code


def test_readiness_rejects_symlinks_nonprivate_modes_and_oversize_files(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    _write(target, _evidence())
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(WorkerBeeError) as symlink_error:
        _read(link)
    assert symlink_error.value.code == "READINESS_EVIDENCE_TYPE_INVALID"

    target.chmod(0o640)
    with pytest.raises(WorkerBeeError) as mode_error:
        _read(target)
    assert mode_error.value.code == "READINESS_EVIDENCE_MODE_INVALID"

    target.write_bytes(b"x" * (MAX_EVIDENCE_BYTES + 1))
    target.chmod(0o600)
    with pytest.raises(WorkerBeeError) as size_error:
        _read(target)
    assert size_error.value.code == "READINESS_EVIDENCE_TOO_LARGE"


def test_runtime_readback_never_conflates_configured_and_measured_state() -> None:
    measured = runtime_state_readback("containerd", {"ok": True, "selected": "podman"})
    unknown = runtime_state_readback(None, {"ok": False, "error": {"code": "MISSING"}})

    assert measured["configured"] == {
        "state": "configured",
        "value": "containerd",
        "provenance": "daemon configuration",
    }
    assert measured["measured"]["value"] == "podman"
    assert measured["effective"] == {"state": "measured", "value": "podman"}
    assert unknown["configured"]["state"] == "unknown"
    assert unknown["measured"]["state"] == "unknown"
    assert unknown["evidence"]["error_code"] == "MISSING"


def test_every_advertised_tool_has_a_versioned_risk_classification() -> None:
    normal = tool_policy_descriptors(MCP_TOOL_NAMES)
    enabled = tool_policy_descriptors([*MCP_TOOL_NAMES, *CONTROLLED_RUN_TOOL_NAMES])

    assert normal["schema"] == "workerbee.mcp-tool-policy/v1"
    assert normal["complete"] is True
    assert normal["count"] == len(MCP_TOOL_NAMES)
    assert enabled["count"] == len(MCP_TOOL_NAMES) + len(CONTROLLED_RUN_TOOL_NAMES)
    assert {item["name"] for item in normal["tools"]} == set(MCP_TOOL_NAMES)
    assert all(item["mutation_intent"] for item in enabled["tools"])
    assert all(item["risk"] in {"low", "medium", "high", "critical"} for item in enabled["tools"])
    with pytest.raises(ValueError, match="unclassified"):
        tool_policy_descriptors(["workerbee_v1_unclassified"])


def test_readiness_file_is_never_opened_without_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flags: list[int] = []
    original_open = os.open

    def capture_open(path: Path, value: int) -> int:
        flags.append(value)
        return original_open(path, value)

    evidence = tmp_path / "readiness.json"
    _write(evidence, _evidence())
    monkeypatch.setattr(os, "open", capture_open)
    _read(evidence)
    assert flags
    assert flags[0] & os.O_NOFOLLOW
