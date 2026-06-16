"""Port allocation helpers."""

from __future__ import annotations

import socket
from contextlib import closing


def _has_active_listener(port: int, host: str) -> bool:
    probe_hosts = {"127.0.0.1"}
    if host not in {"", "0.0.0.0"}:  # noqa: S104 - wildcard maps to loopback for probing.
        probe_hosts.add(host)
    for probe_host in probe_hosts:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.settimeout(0.2)
            try:
                if sock.connect_ex((probe_host, port)) == 0:
                    return True
            except OSError:
                continue
    return False


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    port = int(port)
    if _has_active_listener(port, host):
        return False
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(
    preferred: int,
    *,
    start: int,
    end: int,
    host: str = "127.0.0.1",
    reserved: set[int] | None = None,
    blocked: set[int] | None = None,
) -> int:
    reserved_ports = reserved if reserved is not None else set()
    blocked_ports = blocked if blocked is not None else set()
    if int(preferred) not in reserved_ports | blocked_ports and port_is_free(preferred, host):
        reserved_ports.add(int(preferred))
        return int(preferred)
    for port in range(int(start), int(end) + 1):
        if port in reserved_ports or port in blocked_ports:
            continue
        if port_is_free(port, host):
            reserved_ports.add(port)
            return port
    raise RuntimeError(f"no free port found in {start}-{end}")
