import argparse
import re
from pathlib import Path

from workerbee.agent import agent_instructions_markdown
from workerbee.cli import build_parser


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def test_consolidated_v1_contract_covers_public_operator_surface() -> None:
    text = Path("docs/v1-contract.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for heading in (
        "## Scope and authority",
        "## Compatibility",
        "## Install and rollback",
        "## Operations and readiness",
        "## Security",
        "## Support and diagnostics",
        "## Versioning and migration",
        "## Optional Agent PBX integration",
    ):
        assert heading in text
    for contract in (
        "Direct WorkerBee MCP is the primary interface",
        'source_mode="native-direct"',
        'execution_lane="native-local"',
        "mcp>=1.8,<2",
        "k1s-core==1.0.0rc1",
        "sealed provider artifact",
        "workerbee_v1_readiness_receipt",
        "configured, measured, and unknown",
        "different or expired generation is rejected",
        "install-workerbee.sh --rollback",
        "never falls back to a package index",
    ):
        assert contract in normalized


def test_agent_pbx_is_optional_and_not_in_generic_generated_instructions() -> None:
    contract = Path("docs/v1-contract.md").read_text(encoding="utf-8")
    instructions = agent_instructions_markdown()

    assert "Agent PBX interoperability is an\noptional composition" in contract
    assert "Agent PBX owns access profiles" in contract
    assert "Agent PBX" not in instructions
    assert "controlled-run" not in instructions
    assert "snapshot-import" not in instructions
    assert "managed-clone" not in instructions


def test_readme_and_sdist_expose_the_public_v1_contract() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    manifest = Path("MANIFEST.in").read_text(encoding="utf-8")

    assert "docs/v1-contract.md" in readme
    assert "include docs/v1-contract.md" in manifest
    assert "docs/cli-reference.md" in readme
    assert "include docs/cli-reference.md" in manifest
    assert "workerbee-install/releases" in readme
    assert "docs/wiki/WorkerBee-v1.md" in readme
    assert "v1 alpha" not in readme.lower()
    assert "'k1s-workerbee==1.0.0rc1'" in readme
    assert "PIP_NO_INDEX=1 PIP_CONFIG_FILE=/dev/null" in readme

    for image in (
        "docs/assets/k1s-workerbee-hero.jpg",
        "docs/assets/how-workerbee-works-v0.png",
    ):
        assert (
            "https://raw.githubusercontent.com/the-cm-collective/"
            f"k1s-workerbee/dev/{image}" in readme
        )
    assert 'src="docs/assets/' not in readme


def test_cli_reference_covers_installed_command_tree() -> None:
    text = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    commands = _subcommands(build_parser())
    assert set(commands) == {
        "agent",
        "apishim-smoke",
        "build-image",
        "bundle",
        "cleanup",
        "config",
        "containerd-privilege",
        "deploy",
        "deploy-poc",
        "doctor",
        "edge-link",
        "exec",
        "export-k8s",
        "global-dashboard",
        "ingress",
        "k1s-evidence",
        "logs",
        "manifest",
        "mcp",
        "poc-status",
        "profile",
        "project",
        "projects",
        "reset",
        "runbook",
        "security",
        "start",
        "status",
        "stop",
        "tls-info",
        "trust",
        "validate",
    }
    for command in commands:
        assert f"workerbee {command}" in text

    assert set(_subcommands(commands["config"])) == {"clear", "path", "set", "show"}
    assert set(_subcommands(commands["agent"])) == {"install", "instructions"}
    assert set(_subcommands(commands["mcp"])) == {
        "restart",
        "serve",
        "start",
        "status",
        "stop",
    }
    assert set(_subcommands(commands["manifest"])) == {
        "deploy-k1s",
        "deploy-local",
        "prepare",
        "validate",
    }
    assert set(_subcommands(commands["runbook"])) == {
        "export",
        "import",
        "status",
        "update",
    }
    assert set(_subcommands(commands["k1s-evidence"])) == {
        "doctor",
        "list",
        "logs",
        "plan",
        "status",
    }
    assert "read-only, local-state, non-networked" in text
    assert "workerbee_v1_k1s_profile_evidence(operation, profile," in text
    assert "Bootstrap creation is not exposed" in text


def test_public_docs_use_application_neutral_governance_examples() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in (Path("README.md"), *Path("docs").glob("*.md"))
    ).lower()
    for project_specific_term in (
        "nigerian",
        "ng-translation",
        "hausa",
        "igbo",
        "yoruba",
        "nitda",
    ):
        assert project_specific_term not in text


def test_public_claim_boundaries_distinguish_contract_from_qualification() -> None:
    text = Path("docs/v1-contract.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "## Claim and evidence boundary" in text
    for contract in (
        "source-validated/unqualified",
        "a capability descriptor is not proof",
        "Native/direct mode is never appliance evidence",
        "outside the WorkerBee OSS product boundary",
        "their presence in a schema is not an OSS implementation or availability claim",
        "Examples and future plans are not readiness receipts",
    ):
        assert contract in normalized


def test_release_docs_record_exact_accepted_k1s_rc1_checkpoint() -> None:
    text = Path("docs/releasing.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "### Accepted `k1s-core==1.0.0rc1` checkpoint" in text
    for identity in (
        "0827108aa2ee429de491de0842d33871e84b9eba",
        "44b9aaab8073f5534f38655b29258b4eb1774f0c",
        "k1s_core-1.0.0rc1-py3-none-any.whl",
        "1057213",
        "f341d21a2b0297aaa598826b694fccbc2e07aedc6a449b259010d9e07f520d20",
        "temporary-nonpublished",
    ):
        assert identity in normalized
    for mutable_path in (
        "pyproject.toml",
        "src/workerbee/runtime_provider.py",
        "scripts/build_wheelhouse.sh",
        "CHANGELOG.md",
        "docs/release-foundation.md",
        "tests/test_installer.py",
        "tests/test_profile_evidence.py",
        "tests/test_release_artifacts.py",
        "tests/test_runtime_provider.py",
        "tests/test_v1_contract_docs.py",
    ):
        assert f"`{mutable_path}`" in text
    for validation_only_path in (
        "scripts/audit_release_artifacts.py",
        "scripts/install-workerbee.sh",
        "src/workerbee/profile_evidence.py",
        ".gitea/workflows/integration.yml",
        ".github/workflows/promote-main.yml",
        ".github/workflows/release-testpypi.yml",
        ".github/workflows/release-pypi.yml",
    ):
        assert f"`{validation_only_path}`" in text
    for gate in (
        "PIP_NO_INDEX=1",
        "PIP_CONFIG_FILE=/dev/null",
        "exactly-one-`ae`",
        "two byte-identity builds",
        "does not publish either Python distribution",
    ):
        assert gate in normalized


def test_installed_consumer_qualification_is_copyable_and_evidence_bounded() -> None:
    text = Path("docs/k1s-consumer-contract.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for command_field in (
        "scripts/qualify_installed_consumer.py",
        "--workerbee-size 798087",
        "--workerbee-sha256 78cde1ddbadaed962fc8514a7388024d464e6d3b1f3c7c016e5d115a10321ff9",
        "--workerbee-source-commit a51aeaace39f85d42076aaef0b28f7a92b4cf054",
        "--workerbee-source-tree 171a5f7f5ce744fc64a1e3b574a7aa98925850e7",
        "--k1s-size 1057213",
        "--k1s-sha256 f341d21a2b0297aaa598826b694fccbc2e07aedc6a449b259010d9e07f520d20",
        "--k1s-source-commit 0827108aa2ee429de491de0842d33871e84b9eba",
        "--k1s-source-tree 44b9aaab8073f5534f38655b29258b4eb1774f0c",
        "--runs 2",
    ):
        assert command_field in text
    for boundary in (
        "148e925de97e8131ff7f5d58be514c1c1aea6825fde0bd110f6e86db3715fac8",
        "0b33c0d9a28c093991e36c3962ce55de7b6f9b010ca950ba89ef906f15fedc59",
        "does not qualify live runtime behavior",
        "status` and `logs` remain source-validated",
        "strict-CRI prepare/rollback",
    ):
        assert boundary in normalized


def test_wiki_and_scenario_matrix_preserve_public_private_boundaries() -> None:
    contract = Path("docs/v1-contract.md").read_text(encoding="utf-8")
    wiki = Path("docs/wiki/WorkerBee-v1.md").read_text(encoding="utf-8")
    combined = " ".join((contract + "\n" + wiki).split())

    for scenario_id in (
        "WB-C2-INSTALL-001",
        "WB-D2-PROFILE-STATE-001",
        "WB-D2-STRICT-CRI-001",
        "WB-D3-CONTROLLED-RUN-001",
        "WB-D3-AGENT-PBX-001",
    ):
        assert scenario_id in contract
        assert scenario_id in wiki
    for boundary in (
        "Installed-artifact qualified",
        "Deferred live",
        "Deferred private qualification",
        "no package/consumer defect",
        "start receipt is coordination evidence only",
        "disabled by default",
        "private platform-debug",
        "not implemented or advertised as available by OSS",
    ):
        assert boundary.lower() in combined.lower()


def test_changed_public_docs_have_valid_repository_links_and_no_host_paths() -> None:
    paths = (
        Path("README.md"),
        Path("docs/k1s-consumer-contract.md"),
        Path("docs/releasing.md"),
        Path("docs/v1-contract.md"),
        Path("docs/wiki/WorkerBee-v1.md"),
    )
    link_pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")

    for source in paths:
        text = source.read_text(encoding="utf-8")
        assert "/home/" not in text
        for target in link_pattern.findall(text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            relative = target.split("#", 1)[0]
            if not relative:
                continue
            assert (source.parent / relative).resolve().is_file(), (source, target)
