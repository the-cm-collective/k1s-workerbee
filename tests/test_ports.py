from __future__ import annotations

import socket
from contextlib import closing

from workerbee.ports import choose_port, port_is_free


def _wildcard_listener() -> socket.socket:
    """A foreign controller listening on the wildcard address (``0.0.0.0:PORT``)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))  # noqa: S104 - intentional wildcard bind for the test
    sock.listen()
    return sock


def _ephemeral_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_port_is_free_detects_wildcard_listener() -> None:
    # Before the fix this returns True on macOS/BSD because SO_REUSEADDR lets a bind
    # to 127.0.0.1:PORT succeed while a process LISTENs on 0.0.0.0:PORT.
    with closing(_wildcard_listener()) as listener:
        port = int(listener.getsockname()[1])
        assert port_is_free(port) is False
        assert port_is_free(port, host="0.0.0.0") is False  # noqa: S104


def test_port_is_free_true_when_unbound() -> None:
    assert port_is_free(_ephemeral_free_port()) is True


def test_choose_port_skips_wildcard_listener() -> None:
    with closing(_wildcard_listener()) as listener:
        occupied = int(listener.getsockname()[1])
        chosen = choose_port(occupied, start=occupied, end=occupied + 64)
        assert chosen != occupied
        assert port_is_free(chosen) is True
