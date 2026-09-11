"""Bounded direct HTTPS reads for the OpenSIPS placement consumer.

Only fixed JSON GET contracts are supported. PostgreSQL remains authoritative for
placement and recovery decisions; these observations are expiring input evidence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_address
import json
import re
import socket
import ssl
from urllib.parse import urlsplit

SocketAddress = tuple[str, int] | tuple[str, int, int, int]
DnsResult = list[tuple[int, int, int, str, SocketAddress]]


class ObservationUnavailable(RuntimeError):
    """A credential-safe failure; the caller must withdraw this observation."""


@dataclass(frozen=True)
class JsonReply:
    status: int
    body: bytes
    request_started: float
    completed_at: float


@dataclass(frozen=True)
class RoutingNode:
    node_id: str
    incarnation_id: str
    gateway_generation_id: str
    freeswitch_generation_id: str
    state_version: int
    valid_for_ms: int
    channel_limit: int
    load_origin: str
    load_secret_arn: str
    load_secret_version: str
    siprec_target: str
    voice_target: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class PlacementInventory:
    nodes: tuple[RoutingNode, ...]
    request_started: float
    completed_at: float


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate observation JSON member")
        result[key] = value
    return result


def _integer(value: object, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("invalid observation integer")
    return value


def _identifier(value: object, pattern: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("invalid observation identity")
    return value


def _sip_target(value: object) -> str:
    if not isinstance(value, dict) or set(value) != {"address", "port", "transport"}:
        raise ValueError("invalid SIP target")
    if not isinstance(value["address"], str):
        raise ValueError("SIP address must be explicit text")
    address = ip_address(value["address"])
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or getattr(address, "ipv4_mapped", None) is not None:
        raise ValueError("invalid SIP target address")
    port = _integer(value["port"], 1, 65535)
    transport = value["transport"]
    if transport not in {"udp", "tcp", "tls"}:
        raise ValueError("invalid SIP transport")
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"sip:{host}:{port};transport={transport}"


def _routing_node(value: object, secret_prefix: str) -> RoutingNode | None:
    if not isinstance(value, dict) or set(value) != {
        "node_id", "gateway_generation_id", "freeswitch_generation_id", "state_version", "valid_for_ms", "routing"
    }:
        raise ValueError("invalid placement node")
    node_id = _identifier(value["node_id"], r"[a-z0-9][a-z0-9._-]{0,63}")
    gateway = _identifier(value["gateway_generation_id"], r"[A-Za-z0-9._:-]{1,128}")
    freeswitch = _identifier(value["freeswitch_generation_id"], r"[A-Za-z0-9._:-]{1,128}")
    state = _integer(value["state_version"], 0, 2**63 - 1)
    validity = _integer(value["valid_for_ms"], 1, 15000)
    routing = value["routing"]
    if routing is None:
        return None
    if not isinstance(routing, dict) or set(routing) != {
        "incarnation_id", "siprec_target", "voice_target", "gateway_load_origin", "load_secret",
        "maximum_physical_channels", "route_capabilities"
    }:
        raise ValueError("invalid native routing metadata")
    reference = routing["load_secret"]
    if not isinstance(reference, dict) or set(reference) != {"arn", "version_id"}:
        raise ValueError("invalid load secret reference")
    arn = _identifier(reference["arn"], r"arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]{1,512}")
    if not arn.startswith(secret_prefix):
        raise ValueError("load secret reference outside consumer scope")
    version = _identifier(reference["version_id"], r"[A-Za-z0-9-]{32,64}")
    origin = routing["gateway_load_origin"]
    if not isinstance(origin, str) or len(origin) > 2048:
        raise ValueError("invalid load origin")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or parsed.hostname is None or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("invalid load origin")
    capabilities = routing["route_capabilities"]
    if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 2 or any(
        capability not in ("siprec", "voice") for capability in capabilities
    ) or len(set(capabilities)) != len(capabilities):
        raise ValueError("invalid routing capabilities")
    return RoutingNode(
        node_id, _identifier(routing["incarnation_id"], r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"),
        gateway, freeswitch, state, validity,
        _integer(routing["maximum_physical_channels"], 1, 10000), origin, arn, version,
        _sip_target(routing["siprec_target"]), _sip_target(routing["voice_target"]), frozenset(capabilities),
    )


async def read_inventory(client: DirectJsonClient, origin: str, token: str, secret_prefix: str) -> PlacementInventory:
    """Return only a complete bounded refresh; a partial response never replaces inventory."""
    if not re.fullmatch(r"arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+/", secret_prefix):
        raise ValueError("explicit load-secret namespace is required")
    loop = asyncio.get_running_loop()
    started = loop.time()
    after: str | None = None
    nodes: list[RoutingNode] = []
    count = 0
    try:
        async with asyncio.timeout(5):
            for _ in range(4):
                path = "/internal/v1/node-placement?limit=100" + ("" if after is None else "&after=" + after)
                reply = await client.get(origin, path, token, timeout=2, maximum_bytes=1_000_000)
                if reply.status != 200:
                    raise ObservationUnavailable("placement inventory unavailable")
                value = json.loads(reply.body, object_pairs_hook=_unique_object)
                if not isinstance(value, dict) or set(value) != {"schema_version", "nodes", "next_after"}:
                    raise ValueError("invalid placement page")
                if value["schema_version"] != "1.1.0" or not isinstance(value["nodes"], list) or len(value["nodes"]) > 100:
                    raise ValueError("unsupported placement page")
                last = after
                for item in value["nodes"]:
                    node = _routing_node(item, secret_prefix)
                    node_id = item["node_id"]
                    if last is not None and node_id <= last:
                        raise ValueError("placement page is not identity ordered")
                    last = node_id
                    count += 1
                    if count > 256:
                        raise ValueError("placement inventory exceeds consumer capacity")
                    if node is not None:
                        nodes.append(node)
                cursor = value["next_after"]
                if cursor is None:
                    return PlacementInventory(tuple(nodes), started, loop.time())
                cursor = _identifier(cursor, r"[a-z0-9][a-z0-9._-]{0,63}")
                if (after is not None and cursor <= after) or (last is not None and cursor < last):
                    raise ValueError("placement cursor did not advance")
                after = cursor
            raise ValueError("placement pagination exceeds consumer capacity")
    except (ValueError, TypeError, RecursionError, TimeoutError):
        raise ObservationUnavailable("placement inventory invalid or incomplete") from None


class DirectJsonClient:
    """Pin a verified HTTPS connection to a role-approved address on every read."""

    def __init__(
        self,
        networks: tuple[IPv4Network | IPv6Network, ...],
        ports: frozenset[int],
        *,
        trust: ssl.SSLContext | None = None,
        concurrency: int = 8,
    ) -> None:
        if not networks or any(network.prefixlen == 0 or getattr(network.network_address, "ipv4_mapped", None) is not None for network in networks):
            raise ValueError("explicit observation networks are required")
        if not ports or any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
            raise ValueError("explicit observation ports are required")
        if type(concurrency) is not int or not 1 <= concurrency <= 32:
            raise ValueError("observation concurrency must be between 1 and 32")
        self._trust = trust or ssl.create_default_context()
        if not self._trust.check_hostname or self._trust.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("observation TLS requires certificate and hostname verification")
        self._networks, self._ports = networks, ports
        self._capacity, self._active = concurrency, 0
        self._pending_resolution: set[asyncio.Task[DnsResult]] = set()

    def _resolution_finished(self, task: asyncio.Task[DnsResult]) -> None:
        """Timed-out system resolution retains its capacity slot until it actually finishes."""
        self._pending_resolution.discard(task)
        if not task.cancelled():
            task.exception()
        self._active -= 1

    async def get(
        self, origin: str, path: str, token: str, *, timeout: float, maximum_bytes: int
    ) -> JsonReply:
        """Reject excess work immediately; bound resolution through response closure."""
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or parsed.hostname is None or len(origin) > 2048
        ):
            raise ValueError("observation origin must be bare HTTPS")
        try:
            host, port = parsed.hostname, parsed.port or 443
        except ValueError:
            raise ValueError("invalid observation port") from None
        if port not in self._ports or not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", host):
            raise ValueError("observation origin is outside its role")
        if path != "/v1/node-load" and not re.fullmatch(
            r"/internal/v1/node-placement\?limit=100(?:&after=[a-z0-9][a-z0-9._-]{0,63})?", path
        ):
            raise ValueError("unsupported observation route")
        if not 32 <= len(token) <= 4096 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("invalid observation credential")
        if not 0 < timeout <= 15 or type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 1_000_000:
            raise ValueError("invalid observation bounds")
        if self._active >= self._capacity:
            raise ObservationUnavailable("observation capacity exhausted")
        self._active += 1
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + timeout
        writer: asyncio.StreamWriter | None = None
        resolution = None
        try:
            async with asyncio.timeout_at(deadline):
                resolution = asyncio.create_task(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM))
                addresses = await asyncio.shield(resolution)
                if not addresses or len(addresses) > 32:
                    raise ObservationUnavailable("invalid observation resolution")
                for _, _, _, _, address in addresses:
                    ip = ip_address(address[0])
                    if (
                        ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified
                        or getattr(ip, "ipv4_mapped", None) is not None
                        or not any(ip in network for network in self._networks)
                    ):
                        raise ObservationUnavailable("observation resolution outside its role")
                reader, writer = await asyncio.open_connection(
                    addresses[0][4][0], port, ssl=self._trust, server_hostname=host, limit=8192
                )
                authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                writer.write((
                    f"GET {path} HTTP/1.1\r\nHost: {authority}\r\n"
                    f"Authorization: Bearer {token}\r\nAccept: application/json\r\n"
                    "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
                ).encode("ascii"))
                await writer.drain()
                header = await reader.readuntil(b"\r\n\r\n")
                status, length = _response_header(header, maximum_bytes)
                body = await reader.readexactly(length)
                writer.close()
                await writer.wait_closed()
                return JsonReply(status, body, started, loop.time())
        except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            raise ObservationUnavailable("observation transport unavailable") from None
        finally:
            if writer is not None:
                writer.close()
                # A timed-out TLS peer must not turn shutdown into an unbounded wait.
                writer.transport.abort()
            if resolution is not None and not resolution.done():
                self._pending_resolution.add(resolution)
                resolution.add_done_callback(self._resolution_finished)
            else:
                if resolution is not None and not resolution.cancelled():
                    resolution.exception()
                self._active -= 1


def _response_header(header: bytes, maximum_bytes: int) -> tuple[int, int]:
    """Accept only the exact, length-delimited uncompressed JSON response contract."""
    if len(header) > 8192 or not header.endswith(b"\r\n\r\n"):
        raise ValueError("invalid observation framing")
    lines = header[:-4].split(b"\r\n")
    status = re.fullmatch(rb"HTTP/1\.[01] ([2-5][0-9]{2})(?: [\x20-\x7e]*)?", lines[0])
    if status is None:
        raise ValueError("invalid observation status")
    fields: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator or not re.fullmatch(rb"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name):
            raise ValueError("invalid observation header")
        name, value = name.lower(), value.strip(b" \t")
        if name in fields or any(byte < 32 and byte != 9 or byte > 126 for byte in value):
            raise ValueError("ambiguous observation header")
        fields[name] = value
    length = fields.get(b"content-length", b"")
    if (
        b"transfer-encoding" in fields or not re.fullmatch(rb"[0-9]{1,7}", length)
        or int(length) > maximum_bytes or fields.get(b"content-encoding", b"identity") != b"identity"
        or fields.get(b"content-type", b"").partition(b";")[0].strip().lower() != b"application/json"
    ):
        raise ValueError("unsupported observation response")
    return int(status[1]), int(length)
