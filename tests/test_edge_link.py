from __future__ import annotations

import json
import subprocess
from pathlib import Path
from stat import S_IMODE
from typing import Any

import pytest

from workerbee.cli import _edge_link_kwargs, build_parser
from workerbee.contract import WorkerBeeError
from workerbee.daemon import WorkerBeeDaemon
from workerbee.edge_link import K1sEdgeLinkInfo, K1sEdgeLinkRunner

MASKED_VALUE = "***"


def _bundle() -> dict[str, Any]:
    return {
        "site_id": "workerbee-edge",
        "controller_url": "http://192.168.29.15:9110",
        "agent_token": "agent-secret",
        "nats_leaf_addr": "192.168.29.15:7422",
        "nats_leaf_url": "nats://site-uplink:leaf-secret@192.168.29.15:7422",
        "rathole_server_addr": "192.168.29.15:2333",
        "rathole_token": "rathole-secret",
        "registry_host": "reg.microk8s.core.home.arpa:32000",
        "stack_domain": "k1s-dev-a.core.home.arpa",
        "wildcard_apps_domain": "*.apps.k1s-dev-a.core.home.arpa",
        "suggested_edge_env": {
            "AE_CONTROLLER_URL": "http://192.168.29.15:9110",
            "AE_AGENT_TOKEN": "agent-secret",
            "K1S_NATS_LEAF_ADDR": "192.168.29.15:7422",
            "K1S_NATS_LEAF_URL": "nats://site-uplink:leaf-secret@192.168.29.15:7422",
            "AE_RATHOLE_SERVER_ADDR": "192.168.29.15:2333",
            "AE_RATHOLE_DEFAULT_TOKEN": "rathole-secret",
        },
    }


def _assert_ai_max_installer_assurance(contract: dict[str, Any]) -> None:
    boot_assurance = {
        "secure_image_validation": "enabled",
        "boot_validation": "measured-verified",
        "tamper_detection": "enabled",
        "validation_failure_action": "disable-quarantine",
        "core_alerting": "when-connected",
    }
    assert contract["boot_assurance"] == boot_assurance
    assert contract["installer"] == {
        "profile": "nixos-ai-max-edge-cell-installer-v1",
        "image": "nixos-ai-max-edge-cell-installer",
        "signed_by": "k1s-core-root-of-trust",
        "signer": {
            "authority": "k1s-core-root-of-trust",
            "source": "k1s-core-controller",
        },
        "artifact": {
            "name": "nixos-ai-max-edge-cell-installer",
            "profile": "nixos-ai-max-edge-cell-installer-v1",
            "image": "nixos-ai-max-edge-cell-installer",
            "version": "stage7-local",
            "artifact_digest": (
                "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            ),
            "manifest_digest": (
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            "path_coverage": ["gateway", "cell-node"],
            "provenance": {
                "builder": "k1s-public-stage7-local-simulator",
                "source_revision": "public-dev-stage7",
                "created_at": "2026-06-25T00:00:00Z",
            },
        },
        "signature": {
            "algorithm": "k1s-local-sim-ed25519-sha256",
            "signing_key_id": "k1s-core-root-of-trust",
            "signed_digest": (
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            "signature": (
                "k1s-sim-signature:3333333333333333333333333333333333333333333333333333333333333333"
            ),
        },
        "role_scaffolds": [
            {
                "role": "gateway",
                "module_ref": "nixos/modules/ai-max/installer/gateway.nix",
                "config_ref": "nixos/configs/ai-max/gateway-installed-system.nix",
                "derived_from_manifest_digest": (
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                "post_install": {
                    "auto_boot": "enabled",
                    "connect_target": "core",
                    "usb_device_policy": "signed-only",
                    "display_mode": "telemetry",
                },
            },
            {
                "role": "cell-node",
                "module_ref": "nixos/modules/ai-max/installer/cell-node.nix",
                "config_ref": "nixos/configs/ai-max/cell-node-installed-system.nix",
                "derived_from_manifest_digest": (
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                "post_install": {
                    "auto_boot": "enabled",
                    "connect_target": "gateway",
                    "usb_device_policy": "limited",
                    "display_mode": "connect-monitor-to-gateway",
                },
            },
        ],
        "boot_evidence": [
            {
                "node_id": "gateway-1",
                "role": "gateway",
                "installer_profile": "nixos-ai-max-edge-cell-installer-v1",
                "installer_image": "nixos-ai-max-edge-cell-installer",
                "artifact_digest": (
                    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
                ),
                "manifest_digest": (
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                "boot_measurement_digest": (
                    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                ),
                "signing_key_id": "k1s-core-root-of-trust",
                "verifier_trust_root": "k1s-core-root-of-trust",
                "nonce": "k1s-stage9-nonce-gateway",
                "created_at": "2026-06-25T00:00:00Z",
                "verification": {
                    "status": "verified",
                    "verifier": "k1s-local-boot-evidence-verifier-v1",
                    "trust_root": "k1s-core-root-of-trust",
                    "failure_reasons": [],
                },
            },
            {
                "node_id": "cell-node-1",
                "role": "cell-node",
                "installer_profile": "nixos-ai-max-edge-cell-installer-v1",
                "installer_image": "nixos-ai-max-edge-cell-installer",
                "artifact_digest": (
                    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
                ),
                "manifest_digest": (
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                "boot_measurement_digest": (
                    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
                "signing_key_id": "k1s-core-root-of-trust",
                "verifier_trust_root": "k1s-core-root-of-trust",
                "nonce": "k1s-stage9-nonce-cell-node",
                "created_at": "2026-06-25T00:00:00Z",
                "verification": {
                    "status": "verified",
                    "verifier": "k1s-local-boot-evidence-verifier-v1",
                    "trust_root": "k1s-core-root-of-trust",
                    "failure_reasons": [],
                },
            },
        ],
        "tampered_boot_evidence_fixture": {
            "node_id": "gateway-1",
            "role": "gateway",
            "installer_profile": "nixos-ai-max-edge-cell-installer-v1",
            "installer_image": "nixos-ai-max-edge-cell-installer",
            "artifact_digest": (
                "sha256:6666666666666666666666666666666666666666666666666666666666666666"
            ),
            "manifest_digest": (
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            "boot_measurement_digest": (
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
            "signing_key_id": "k1s-core-root-of-trust",
            "verifier_trust_root": "k1s-core-root-of-trust",
            "nonce": "stale-nonce",
            "created_at": "2026-06-25T00:00:00Z",
            "verification": {
                "status": "rejected",
                "verifier": "k1s-local-boot-evidence-verifier-v1",
                "trust_root": "k1s-core-root-of-trust",
                "failure_reasons": ["artifact-digest-mismatch", "stale-nonce"],
            },
        },
        "verification": {
            "status": "verified",
            "checked_by": "workerbee-local-simulator",
            "root_of_trust": "k1s-core-root-of-trust",
            "signature_algorithm": "k1s-local-sim-ed25519-sha256",
            "signed_digest": (
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            "profile_match": True,
            "image_match": True,
            "path_coverage": ["gateway", "cell-node"],
            "role_scaffold_ready": True,
            "role_coverage": ["gateway", "cell-node"],
            "boot_evidence_ready": True,
            "boot_evidence_roles": ["gateway", "cell-node"],
        },
        "assurance": boot_assurance,
        "install_paths": [
            {
                "path": "gateway",
                "post_install": {
                    "auto_boot": "enabled",
                    "connect_target": "core",
                    "usb_device_policy": "signed-only",
                    "display_mode": "telemetry",
                },
            },
            {
                "path": "cell-node",
                "post_install": {
                    "auto_boot": "enabled",
                    "connect_target": "gateway",
                    "usb_device_policy": "limited",
                    "display_mode": "connect-monitor-to-gateway",
                },
            },
        ],
    }
    healthy_members = [
        {
            "node_id": item["node_id"],
            "role": item["role"],
            "status": "verified",
            "schedulable": True,
            "quarantined": False,
            "failure_reasons": [],
            "alert": "none",
        }
        for item in contract["members"]
    ]
    quarantined_node_id = next(
        item["node_id"] for item in contract["members"] if item["role"] == "cell-node"
    )
    tampered_members = [
        {
            "node_id": item["node_id"],
            "role": item["role"],
            "status": "tampered" if item["node_id"] == quarantined_node_id else "verified",
            "schedulable": item["node_id"] != quarantined_node_id,
            "quarantined": item["node_id"] == quarantined_node_id,
            "failure_reasons": (
                ["boot-measurement-mismatch"] if item["node_id"] == quarantined_node_id else []
            ),
            "alert": "pending" if item["node_id"] == quarantined_node_id else "none",
        }
        for item in contract["members"]
    ]
    assert contract["assurance_enforcement"] == {
        "mode": "local-simulated",
        "policy": "exclude-quarantined-from-placement",
        "status": "healthy",
        "usable_fabric_size": len(contract["members"]),
        "quarantined_count": 0,
        "members": healthy_members,
        "boot_evidence_status": [
            {
                "node_id": "gateway-1",
                "role": "gateway",
                "status": "verified",
                "failure_reasons": [],
            },
            {
                "node_id": "cell-node-1",
                "role": "cell-node",
                "status": "verified",
                "failure_reasons": [],
            },
        ],
        "tampered_quarantine_fixture": {
            "status": "quarantined",
            "quarantined_node_id": quarantined_node_id,
            "usable_fabric_size": len(contract["members"]) - 1,
            "quarantined_count": 1,
            "members": tampered_members,
        },
    }
    assert contract["autonomy_state"] == {
        "mode": "local-simulated",
        "current_state": "connected",
        "local_service_continuity": True,
        "cache": {
            "ready": True,
            "approved_workload_ref": "inferencecell/default/ai-max-edge-cell",
            "model_artifact_ref": "models/llama:stage11-local",
            "service_endpoints": {
                "gateway-api": "http://gateway.local:18080",
                "cell-monitor": "http://gateway.local:19090",
            },
            "last_core_sync": "core-sync-stage11",
        },
        "supported_events": [
            "core-link-lost",
            "local-services-retained",
            "core-link-restored",
            "reconcile-completed",
            "reconcile-failed",
        ],
        "supported_transitions": [
            {
                "from": "connected",
                "event": "core-link-lost",
                "to": "core-link-unavailable",
            },
            {
                "from": "core-link-unavailable",
                "event": "local-services-retained",
                "to": "degraded-local-only",
            },
            {
                "from": "degraded-local-only",
                "event": "core-link-restored",
                "to": "reconciling",
            },
            {
                "from": "reconciling",
                "event": "reconcile-completed",
                "to": "reconciled",
            },
            {
                "from": "reconciling",
                "event": "reconcile-failed",
                "to": "degraded-local-only",
            },
        ],
        "sample_transition_trace": [
            {
                "from": "connected",
                "event": "core-link-lost",
                "to": "core-link-unavailable",
            },
            {
                "from": "core-link-unavailable",
                "event": "local-services-retained",
                "to": "degraded-local-only",
            },
            {
                "from": "degraded-local-only",
                "event": "core-link-restored",
                "to": "reconciling",
            },
            {
                "from": "reconciling",
                "event": "reconcile-completed",
                "to": "reconciled",
            },
        ],
        "sample_final_state": "reconciled",
    }
    assert contract["disconnected_drill_report"] == {
        "drill_id": "ai-max-disconnected-local-drill-stage12",
        "name": "AI Max disconnected autonomy local simulation",
        "version": "stage12-local-v1",
        "mode": "simulation-only",
        "live_core_mutation": False,
        "live_network_disruption": False,
        "starting_state": "connected",
        "core_outage_event": "core-link-lost",
        "degraded_state": "degraded-local-only",
        "local_service_available": True,
        "local_probe": {
            "kind": "simulated-http",
            "endpoint": "http://gateway.local:18080",
            "expected_status": 200,
            "observed_status": 200,
            "ok": True,
            "source": "gateway-cache",
        },
        "core_restore_event": "core-link-restored",
        "reconciliation": {
            "from": "reconciling",
            "to": "reconciled",
            "event": "reconcile-completed",
            "ok": True,
            "evidence_marker": "stage12-reconcile-marker",
        },
        "transition_trace": contract["autonomy_state"]["sample_transition_trace"],
        "final_state": "reconciled",
        "cache_summary": {
            "ready": True,
            "approved_workload_ref": "inferencecell/default/ai-max-edge-cell",
            "model_artifact_ref": "models/llama:stage11-local",
            "last_core_sync": "core-sync-stage11",
        },
        "assertions": {
            "started_connected": True,
            "degraded_local_only": True,
            "local_service_continuity": True,
            "restored_to_reconciling": True,
            "reconciled": True,
            "no_live_disruption": True,
        },
    }
    assert contract["ha_lab_deployment_plan"] == {
        "plan_id": "ai-max-ha-lab-k1s-dev-a-stage13",
        "name": "AI Max HA lab deployment path dry run",
        "version": "stage13-local-v1",
        "mode": "dry-run-plan-only",
        "target": {
            "release": "k1s-dev-a",
            "namespace": "k1s-dev-a",
            "runtime": "microk8s",
        },
        "profile": {
            "path": "edge-link",
            "cell_node_count": 3,
            "fabric_cell_count": contract["fabric_cell_count"],
            "lan_scope": contract["lan_scope"],
        },
        "preflight_checklist": [
            {
                "id": "core-controller-url",
                "required": True,
                "description": "Provide a reachable k1s core/controller URL in the bundle.",
            },
            {
                "id": "agent-token-or-bundle",
                "required": True,
                "description": "Provide the edge agent token through a bundle or explicit input.",
            },
            {
                "id": "namespace",
                "required": True,
                "description": "Confirm namespace k1s-dev-a exists before live use.",
            },
            {
                "id": "dry-run-no-live-mutation",
                "required": True,
                "description": (
                    "This plan is metadata only and must not mutate MicroK8s during tests."
                ),
            },
        ],
        "validation_steps": [
            "manifest validation",
            "edge-link start",
            "edge-link validate",
            "edge-link status",
            "disconnected drill report review",
            "cleanup stop",
        ],
        "commands": {
            "start": (
                "scripts/dev/wb-containerd --project wb014 edge-link start --k1s-root ../k1s "
                "--from-microk8s --release k1s-dev-a --namespace k1s-dev-a --site-id "
                "workerbee-edge --node-id workerbee-edge-node --cell-node-count 3 "
                f"--fabric-cell-count {contract['fabric_cell_count']} --lan-scope "
                f"{contract['lan_scope']}"
            ),
            "validate": (
                "scripts/dev/wb-containerd --project wb014 edge-link validate --k1s-root ../k1s "
                "--from-microk8s --release k1s-dev-a --namespace k1s-dev-a"
            ),
            "status": "scripts/dev/wb-containerd --project wb014 edge-link status",
            "stop": "scripts/dev/wb-containerd --project wb014 edge-link stop",
        },
        "report_inputs": {
            "disconnected_drill_report": "edge_cell_contract.disconnected_drill_report",
            "assurance_enforcement": "edge_cell_contract.assurance_enforcement",
            "autonomy_state": "edge_cell_contract.autonomy_state",
        },
        "safety": {
            "dry_run": True,
            "mutates_microk8s": False,
            "starts_workerbee_project": False,
            "requires_operator_confirmation_for_live_run": True,
        },
    }


def test_edge_link_runner_rejects_non_containerd_runtime(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("workerbee.edge_link.resolve_runtime", lambda _runtime: "docker")
    runner = K1sEdgeLinkRunner(project="demo", state_root=tmp_path, runtime="docker")

    with pytest.raises(WorkerBeeError) as exc:
        runner.start(bundle=_bundle(), advertise_host="192.168.29.111", build_images=False)

    assert exc.value.code == "K1S_EDGE_LINK_REQUIRES_CONTAINERD"


def test_edge_link_requires_bootstrap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("workerbee.edge_link.resolve_runtime", lambda _runtime: "containerd")
    runner = K1sEdgeLinkRunner(project="demo", state_root=tmp_path, runtime="containerd")

    with pytest.raises(WorkerBeeError) as exc:
        runner.start(advertise_host="192.168.29.111", build_images=False)

    assert exc.value.code == "EDGE_LINK_BOOTSTRAP_REQUIRED"
    assert exc.value.details["missing"] == [
        "agent_token",
        "controller_url",
        "nats_leaf_addr",
        "rathole_server_addr",
    ]


def test_edge_link_start_writes_masked_state_and_container_commands(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.edge_link.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr(K1sEdgeLinkRunner, "_infer_advertise_host", lambda _self: "192.168.29.111")
    monkeypatch.setattr(K1sEdgeLinkRunner, "_wait_ready", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(
        "workerbee.edge_link.choose_port",
        lambda preferred, **_kwargs: int(preferred),
    )

    nvidia_dir = tmp_path / "nvidia"
    nvidia_dir.mkdir()
    nvidia_smi = nvidia_dir / "nvidia-smi"
    nvidia_cli = nvidia_dir / "nvidia-container-cli"
    nvidia_runtime = nvidia_dir / "nvidia-container-runtime"
    for path in (nvidia_smi, nvidia_cli, nvidia_runtime):
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
    runtime_config = nvidia_dir / "config"
    runtime_config.mkdir()
    monkeypatch.setattr(
        K1sEdgeLinkRunner,
        "_detect_nvidia",
        lambda _self: {
            "present": True,
            "nvidia_smi": str(nvidia_smi),
            "nvidia_container_cli": str(nvidia_cli),
            "nvidia_container_runtime": str(nvidia_runtime),
            "runtime_config_dir": str(runtime_config),
            "summary": "GPU 0: Test GPU",
        },
    )

    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        commands.append([str(part) for part in cmd])
        if "network" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "missing")
        if "run" in cmd:
            name = cmd[cmd.index("--name") + 1]
            return subprocess.CompletedProcess(cmd, 0, f"{name}-id\n", "")
        if "ps" in cmd:
            name_filters = [
                str(cmd[index + 1]).removeprefix("name=")
                for index, value in enumerate(cmd[:-1])
                if value == "--filter" and str(cmd[index + 1]).startswith("name=")
            ]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(name_filters), "")
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr("workerbee.edge_link.subprocess.run", fake_run)

    k1s_root = tmp_path / "k1s"
    (k1s_root / "ops" / "dev").mkdir(parents=True)
    (k1s_root / "ops" / "images").mkdir(parents=True)
    (k1s_root / "ops" / "dev" / "nats-edge.conf").write_text(
        'server_name: "edge-sfo-01"\nport: 4223\nhttp: 8223\n'
        'leafnodes { remotes = [{ url: "nats://site-sfo-edge-01-uplink:dev@nats-hub:7422" }] }\n',
        encoding="utf-8",
    )

    runner = K1sEdgeLinkRunner(
        project="Edge Demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=k1s_root,
    )
    result = runner.start(
        bundle=_bundle(),
        node_id="edge-node-1",
        edge_local_addr="192.168.29.111:8081",
        timeout=0.01,
    )

    assert result["ok"] is True
    edge = result["edge_link"]
    assert edge["profile"] == "k1s-edge-link"
    assert edge["agent_token"] == MASKED_VALUE
    assert edge["rathole_token"] == MASKED_VALUE
    assert edge["nats_leaf_url"] == MASKED_VALUE
    assert edge["bootstrap"]["nats_leaf_url"] == MASKED_VALUE
    assert edge["bootstrap"]["suggested_edge_env"]["K1S_NATS_LEAF_URL"] == MASKED_VALUE
    assert edge["cell_node_count"] == 0
    assert edge["fabric_cell_count"] == 1
    assert edge["lan_scope"] == "workerbee-lan"
    assert edge["edge_cell_contract"] == {}
    assert edge["agent_endpoint"] == "http://192.168.29.111:19109"
    assert edge["edge_local_addr"] == "192.168.29.111:8081"
    assert S_IMODE((runner.edge_dir / "bootstrap.json").stat().st_mode) == 0o600

    run_commands = [cmd for cmd in commands if "run" in cmd]
    assert [cmd[cmd.index("--name") + 1].rsplit("-", 1)[-1] for cmd in run_commands] == [
        "nats",
        "rathole",
        "gateway",
        "node",
    ]
    for cmd in run_commands:
        assert "--restart" in cmd
        assert cmd[cmd.index("--restart") + 1] == "unless-stopped"
    rathole_cmd = next(
        cmd for cmd in run_commands if cmd[cmd.index("--name") + 1].endswith("-rathole")
    )
    assert rathole_cmd[rathole_cmd.index("--network") + 1] == "host"
    node_cmd = next(cmd for cmd in run_commands if cmd[cmd.index("--name") + 1].endswith("-node"))
    assert "0.0.0.0:19109:9109" in node_cmd
    assert f"AE_NVIDIA_SMI_BIN={nvidia_smi}" in node_cmd
    assert f"{nvidia_smi}:{nvidia_smi}:ro" in node_cmd
    assert "AE_AGENT_ENDPOINT=http://192.168.29.111:19109" in node_cmd
    assert "AE_RUNTIME_BACKEND=containerd" in node_cmd

    nats_conf = runner.edge_dir / "config" / "nats-edge.conf"
    assert "workerbee-edge" in nats_conf.read_text(encoding="utf-8")
    assert "leaf-secret" in nats_conf.read_text(encoding="utf-8")
    rathole_conf = runner.edge_dir / "config" / "rathole-client.toml"
    assert 'local_addr = "192.168.29.111:8081"' in rathole_conf.read_text(encoding="utf-8")
    info = json.loads(runner.info_file.read_text(encoding="utf-8"))
    assert info["agent_token"] == _bundle()["agent_token"]


def test_edge_link_start_can_simulate_ai_max_edge_cell(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.edge_link.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr(K1sEdgeLinkRunner, "_infer_advertise_host", lambda _self: "192.168.29.111")
    monkeypatch.setattr(K1sEdgeLinkRunner, "_wait_ready", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(
        "workerbee.edge_link.choose_port",
        lambda preferred, **_kwargs: int(preferred),
    )
    monkeypatch.setattr(K1sEdgeLinkRunner, "_detect_nvidia", lambda _self: {"present": False})

    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        commands.append([str(part) for part in cmd])
        if "network" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "missing")
        if "run" in cmd:
            name = cmd[cmd.index("--name") + 1]
            return subprocess.CompletedProcess(cmd, 0, f"{name}-id\n", "")
        if "ps" in cmd:
            name_filters = [
                str(cmd[index + 1]).removeprefix("name=")
                for index, value in enumerate(cmd[:-1])
                if value == "--filter" and str(cmd[index + 1]).startswith("name=")
            ]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(name_filters), "")
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr("workerbee.edge_link.subprocess.run", fake_run)

    k1s_root = tmp_path / "k1s"
    (k1s_root / "ops" / "dev").mkdir(parents=True)
    (k1s_root / "ops" / "images").mkdir(parents=True)

    runner = K1sEdgeLinkRunner(
        project="Edge Demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=k1s_root,
    )
    result = runner.start(
        bundle=_bundle(),
        node_id="edge-node-1",
        cell_node_count=3,
        timeout=0.01,
        build_images=False,
    )

    assert result["ok"] is True
    edge = result["edge_link"]
    assert edge["node_id"] == "edge-node-1"
    assert edge["agent_endpoint"] == "http://192.168.29.111:19109"
    assert edge["cell_node_count"] == 3
    assert edge["fabric_cell_count"] == 1
    assert edge["lan_scope"] == "workerbee-lan"
    contract = edge["edge_cell_contract"]
    assert contract["profile"] == "ai-max-edge-cell-v1"
    assert contract["size"] == 4
    assert contract["fabric_cell_count"] == 1
    assert contract["fabric_size"] == 4
    assert contract["gateway_node_id"] == "edge-node-1"
    assert contract["gateway_peer_ids"] == []
    assert contract["cell_node_ids"] == [
        "edge-node-1-cell-1",
        "edge-node-1-cell-2",
        "edge-node-1-cell-3",
    ]
    assert contract["compute_node_ids"] == [
        "edge-node-1",
        "edge-node-1-cell-1",
        "edge-node-1-cell-2",
        "edge-node-1-cell-3",
    ]
    assert contract["gateway_discovery"] == {
        "mode": "lan-local",
        "fabric_cell_count": 1,
        "lan_scope": "workerbee-lan",
        "gateway_peer_ids": [],
    }
    _assert_ai_max_installer_assurance(contract)
    assert contract["cells"] == [
        {
            "cell_index": 1,
            "gateway_node_id": "edge-node-1",
            "cell_node_ids": [
                "edge-node-1-cell-1",
                "edge-node-1-cell-2",
                "edge-node-1-cell-3",
            ],
            "compute_node_ids": [
                "edge-node-1",
                "edge-node-1-cell-1",
                "edge-node-1-cell-2",
                "edge-node-1-cell-3",
            ],
        }
    ]
    assert [member["role"] for member in contract["members"]] == [
        "gateway",
        "cell-node",
        "cell-node",
        "cell-node",
    ]
    assert all(member["compute_eligible"] is True for member in contract["members"])

    run_commands = [cmd for cmd in commands if "run" in cmd]
    component_names = [cmd[cmd.index("--name") + 1] for cmd in run_commands]
    assert [name.rsplit("k1s-edge-link-", 1)[-1] for name in component_names] == [
        "edge-nats",
        "rathole",
        "gateway",
        "node",
        "cell-node-1",
        "cell-node-2",
        "cell-node-3",
    ]
    node_commands = [
        cmd
        for cmd in run_commands
        if cmd[cmd.index("--name") + 1].rsplit("k1s-edge-link-", 1)[-1]
        in {"node", "cell-node-1", "cell-node-2", "cell-node-3"}
    ]
    assert [next(item for item in cmd if item.endswith(":9109")) for cmd in node_commands] == [
        "0.0.0.0:19109:9109",
        "0.0.0.0:19110:9109",
        "0.0.0.0:19111:9109",
        "0.0.0.0:19112:9109",
    ]
    assert any(
        "AE_NODE_LABELS=role=gateway,compute_eligible=true" in item for item in node_commands[0]
    )
    for cmd in node_commands[1:]:
        assert any("AE_NODE_LABELS=role=cell-node,compute_eligible=true" in item for item in cmd)


def test_edge_link_start_can_simulate_ai_max_multi_cell_fabric(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.edge_link.resolve_runtime", lambda _runtime: "containerd")
    monkeypatch.setattr(K1sEdgeLinkRunner, "_infer_advertise_host", lambda _self: "192.168.29.111")
    monkeypatch.setattr(K1sEdgeLinkRunner, "_wait_ready", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(
        "workerbee.edge_link.choose_port",
        lambda preferred, **_kwargs: int(preferred),
    )
    monkeypatch.setattr(K1sEdgeLinkRunner, "_detect_nvidia", lambda _self: {"present": False})

    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        commands.append([str(part) for part in cmd])
        if "network" in cmd and "inspect" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "missing")
        if "run" in cmd:
            name = cmd[cmd.index("--name") + 1]
            return subprocess.CompletedProcess(cmd, 0, f"{name}-id\n", "")
        if "ps" in cmd:
            name_filters = [
                str(cmd[index + 1]).removeprefix("name=")
                for index, value in enumerate(cmd[:-1])
                if value == "--filter" and str(cmd[index + 1]).startswith("name=")
            ]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(name_filters), "")
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr("workerbee.edge_link.subprocess.run", fake_run)

    k1s_root = tmp_path / "k1s"
    (k1s_root / "ops" / "dev").mkdir(parents=True)
    (k1s_root / "ops" / "images").mkdir(parents=True)

    runner = K1sEdgeLinkRunner(
        project="Edge Demo",
        state_root=tmp_path,
        runtime="containerd",
        k1s_root=k1s_root,
    )
    result = runner.start(
        bundle=_bundle(),
        node_id="edge-node-1",
        cell_node_count=3,
        fabric_cell_count=2,
        lan_scope="floor-a",
        timeout=0.01,
        build_images=False,
    )

    assert result["ok"] is True
    edge = result["edge_link"]
    assert edge["cell_node_count"] == 3
    assert edge["fabric_cell_count"] == 2
    assert edge["lan_scope"] == "floor-a"
    contract = edge["edge_cell_contract"]
    assert contract["profile"] == "ai-max-edge-cell-v1"
    assert contract["size"] == 4
    assert contract["fabric_cell_count"] == 2
    assert contract["fabric_size"] == 8
    assert contract["gateway_node_id"] == "edge-node-1"
    assert contract["gateway_peer_ids"] == ["edge-node-1-gateway-2"]
    assert contract["gateway_discovery"] == {
        "mode": "lan-local",
        "fabric_cell_count": 2,
        "lan_scope": "floor-a",
        "gateway_peer_ids": ["edge-node-1-gateway-2"],
    }
    _assert_ai_max_installer_assurance(contract)
    assert contract["cell_node_ids"] == [
        "edge-node-1-cell-1",
        "edge-node-1-cell-2",
        "edge-node-1-cell-3",
    ]
    assert contract["all_cell_node_ids"] == [
        "edge-node-1-cell-1",
        "edge-node-1-cell-2",
        "edge-node-1-cell-3",
        "edge-node-1-gateway-2-cell-1",
        "edge-node-1-gateway-2-cell-2",
        "edge-node-1-gateway-2-cell-3",
    ]
    assert contract["compute_node_ids"] == [
        "edge-node-1",
        "edge-node-1-cell-1",
        "edge-node-1-cell-2",
        "edge-node-1-cell-3",
        "edge-node-1-gateway-2",
        "edge-node-1-gateway-2-cell-1",
        "edge-node-1-gateway-2-cell-2",
        "edge-node-1-gateway-2-cell-3",
    ]
    assert len(contract["cells"]) == 2
    assert contract["cells"][1] == {
        "cell_index": 2,
        "gateway_node_id": "edge-node-1-gateway-2",
        "cell_node_ids": [
            "edge-node-1-gateway-2-cell-1",
            "edge-node-1-gateway-2-cell-2",
            "edge-node-1-gateway-2-cell-3",
        ],
        "compute_node_ids": [
            "edge-node-1-gateway-2",
            "edge-node-1-gateway-2-cell-1",
            "edge-node-1-gateway-2-cell-2",
            "edge-node-1-gateway-2-cell-3",
        ],
    }
    roles = [member["role"] for member in contract["members"]]
    assert roles.count("gateway") == 2
    assert roles.count("cell-node") == 6
    assert all(member["compute_eligible"] is True for member in contract["members"])
    assert [member["agent_host_port"] for member in contract["members"]] == [
        19109,
        19110,
        19111,
        19112,
        19113,
        19114,
        19115,
        19116,
    ]

    run_commands = [cmd for cmd in commands if "run" in cmd]
    component_names = [cmd[cmd.index("--name") + 1] for cmd in run_commands]
    assert [name.rsplit("k1s-edge-link-", 1)[-1] for name in component_names] == [
        "edge-nats",
        "rathole",
        "gateway",
        "node",
        "cell-node-1",
        "cell-node-2",
        "cell-node-3",
        "cell-2-gateway",
        "cell-2-node-1",
        "cell-2-node-2",
        "cell-2-node-3",
    ]
    node_commands = [
        cmd
        for cmd in run_commands
        if cmd[cmd.index("--name") + 1].rsplit("k1s-edge-link-", 1)[-1]
        not in {"edge-nats", "rathole", "gateway"}
    ]
    assert any(
        "AE_NODE_LABELS=role=gateway,compute_eligible=true" in item for item in node_commands[4]
    )
    for cmd in node_commands[1:4] + node_commands[5:]:
        assert any("AE_NODE_LABELS=role=cell-node,compute_eligible=true" in item for item in cmd)


def test_edge_link_rejects_unsupported_cell_node_count(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("workerbee.edge_link.resolve_runtime", lambda _runtime: "containerd")
    runner = K1sEdgeLinkRunner(project="demo", state_root=tmp_path, runtime="containerd")

    with pytest.raises(WorkerBeeError) as exc:
        runner.start(
            bundle=_bundle(),
            advertise_host="192.168.29.111",
            cell_node_count=2,
            build_images=False,
        )

    assert exc.value.code == "K1S_EDGE_CELL_UNSUPPORTED_SIZE"
    assert exc.value.details["supported_cell_node_counts"] == [0, 3]


@pytest.mark.parametrize("fabric_cell_count", [0, -1, 3])
def test_edge_link_rejects_unsupported_fabric_cell_count(
    tmp_path: Path,
    monkeypatch,
    fabric_cell_count: int,
) -> None:
    monkeypatch.setattr("workerbee.edge_link.resolve_runtime", lambda _runtime: "containerd")
    runner = K1sEdgeLinkRunner(project="demo", state_root=tmp_path, runtime="containerd")

    with pytest.raises(WorkerBeeError) as exc:
        runner.start(
            bundle=_bundle(),
            advertise_host="192.168.29.111",
            cell_node_count=3,
            fabric_cell_count=fabric_cell_count,
            build_images=False,
        )

    assert exc.value.code == "K1S_EDGE_FABRIC_UNSUPPORTED_CELL_COUNT"
    assert exc.value.details["supported_fabric_cell_counts"] == [1, 2, 4, 8]


def test_edge_link_rejects_multi_cell_fabric_without_edge_cell(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("workerbee.edge_link.resolve_runtime", lambda _runtime: "containerd")
    runner = K1sEdgeLinkRunner(project="demo", state_root=tmp_path, runtime="containerd")

    with pytest.raises(WorkerBeeError) as exc:
        runner.start(
            bundle=_bundle(),
            advertise_host="192.168.29.111",
            fabric_cell_count=2,
            build_images=False,
        )

    assert exc.value.code == "K1S_EDGE_FABRIC_REQUIRES_EDGE_CELL"
    assert exc.value.details["required_cell_node_count"] == 3


def test_edge_link_rejects_blank_lan_scope(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("workerbee.edge_link.resolve_runtime", lambda _runtime: "containerd")
    runner = K1sEdgeLinkRunner(project="demo", state_root=tmp_path, runtime="containerd")

    with pytest.raises(WorkerBeeError) as exc:
        runner.start(
            bundle=_bundle(),
            advertise_host="192.168.29.111",
            cell_node_count=3,
            fabric_cell_count=2,
            lan_scope=" ",
            build_images=False,
        )

    assert exc.value.code == "K1S_EDGE_FABRIC_LAN_SCOPE_REQUIRED"


def test_edge_link_node_check_requires_fresh_heartbeat(tmp_path: Path, monkeypatch) -> None:
    runner = K1sEdgeLinkRunner(project="demo", state_root=tmp_path, runtime="containerd")
    info = K1sEdgeLinkInfo(
        project="demo",
        profile="k1s-edge-link",
        state_root=str(tmp_path),
        edge_dir=str(tmp_path / "edge-link"),
        k1s_root=str(tmp_path / "k1s"),
        runtime="containerd",
        network="demo",
        namespace="demo",
        started_at=1779135542.0,
        site_id="workerbee-edge",
        node_id="edge-node-1",
        controller_url="http://127.0.0.1:9110",
        agent_token=MASKED_VALUE,
        nats_leaf_addr="127.0.0.1:7422",
    )
    old = {
        "node_id": "edge-node-1",
        "seen_at": "2026-05-18T20:18:39+00:00",
    }
    fresh = {
        "node_id": "edge-node-1",
        "seen_at": "2026-05-18T20:19:05+00:00",
    }
    records = [old, fresh]

    monkeypatch.setattr(K1sEdgeLinkRunner, "_node_record", lambda *_args: records.pop(0))
    monkeypatch.setattr("workerbee.edge_link.time.sleep", lambda _seconds: None)

    result = runner._wait_node_check(info, timeout=1.0, fresh_after=info.started_at)

    assert result["ok"] is True
    assert result["node"] == fresh


def test_daemon_profile_start_delegates_edge_link(tmp_path: Path, monkeypatch) -> None:
    daemon = WorkerBeeDaemon(
        state_root=tmp_path,
        runtime="containerd",
        default_project="demo",
        cwd=tmp_path,
    )
    calls: list[dict[str, Any]] = []

    class FakeEdgeRunner:
        def start(self, **kwargs):  # noqa: ANN001
            calls.append(kwargs)
            return {"ok": True, "edge_link": {"profile": "k1s-edge-link"}}

    monkeypatch.setattr(daemon, "_edge_link_runner", lambda *_args, **_kwargs: FakeEdgeRunner())
    monkeypatch.setattr(daemon, "_sync_ingress_projects_result", lambda: {"synced": False})

    result = daemon.profile_start(
        profile="k1s-edge-link",
        project="demo",
        from_microk8s=True,
        release="k1s-dev-a",
        namespace="k1s-dev-a",
        k1s_root=tmp_path / "k1s",
        timeout=12,
        build_images=False,
        cell_node_count=3,
        fabric_cell_count=2,
        lan_scope="floor-a",
    )

    assert result["ok"] is True
    assert calls[0]["from_microk8s"] is True
    assert calls[0]["release"] == "k1s-dev-a"
    assert calls[0]["timeout"] == 12
    assert calls[0]["build_images"] is False
    assert calls[0]["cell_node_count"] == 3
    assert calls[0]["fabric_cell_count"] == 2
    assert calls[0]["lan_scope"] == "floor-a"


def test_cli_edge_link_kwargs_include_edge_cell_fabric_options() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "edge-link",
            "start",
            "--cell-node-count",
            "3",
            "--fabric-cell-count",
            "2",
            "--lan-scope",
            "floor-a",
        ]
    )

    assert _edge_link_kwargs(args)["cell_node_count"] == 3
    assert _edge_link_kwargs(args)["fabric_cell_count"] == 2
    assert _edge_link_kwargs(args)["lan_scope"] == "floor-a"
