from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = (
    Path(".github/workflows/release-testpypi.yml"),
    Path(".github/workflows/release-pypi.yml"),
)


def test_release_actions_are_pinned_to_full_commit_shas() -> None:
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        uses = re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE)
        assert uses
        for action in uses:
            assert re.search(r"@[0-9a-f]{40}$", action), f"unpinned action in {workflow}: {action}"


def test_release_workflows_are_manual_only_and_default_to_no_publish() -> None:
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert "workflow_dispatch:" in text
        assert "default: false" in text
        assert not re.search(r"^\s{2}(push|pull_request|release|schedule):", text, re.MULTILINE)
        assert "id-token: write" in text
        assert "skip-existing: false" in text
        assert "extra-index-url" not in text


def test_testpypi_workflow_builds_twice_and_publishes_only_verified_bytes() -> None:
    text = WORKFLOWS[0].read_text(encoding="utf-8")
    assert text.count("build_release_candidate.sh") == 2
    assert "cmp dist/candidate-a/RELEASE-MANIFEST.json" in text
    assert "if: ${{ inputs.publish }}" in text
    assert "https://test.pypi.org/legacy/" in text
    assert "fetch-index" in text


def test_production_workflow_promotes_without_rebuilding() -> None:
    text = WORKFLOWS[1].read_text(encoding="utf-8")
    assert "candidate_run_id" in text
    assert "run-id: ${{ inputs.candidate_run_id }}" in text
    assert "build_release_candidate" not in text
    assert "python -m build" not in text
    assert "https://test.pypi.org/pypi" in text
    assert "https://pypi.org/pypi" in text
    assert "if: ${{ inputs.publish }}" in text
