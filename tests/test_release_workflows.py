from __future__ import annotations

import re
from pathlib import Path

RELEASE_WORKFLOWS = (
    Path(".github/workflows/release-testpypi.yml"),
    Path(".github/workflows/release-pypi.yml"),
)
PROMOTION_WORKFLOW = Path(".github/workflows/promote-main.yml")
GITEA_WORKFLOW = Path(".gitea/workflows/integration.yml")
ALL_WORKFLOWS = (*RELEASE_WORKFLOWS, PROMOTION_WORKFLOW, GITEA_WORKFLOW)


def test_gitea_integration_lane_cannot_publish() -> None:
    text = GITEA_WORKFLOW.read_text(encoding="utf-8")
    forbidden = (
        "workflow_dispatch:",
        "id-token: write",
        "gh-action-pypi-publish",
        "twine upload",
        "repository-url:",
        "packages-dir:",
        "pypi.org/legacy",
        "secrets.",
        "release-testpypi.yml",
        "release-pypi.yml",
    )
    assert "contents: read" in text
    assert "branches: [dev]" in text
    assert not re.search(r"^\s+[-\w]+:\s+write\s*$", text, re.MULTILINE)
    assert all(marker not in text for marker in forbidden)


def test_release_actions_are_pinned_to_full_commit_shas() -> None:
    for workflow in ALL_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        uses = re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE)
        assert uses
        for action in uses:
            assert re.search(r"@[0-9a-f]{40}$", action), f"unpinned action in {workflow}: {action}"


def test_release_workflows_are_manual_only_and_default_to_no_publish() -> None:
    for workflow in RELEASE_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert "workflow_dispatch:" in text
        assert "default: false" in text
        assert not re.search(r"^\s{2}(push|pull_request|release|schedule):", text, re.MULTILINE)
        assert "id-token: write" in text
        assert "skip-existing: false" in text
        assert "extra-index-url" not in text
        assert "python scripts/audit_release_artifacts.py verify-project" in text


def test_testpypi_workflow_builds_twice_and_publishes_only_verified_bytes() -> None:
    text = RELEASE_WORKFLOWS[0].read_text(encoding="utf-8")
    assert text.count("build_release_candidate.sh") == 2
    assert "cmp dist/candidate-a/RELEASE-MANIFEST.json" in text
    assert "if: ${{ inputs.publish }}" in text
    assert "https://test.pypi.org/legacy/" in text
    assert "fetch-index" in text


def test_production_workflow_promotes_without_rebuilding() -> None:
    text = RELEASE_WORKFLOWS[1].read_text(encoding="utf-8")
    assert "candidate_run_id" in text
    assert "run-id: ${{ inputs.candidate_run_id }}" in text
    assert "build_release_candidate" not in text
    assert "python -m build" not in text
    assert "https://test.pypi.org/pypi" in text
    assert "https://pypi.org/pypi" in text
    assert "if: ${{ inputs.publish }}" in text


def test_release_jobs_require_publication_ready_contract_before_oidc_action() -> None:
    for workflow in RELEASE_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        gate = text.index("verify-publication-ready")
        publisher = text.index("pypa/gh-action-pypi-publish@")
        assert gate < publisher
        assert "password:" not in text
        assert "PYPI_API_TOKEN" not in text


def test_main_promotion_is_read_only_dev_to_main_review_gate() -> None:
    text = PROMOTION_WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request:" in text
    assert "branches: [main]" in text
    assert 'test "$BASE_REF" = main' in text
    assert 'test "$HEAD_REF" = dev' in text
    assert 'test "$HEAD_REPO" = "$REPOSITORY"' in text
    assert 'test "$(git rev-parse HEAD)" = "$HEAD_SHA"' in text
    assert 'git merge-base --is-ancestor "$BASE_SHA" "$HEAD_SHA"' in text
    assert "persist-credentials: false" in text
    assert "contents: read" in text
    assert "id-token: write" not in text
    assert not re.search(r"^\s+[-\w]+:\s+write\s*$", text, re.MULTILINE)
    assert not re.search(r"^\s{2}(push|workflow_dispatch|schedule):", text, re.MULTILINE)
    assert "git push" not in text


def test_integration_and_promotion_commands_are_host_safe() -> None:
    forbidden = (
        "sudo ",
        "microk8s",
        "/run/containerd",
        "nerdctl",
        "podman",
        "docker",
        "kubectl",
        "systemctl",
        "workerbee mcp",
        "workerbee_v1_",
        "scripts/dev/",
        "manifest deploy",
        "profile start",
        "profile stop",
    )
    for workflow in (GITEA_WORKFLOW, PROMOTION_WORKFLOW):
        text = workflow.read_text(encoding="utf-8").lower()
        assert all(marker not in text for marker in forbidden)
