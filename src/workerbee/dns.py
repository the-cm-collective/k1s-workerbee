"""Small LAN DNS server for explicit WorkerBee dev ingress."""

from __future__ import annotations

import os
import socket
import socketserver
import struct
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any

DNS_MODE_OFF = "off"
DNS_MODE_FORWARDING = "forwarding"
VALID_DNS_MODES = {DNS_MODE_OFF, DNS_MODE_FORWARDING}
DEFAULT_DNS_DOMAIN = "workerbee.home.arpa"
DEFAULT_DNS_PORT = 53
DEFAULT_DNS_TTL = 30
DNS_FORWARD_TIMEOUT_SECONDS = 2.0

TYPE_A = 1
TYPE_AAAA = 28
CLASS_IN = 1

RCODE_OK = 0
RCODE_FORMAT_ERROR = 1
RCODE_SERVER_FAILURE = 2
RCODE_REFUSED = 5


@dataclass(frozen=True, slots=True)
class DNSSettings:
    mode: str = DNS_MODE_OFF
    bind_host: str | None = None
    port: int = DEFAULT_DNS_PORT
    answer: str | None = None
    upstreams: tuple[str, ...] = field(default_factory=tuple)
    ttl: int = DEFAULT_DNS_TTL

    @property
    def enabled(self) -> bool:
        return self.mode != DNS_MODE_OFF

    def public_dict(self, *, running: bool | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "mode": self.mode,
            "bind_host": self.bind_host,
            "port": self.port,
            "answer": self.answer,
            "upstreams": list(self.upstreams),
            "ttl": self.ttl,
        }
        if running is not None:
            data["running"] = running
        return data


def resolve_dns_settings(
    *,
    exposure: str,
    bind_host: str,
    mode: str | None = None,
    port: int | str | None = None,
    dns_bind: str | None = None,
    answer: str | None = None,
    upstreams: list[str] | tuple[str, ...] | str | None = None,
) -> DNSSettings:
    selected_mode = mode
    if selected_mode is None:
        selected_mode = os.getenv("WORKERBEE_INGRESS_DNS")
    selected_mode = str(selected_mode or DNS_MODE_OFF).strip().lower()
    if selected_mode in {"0", "false", "no", "disabled", "disable"}:
        selected_mode = DNS_MODE_OFF
    if selected_mode in {"1", "true", "yes", "on", "enable", "enabled"}:
        selected_mode = DNS_MODE_FORWARDING
    if selected_mode not in VALID_DNS_MODES:
        expected = ", ".join(sorted(VALID_DNS_MODES))
        raise ValueError(
            f"invalid WorkerBee ingress DNS mode: {selected_mode}; expected {expected}"
        )
    if selected_mode == DNS_MODE_OFF:
        return DNSSettings()
    if exposure != "lan":
        raise ValueError("WorkerBee ingress DNS requires LAN ingress exposure")

    raw_port = port
    if raw_port is None:
        raw_port = _env_int("WORKERBEE_INGRESS_DNS_PORT")
    selected_port = int(raw_port or DEFAULT_DNS_PORT)
    if selected_port < 1 or selected_port > 65535:
        raise ValueError(f"invalid WorkerBee ingress DNS port: {selected_port}")

    selected_bind = dns_bind
    if selected_bind is None:
        selected_bind = os.getenv("WORKERBEE_INGRESS_DNS_BIND")
    if selected_bind in (None, ""):
        selected_bind = bind_host
    selected_bind = str(selected_bind).strip()
    if not selected_bind:
        raise ValueError("WorkerBee ingress DNS bind address cannot be empty")

    selected_answer = answer
    if selected_answer is None:
        selected_answer = os.getenv("WORKERBEE_INGRESS_DNS_ANSWER")
    if selected_answer in (None, ""):
        selected_answer = detect_lan_ip()
    if not selected_answer:
        raise ValueError(
            "could not determine a LAN IP for WorkerBee DNS; pass --ingress-dns-answer"
        )
    _validate_answer_ip(str(selected_answer))

    selected_upstreams = _normalize_upstreams(upstreams)
    if not selected_upstreams:
        selected_upstreams = tuple(_system_resolvers())
    if not selected_upstreams:
        raise ValueError(
            "WorkerBee DNS forwarding requires an upstream resolver; "
            "pass --ingress-dns-upstream"
        )
    return DNSSettings(
        mode=selected_mode,
        bind_host=selected_bind,
        port=selected_port,
        answer=str(selected_answer),
        upstreams=selected_upstreams,
    )


class WorkerBeeDNSServer:
    def __init__(
        self,
        *,
        settings: DNSSettings,
        base_domain: str,
    ) -> None:
        if not settings.enabled:
            raise ValueError("WorkerBee DNS server requires enabled DNS settings")
        self.settings = settings
        self.base_domain = base_domain.strip(".").lower()
        self._udp_server: _ThreadingUDPServer | None = None
        self._tcp_server: _ThreadingTCPServer | None = None
        self._threads: list[threading.Thread] = []

    def start(self) -> dict[str, Any]:
        host = str(self.settings.bind_host or "")
        port = int(self.settings.port)
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        udp_cls = _udp_server_class(address_family)
        tcp_cls = _tcp_server_class(address_family)
        try:
            self._udp_server = udp_cls((host, port), _WorkerBeeUDPHandler)
            self._udp_server.resolver = self
            self._tcp_server = tcp_cls((host, port), _WorkerBeeTCPHandler)
            self._tcp_server.resolver = self
        except Exception:
            self.stop()
            raise
        for name, server in (
            ("workerbee-dns-udp", self._udp_server),
            ("workerbee-dns-tcp", self._tcp_server),
        ):
            thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self.status(running=True)

    def stop(self) -> None:
        for server in (self._udp_server, self._tcp_server):
            if server is None:
                continue
            if self._threads:
                with suppress(Exception):
                    server.shutdown()
            with suppress(Exception):
                server.server_close()
        self._udp_server = None
        self._tcp_server = None
        self._threads = []

    def status(self, *, running: bool | None = None) -> dict[str, Any]:
        data = self.settings.public_dict(running=running)
        data["base_domain"] = self.base_domain
        return data

    def resolve(self, packet: bytes, client: tuple[Any, ...]) -> bytes | None:
        if not _client_allowed(str(client[0])):
            return _error_response(packet, RCODE_REFUSED)
        query = _parse_query(packet)
        if query is None:
            return _error_response(packet, RCODE_FORMAT_ERROR)
        if _matches_base_domain(query.qname, self.base_domain):
            return _local_response(
                packet,
                query=query,
                answer=str(self.settings.answer or ""),
                ttl=int(self.settings.ttl),
            )
        return _forward_query(packet, self.settings.upstreams)


@dataclass(frozen=True, slots=True)
class _DNSQuery:
    qname: str
    qtype: int
    qclass: int
    question_end: int


class _ThreadingUDPServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
    daemon_threads = True
    allow_reuse_address = True
    resolver: WorkerBeeDNSServer


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    resolver: WorkerBeeDNSServer


class _WorkerBeeUDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        packet, sock = self.request
        response = self.server.resolver.resolve(  # type: ignore[attr-defined]
            packet,
            self.client_address,
        )
        if response:
            sock.sendto(response, self.client_address)


class _WorkerBeeTCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        header = _recv_exact(self.request, 2)
        if not header:
            return
        length = struct.unpack("!H", header)[0]
        packet = _recv_exact(self.request, length)
        if not packet:
            return
        response = self.server.resolver.resolve(  # type: ignore[attr-defined]
            packet,
            self.client_address,
        )
        if response:
            self.request.sendall(struct.pack("!H", len(response)) + response)


def dns_port_available(settings: DNSSettings) -> dict[str, Any]:
    if not settings.enabled:
        return {"ok": True}
    host = str(settings.bind_host or "")
    port = int(settings.port)
    checks = [
        ("udp", socket.SOCK_DGRAM),
        ("tcp", socket.SOCK_STREAM),
    ]
    for protocol, sock_type in checks:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, sock_type) as sock:
                sock.bind((host, port))
        except OSError as exc:
            return {
                "ok": False,
                "error": {
                    "code": "INGRESS_DNS_PORT_IN_USE",
                    "message": f"WorkerBee DNS {protocol} port {host}:{port} is unavailable",
                    "details": {
                        "host": host,
                        "port": port,
                        "protocol": protocol,
                        "error": str(exc),
                    },
                    "retryable": True,
                    "remediation": (
                        "Stop the process using this DNS port, run as a user allowed to bind "
                        "the port, pass --ingress-dns-bind <LAN-IP>, or choose a different "
                        "--ingress-dns-port for clients that support custom DNS ports."
                    ),
                },
            }
    return {"ok": True}


def detect_lan_ip() -> str | None:
    candidates: list[str] = []
    with suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        candidates.append(str(sock.getsockname()[0]))
    with suppress(OSError):
        for family, _type, _proto, _canon, address in socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        ):
            if family == socket.AF_INET and address:
                candidates.append(str(address[0]))
    for candidate in candidates:
        with suppress(ValueError):
            parsed = ip_address(candidate)
            if parsed.version == 4 and not parsed.is_loopback and not parsed.is_unspecified:
                return candidate
    return None


def _parse_query(packet: bytes) -> _DNSQuery | None:
    if len(packet) < 12:
        return None
    _ident, _flags, qdcount, _ancount, _nscount, _arcount = struct.unpack("!HHHHHH", packet[:12])
    if qdcount < 1:
        return None
    name, offset = _decode_name(packet, 12)
    if name is None or offset + 4 > len(packet):
        return None
    qtype, qclass = struct.unpack("!HH", packet[offset : offset + 4])
    return _DNSQuery(
        qname=name,
        qtype=qtype,
        qclass=qclass,
        question_end=offset + 4,
    )


def _decode_name(packet: bytes, offset: int) -> tuple[str | None, int]:
    labels: list[str] = []
    jumped = False
    original_offset = offset
    seen: set[int] = set()
    while True:
        if offset >= len(packet):
            return None, original_offset
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                return None, original_offset
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            if pointer in seen:
                return None, original_offset
            seen.add(pointer)
            if not jumped:
                original_offset = offset + 2
            offset = pointer
            jumped = True
            continue
        if length & 0xC0:
            return None, original_offset
        offset += 1
        if length == 0:
            return ".".join(labels).lower(), (original_offset if jumped else offset)
        if offset + length > len(packet):
            return None, original_offset
        try:
            labels.append(packet[offset : offset + length].decode("ascii"))
        except UnicodeDecodeError:
            return None, original_offset
        offset += length


def _local_response(packet: bytes, *, query: _DNSQuery, answer: str, ttl: int) -> bytes:
    if query.qclass != CLASS_IN:
        return _response(packet, query=query, answers=[], rcode=RCODE_OK, authoritative=True)
    with suppress(ValueError):
        parsed = ip_address(answer)
        if query.qtype == TYPE_A and parsed.version == 4:
            return _response(
                packet,
                query=query,
                answers=[_answer_record(TYPE_A, ttl, parsed.packed)],
                rcode=RCODE_OK,
                authoritative=True,
            )
        if query.qtype == TYPE_AAAA and parsed.version == 6:
            return _response(
                packet,
                query=query,
                answers=[_answer_record(TYPE_AAAA, ttl, parsed.packed)],
                rcode=RCODE_OK,
                authoritative=True,
            )
    return _response(packet, query=query, answers=[], rcode=RCODE_OK, authoritative=True)


def _response(
    packet: bytes,
    *,
    query: _DNSQuery,
    answers: list[bytes],
    rcode: int,
    authoritative: bool,
) -> bytes:
    ident, flags, _qdcount, _ancount, _nscount, _arcount = struct.unpack("!HHHHHH", packet[:12])
    response_flags = 0x8000 | (flags & 0x0100) | 0x0080 | (rcode & 0x000F)
    if authoritative:
        response_flags |= 0x0400
    header = struct.pack("!HHHHHH", ident, response_flags, 1, len(answers), 0, 0)
    question = packet[12 : query.question_end]
    return header + question + b"".join(answers)


def _answer_record(record_type: int, ttl: int, payload: bytes) -> bytes:
    return (
        b"\xc0\x0c"
        + struct.pack("!HHIH", record_type, CLASS_IN, int(ttl), len(payload))
        + payload
    )


def _error_response(packet: bytes, rcode: int) -> bytes | None:
    query = _parse_query(packet)
    if query is None:
        if len(packet) < 2:
            return None
        ident = struct.unpack("!H", packet[:2])[0]
        return struct.pack("!HHHHHH", ident, 0x8000 | (rcode & 0x000F), 0, 0, 0, 0)
    return _response(packet, query=query, answers=[], rcode=rcode, authoritative=False)


def _forward_query(packet: bytes, upstreams: tuple[str, ...]) -> bytes:
    for upstream in upstreams:
        host, port = _parse_upstream(upstream)
        try:
            return _forward_udp(packet, host=host, port=port)
        except OSError:
            continue
    response = _error_response(packet, RCODE_SERVER_FAILURE)
    return response or b""


def _forward_udp(packet: bytes, *, host: str, port: int) -> bytes:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    last_error: OSError | None = None
    for family, sock_type, proto, _canon, sockaddr in infos:
        try:
            with socket.socket(family, sock_type, proto) as sock:
                sock.settimeout(DNS_FORWARD_TIMEOUT_SECONDS)
                sock.sendto(packet, sockaddr)
                response, _address = sock.recvfrom(4096)
                return response
        except OSError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise OSError(f"no DNS upstream address for {host}:{port}")


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return b""
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _matches_base_domain(qname: str, base_domain: str) -> bool:
    name = qname.strip(".").lower()
    base = base_domain.strip(".").lower()
    return name == base or name.endswith(f".{base}")


def _client_allowed(raw: str) -> bool:
    with suppress(ValueError):
        parsed = ip_address(raw)
        return not parsed.is_global and not parsed.is_multicast and not parsed.is_unspecified
    return False


def _normalize_upstreams(
    upstreams: list[str] | tuple[str, ...] | str | None,
) -> tuple[str, ...]:
    if upstreams is None:
        env_raw = os.getenv("WORKERBEE_INGRESS_DNS_UPSTREAM")
        upstreams = env_raw if env_raw else None
    if upstreams is None:
        return ()
    if isinstance(upstreams, str):
        values = upstreams.replace(",", " ").split()
    else:
        values = [
            item
            for value in upstreams
            for item in str(value).replace(",", " ").split()
        ]
    return tuple(value.strip() for value in values if value.strip())


def _system_resolvers() -> list[str]:
    resolvers: list[str] = []
    path = Path("/etc/resolv.conf")
    with suppress(OSError):
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("nameserver "):
                continue
            parts = line.split()
            if len(parts) >= 2:
                resolvers.append(parts[1])
    return resolvers


def _parse_upstream(raw: str) -> tuple[str, int]:
    value = raw.strip()
    if value.startswith("[") and "]" in value:
        host, _, rest = value[1:].partition("]")
        if rest.startswith(":"):
            return host, int(rest[1:])
        return host, DEFAULT_DNS_PORT
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            return host, int(port)
    return value, DEFAULT_DNS_PORT


def _validate_answer_ip(raw: str) -> None:
    try:
        parsed = ip_address(raw)
    except ValueError as exc:
        raise ValueError(f"invalid WorkerBee ingress DNS answer IP: {raw}") from exc
    if parsed.is_unspecified:
        raise ValueError("WorkerBee ingress DNS answer IP cannot be unspecified")


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw in (None, ""):
        return None
    return int(raw)


def _udp_server_class(address_family: socket.AddressFamily) -> type[_ThreadingUDPServer]:
    class Server(_ThreadingUDPServer):
        pass

    Server.address_family = address_family
    return Server


def _tcp_server_class(address_family: socket.AddressFamily) -> type[_ThreadingTCPServer]:
    class Server(_ThreadingTCPServer):
        pass

    Server.address_family = address_family
    return Server
