from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

PUBLISHED_COMMIT = "ab0c39dd4fd49ea18e0195be4db580dc4f7fa94c"
PUBLISHED_TREE = "945de1b550c9244049b3da9a2bc2ab81d9a31fa6"
PUBLISHED_PARENT = "8a68c6b7ac863765579a4f5bd4e4d4228bf23053"
PUBLISHED_TAG_OBJECT = "2bac976794ecb61f53f0f1f4568ee22debeacd57"

CANDIDATES = (
    (
        "cd61ffde26a1bb466c5f2099882b0276b97ab228",
        "cd3e303439debe3d6abe66c4c9c32520988fe5aa",
        PUBLISHED_COMMIT,
    ),
    (
        "b0e3e4943d3f624b3a52db5fce4ade003e421ec1",
        "d05d955cd7f2277ec4aa88d571d867438eef1cba",
        "cd61ffde26a1bb466c5f2099882b0276b97ab228",
    ),
    (
        "6a205c75d993712f8e526b111b5c14c1c2f5cce4",
        "b8009383204141a847351dfcbb588e966f99bb3f",
        "b0e3e4943d3f624b3a52db5fce4ade003e421ec1",
    ),
    (
        "a742080177fd3f141ba1520a1a9af16fb5dafc5a",
        "8e0c784d9629a98fbda64facdab786f8fda74de8",
        "6a205c75d993712f8e526b111b5c14c1c2f5cce4",
    ),
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def test_published_v017_receipt_matches_repository_objects() -> None:
    assert _git("rev-parse", "v0.1.7") == PUBLISHED_TAG_OBJECT
    assert _git("rev-parse", "v0.1.7^{commit}") == PUBLISHED_COMMIT
    assert _git("rev-parse", "v0.1.7^{tree}") == PUBLISHED_TREE
    assert _git("show", "-s", "--format=%P", PUBLISHED_COMMIT) == PUBLISHED_PARENT

    baseline = tomllib.loads(_git("show", f"{PUBLISHED_COMMIT}:pyproject.toml"))
    assert baseline["project"]["name"] == "k1s-workerbee"
    assert baseline["project"]["version"] == "0.1.7"


def test_four_candidate_receipts_form_one_preserved_lineage() -> None:
    for commit, tree, parent in CANDIDATES:
        assert _git("show", "-s", "--format=%T", commit) == tree
        assert _git("show", "-s", "--format=%P", commit) == parent
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", PUBLISHED_COMMIT, "HEAD"],
        check=True,
    )


def test_controlled_run_and_native_direct_commits_remain_distinct_evidence() -> None:
    controlled_paths = set(
        _git(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "6a205c75d993712f8e526b111b5c14c1c2f5cce4",
        ).splitlines()
    )
    native_paths = set(
        _git(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "a742080177fd3f141ba1520a1a9af16fb5dafc5a",
        ).splitlines()
    )

    assert controlled_paths == {
        "README.md",
        "docs/controlled-runs.md",
        "src/workerbee/controlled_runs.py",
        "src/workerbee/daemon.py",
        "src/workerbee/mcp_server.py",
        "tests/test_controlled_runs.py",
    }
    assert native_paths == {
        "README.md",
        "docs/native-direct-interoperability.md",
        "src/workerbee/agent.py",
        "src/workerbee/daemon.py",
        "tests/test_native_direct_interop.py",
    }


def test_release_foundation_document_records_every_receipt() -> None:
    text = Path("docs/release-foundation.md").read_text(encoding="utf-8")
    for identity in (
        PUBLISHED_TAG_OBJECT,
        PUBLISHED_COMMIT,
        PUBLISHED_TREE,
        PUBLISHED_PARENT,
        *(value for receipt in CANDIDATES for value in receipt),
    ):
        assert identity in text
    assert "no `main` ref advertised" in text
    assert "does not fetch or create those refs" in text
