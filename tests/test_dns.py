from __future__ import annotations

import socketserver
import struct
import threading

from workerbee import dns as dns_module
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


def test_workerbee_dns_local_unsupported_type_stays_noerror_empty() -> None:
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
        _query("app.demo.workerbee.home.arpa", qtype=16),
        ("192.168.1.50", 52123),
    )

    assert _rcode(response or b"") == 0
    assert _answer_count(response or b"") == 0


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


def test_workerbee_dns_prefers_later_positive_over_first_nxdomain(monkeypatch) -> None:
    calls: list[str] = []

    def fake_forward(packet: bytes, *, host: str, port: int) -> bytes:
        _ = port
        calls.append(host)
        if host == "router":
            return _nxdomain_response(packet)
        return _a_response(packet, "104.237.148.74")

    monkeypatch.setattr(dns_module, "_forward_udp", fake_forward)
    server = _forwarding_server(("router", "public"))

    response = server.resolve(
        _query("workerbee.preview.cosmosmechane.com"),
        ("192.168.1.50", 52123),
    )

    assert calls == ["router", "public"]
    answer = _first_answer(response or b"")
    assert answer["rdata"] == b"\x68\xed\x94\x4a"


def test_workerbee_dns_prefers_later_positive_over_first_empty_answer(monkeypatch) -> None:
    calls: list[str] = []

    def fake_forward(packet: bytes, *, host: str, port: int) -> bytes:
        _ = port
        calls.append(host)
        if host == "router":
            return _empty_response(packet)
        return _a_response(packet, "104.237.148.74")

    monkeypatch.setattr(dns_module, "_forward_udp", fake_forward)
    server = _forwarding_server(("router", "public"))

    response = server.resolve(
        _query("workerbee.preview.cosmosmechane.com"),
        ("192.168.1.50", 52123),
    )

    assert calls == ["router", "public"]
    assert _first_answer(response or b"")["rdata"] == b"\x68\xed\x94\x4a"


def test_workerbee_dns_returns_nxdomain_when_all_upstreams_nxdomain(monkeypatch) -> None:
    def fake_forward(packet: bytes, *, host: str, port: int) -> bytes:
        _ = host, port
        return _nxdomain_response(packet)

    monkeypatch.setattr(dns_module, "_forward_udp", fake_forward)
    server = _forwarding_server(("router", "public"))

    response = server.resolve(
        _query("missing.preview.cosmosmechane.com"),
        ("192.168.1.50", 52123),
    )

    assert _rcode(response or b"") == 3
    assert _answer_count(response or b"") == 0


def test_workerbee_dns_continues_after_upstream_error(monkeypatch) -> None:
    calls: list[str] = []

    def fake_forward(packet: bytes, *, host: str, port: int) -> bytes:
        _ = port
        calls.append(host)
        if host == "router":
            raise OSError("timeout")
        return _a_response(packet, "104.237.148.74")

    monkeypatch.setattr(dns_module, "_forward_udp", fake_forward)
    server = _forwarding_server(("router", "public"))

    response = server.resolve(
        _query("workerbee.preview.cosmosmechane.com"),
        ("192.168.1.50", 52123),
    )

    assert calls == ["router", "public"]
    assert _first_answer(response or b"")["rdata"] == b"\x68\xed\x94\x4a"


def test_workerbee_dns_returns_servfail_when_no_upstream_is_usable(monkeypatch) -> None:
    def fake_forward(packet: bytes, *, host: str, port: int) -> bytes:
        _ = packet, host, port
        raise OSError("timeout")

    monkeypatch.setattr(dns_module, "_forward_udp", fake_forward)
    server = _forwarding_server(("router", "public"))

    response = server.resolve(
        _query("workerbee.preview.cosmosmechane.com"),
        ("192.168.1.50", 52123),
    )

    assert _rcode(response or b"") == 2
    assert _answer_count(response or b"") == 0


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


def test_dns_public_dict_describes_forwarding_fallback_policy() -> None:
    settings = DNSSettings(
        mode="forwarding",
        bind_host="127.0.0.1",
        port=1053,
        answer="192.168.1.23",
        upstreams=("192.168.29.1", "1.1.1.1"),
    )

    public = settings.public_dict(running=True)

    assert public["upstreams"] == ["192.168.29.1", "1.1.1.1"]
    assert public["forwarding_policy"] == {
        "strategy": "try-all-prefer-positive",
        "negative_fallback_order": ["noerror-empty", "nxdomain"],
        "on_all_upstream_errors": "servfail",
        "query_names_logged": False,
    }


def _forwarding_server(upstreams: tuple[str, ...]) -> WorkerBeeDNSServer:
    return WorkerBeeDNSServer(
        settings=DNSSettings(
            mode="forwarding",
            bind_host="127.0.0.1",
            port=1053,
            answer="192.168.1.23",
            upstreams=upstreams,
        ),
        base_domain="workerbee.home.arpa",
    )


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
    return _response(packet, flags=0x8180, answers=[])


def _nxdomain_response(packet: bytes) -> bytes:
    return _response(packet, flags=0x8183, answers=[])


def _a_response(packet: bytes, address: str) -> bytes:
    payload = bytes(int(part) for part in address.split("."))
    answer = (
        b"\xc0\x0c"
        + struct.pack("!HHIH", 1, 1, 30, len(payload))
        + payload
    )
    return _response(packet, flags=0x8180, answers=[answer])


def _response(packet: bytes, *, flags: int, answers: list[bytes]) -> bytes:
    question = packet[12:]
    ident = struct.unpack("!H", packet[:2])[0]
    return (
        struct.pack("!HHHHHH", ident, flags, 1, len(answers), 0, 0)
        + question
        + b"".join(answers)
    )


def _rcode(packet: bytes) -> int:
    _ident, flags, _qdcount, _ancount, _nscount, _arcount = struct.unpack(
        "!HHHHHH",
        packet[:12],
    )
    return flags & 0x000F


def _answer_count(packet: bytes) -> int:
    _ident, _flags, _qdcount, ancount, _nscount, _arcount = struct.unpack(
        "!HHHHHH",
        packet[:12],
    )
    return ancount


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
