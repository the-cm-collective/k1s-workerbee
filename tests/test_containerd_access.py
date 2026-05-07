from __future__ import annotations

from pathlib import Path

from workerbee.containerd_access import release_containerd_socket_access


def test_release_keeps_acl_when_another_active_lease_exists(tmp_path: Path, monkeypatch) -> None:
    lease_file = tmp_path / "global" / "containerd-socket-access.json"
    lease_file.parent.mkdir(parents=True)
    lease_file.write_text(
        (
            '{"leases": ['
            '{"id": "old", "pid": 100, "socket": "/run/containerd/containerd.sock", '
            '"user": "dev", "added_acl": true, "preexisting_access": false},'
            '{"id": "peer", "pid": 200, "socket": "/run/containerd/containerd.sock", '
            '"user": "dev", "added_acl": true, "preexisting_access": false}'
            "]}"
        ),
        encoding="utf-8",
    )
    acl_calls: list[list[str]] = []
    monkeypatch.setattr("workerbee.containerd_access._pid_alive", lambda pid: pid == 200)
    monkeypatch.setattr(
        "workerbee.containerd_access._run_acl",
        lambda args, **_kwargs: acl_calls.append(args),
    )

    result = release_containerd_socket_access(
        state_root=tmp_path,
        lease_id="old",
        force=True,
    )

    assert result["ok"] is True
    assert result["deprecated"] is True
    assert not acl_calls
    assert result["actions"][0]["action"] == "keep"


def test_release_skips_revoke_when_socket_is_missing(tmp_path: Path, monkeypatch) -> None:
    lease_file = tmp_path / "global" / "containerd-socket-access.json"
    lease_file.parent.mkdir(parents=True)
    lease_file.write_text(
        (
            '{"leases": ['
            '{"id": "old", "pid": 100, "socket": "/tmp/missing-containerd.sock", '
            '"user": "dev", "added_acl": true, "preexisting_access": false}'
            "]}"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("workerbee.containerd_access._pid_alive", lambda _pid: False)

    result = release_containerd_socket_access(state_root=tmp_path)

    assert result["ok"] is True
    assert result["released"] is True
    assert result["actions"][0]["action"] == "skip"
