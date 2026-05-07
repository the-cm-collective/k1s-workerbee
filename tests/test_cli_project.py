from pathlib import Path

from workerbee.cli import build_parser


def test_project_mode_accepts_cwd_project_and_open(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "project",
            "mode",
            "lazy",
            "--cwd",
            str(tmp_path),
            "--project",
            "Demo App",
            "--open",
        ]
    )

    assert args.cmd == "project"
    assert args.project_cmd == "mode"
    assert args.mode == "lazy"
    assert args.project_cwd == tmp_path
    assert args.project_name == "Demo App"
    assert args.open is True


def test_mcp_start_accepts_background_bind_flags(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--state-root",
            str(tmp_path),
            "--runtime",
            "containerd",
            "mcp",
            "start",
            "--host",
            "127.0.0.1",
            "--port",
            "9999",
            "--timeout",
            "1",
        ]
    )

    assert args.cmd == "mcp"
    assert args.mcp_cmd == "start"
    assert args.runtime == "containerd"
    assert args.state_root == tmp_path
    assert args.host == "127.0.0.1"
    assert args.port == 9999
    assert args.timeout == 1
