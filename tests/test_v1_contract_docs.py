from pathlib import Path

from workerbee.agent import agent_instructions_markdown


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
        "k1s-core==0.1.8a1",
        "temporary and nonpublished",
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
    assert "workerbee-install/releases" in readme
