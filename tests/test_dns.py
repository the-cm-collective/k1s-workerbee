from __future__ import annotations

import socketserver
import struct
import threading

from workerbee.dns import DNSSettings, WorkerBeeDNSServer
from workerbee.ingress import resolve_ingress_settings


def test_workerbee_dns_answers_workerbee_domain_a_records() -> None:
    server = WorkerBeeDNSServer(
        settings=DNSSettings(
            mode="forwarding",
            bind_host="127.0.0.1",
            port=1053,
            answer="192.168.1.23",
            upstreams=("127.0.0.1",),
        ),
        base_domain="workerbee.home.arpa",
    )

    response = server.resolve(
        _query("app.demo.workerbee.home.arpa"),
        ("192.168.1.50", 52123),
    )

    answer = _first_answer(response or b"")
    assert answer["type"] == 1
    assert answer["class"] == 1
    assert answer["ttl"] == 30
    assert answer["rdata"] == b"\xc0\xa8\x01\x17"


def test_workerbee_dns_forwards_non_workerbee_names_to_upstream() -> None:
    calls: list[bytes] = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            packet, sock = self.request
            calls.append(packet)
            sock.sendto(_empty_response(packet), self.client_address)

    upstream = socketserver.UDPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(upstream.server_address[1])
        server = WorkerBeeDNSServer(
            settings=DNSSettings(
                mode="forwarding",
                bind_host="127.0.0.1",
                port=1053,
                answer="192.168.1.23",
                upstreams=(f"127.0.0.1:{port}",),
            ),
            base_domain="workerbee.home.arpa",
        )

        response = server.resolve(_query("example.com"), ("192.168.1.50", 52123))
    finally:
        upstream.shutdown()
        upstream.server_close()

    assert calls
    assert response == _empty_response(calls[0])


def test_workerbee_dns_refuses_public_clients() -> None:
    server = WorkerBeeDNSServer(
        settings=DNSSettings(
            mode="forwarding",
            bind_host="127.0.0.1",
            port=1053,
            answer="192.168.1.23",
            upstreams=("127.0.0.1",),
        ),
        base_domain="workerbee.home.arpa",
    )

    response = server.resolve(_query("app.demo.workerbee.home.arpa"), ("8.8.8.8", 52123))

    _ident, flags, _qdcount, ancount, _nscount, _arcount = struct.unpack(
        "!HHHHHH",
        response[:12],
    )
    assert flags & 0x000F == 5
    assert ancount == 0


def test_dns_enabled_lan_ingress_defaults_to_home_arpa(monkeypatch) -> None:
    monkeypatch.setattr("workerbee.dns.detect_lan_ip", lambda: "192.168.1.23")

    settings = resolve_ingress_settings(
        exposure="lan",
        dns_mode="forwarding",
        dns_upstreams=("127.0.0.1",),
    )

    assert settings.base_domain == "workerbee.home.arpa"
    assert settings.dns.enabled is True
    assert settings.dns.answer == "192.168.1.23"


def _query(name: str, qtype: int = 1) -> bytes:
    labels = b"".join(
        bytes([len(part)]) + part.encode("ascii")
        for part in name.strip(".").split(".")
    )
    return (
        struct.pack("!HHHHHH", 0xBEEF, 0x0100, 1, 0, 0, 0)
        + labels
        + b"\x00"
        + struct.pack("!HH", qtype, 1)
    )


def _empty_response(packet: bytes) -> bytes:
    question = packet[12:]
    return struct.pack("!HHHHHH", 0xBEEF, 0x8180, 1, 0, 0, 0) + question


def _first_answer(packet: bytes) -> dict[str, object]:
    _ident, _flags, _qdcount, ancount, _nscount, _arcount = struct.unpack(
        "!HHHHHH",
        packet[:12],
    )
    assert ancount == 1
    offset = 12
    while packet[offset] != 0:
        offset += packet[offset] + 1
    offset += 5
    assert packet[offset : offset + 2] == b"\xc0\x0c"
    rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", packet[offset + 2 : offset + 12])
    rdata = packet[offset + 12 : offset + 12 + rdlen]
    return {"type": rtype, "class": rclass, "ttl": ttl, "rdata": rdata}
