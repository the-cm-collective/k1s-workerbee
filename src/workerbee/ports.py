"""Port allocation helpers."""

from __future__ import annotations

import socket
from contextlib import closing


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True


def choose_port(preferred: int, *, start: int, end: int, host: str = "127.0.0.1") -> int:
    if port_is_free(preferred, host):
        return int(preferred)
    for port in range(int(start), int(end) + 1):
        if port_is_free(port, host):
            return port
    raise RuntimeError(f"no free port found in {start}-{end}")

