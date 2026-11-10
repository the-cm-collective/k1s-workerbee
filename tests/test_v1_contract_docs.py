import argparse
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
        "k1s-core==1.0.0a3",
        "published provider artifact",
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


def test_release_docs_record_exact_future_k1s_rc1_rebind_inventory() -> None:
    text = Path("docs/releasing.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "### Future `k1s-core==1.0.0rc1` rebind inventory" in text
    assert "not an accepted artifact" in normalized
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
        "does not by itself create `k1s-workerbee==1.0.0rc1`",
    ):
        assert gate in text
