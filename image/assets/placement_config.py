"""Shared schema-v2 placement configuration validation for renderer and service."""

from ipaddress import ip_address, ip_network
from pathlib import Path
import re
from urllib.parse import urlsplit


def validate_placement(value: object) -> dict[str, object]:
    required = {"namespace", "database_url", "service_token", "sage_origin", "sage_token", "load_secret_prefix",
                "inventory_networks", "inventory_ports", "gateway_networks", "gateway_ports", "sip_networks",
                "sip_ports", "ca_bundle", "database_ca_bundle"}
    try:
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError
        if not isinstance(value["namespace"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value["namespace"]):
            raise ValueError
        if not isinstance(value["service_token"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["service_token"]):
            raise ValueError
        token = value["sage_token"]
        if not isinstance(token, str) or not 32 <= len(token) <= 4096 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError
        if token == value["service_token"]:
            raise ValueError
        database = urlsplit(value["database_url"])
        if database.scheme not in {"postgres", "postgresql"} or not database.hostname or not database.username or not database.password:
            raise ValueError
        origin = urlsplit(value["sage_origin"])
        if origin.scheme != "https" or not origin.hostname or origin.username is not None or origin.password is not None or origin.query or origin.fragment or origin.path not in {"", "/"}:
            raise ValueError
        if not re.fullmatch(r"arn:aws:secretsmanager:[a-z0-9-]{1,32}:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]{1,128}/", value["load_secret_prefix"]):
            raise ValueError
        for role in ("inventory", "gateway", "sip"):
            networks, ports = value[role+"_networks"], value[role+"_ports"]
            if not isinstance(networks, list) or not 1 <= len(networks) <= 32 or not isinstance(ports, list) or not 1 <= len(ports) <= 32:
                raise ValueError
            for item in networks:
                if not isinstance(item, str):
                    raise ValueError
                network = ip_network(item)
                if (network.prefixlen == 0 or network.network_address.is_loopback or network.network_address.is_link_local
                        or network.network_address.is_multicast or network.network_address.is_unspecified
                        or getattr(network.network_address, "ipv4_mapped", None) is not None):
                    raise ValueError
            if any(type(port) is not int or not 1 <= port <= 65535 for port in ports) or len(set(ports)) != len(ports):
                raise ValueError
        if (origin.port or 443) not in value["inventory_ports"]:
            raise ValueError
        for name in ("ca_bundle", "database_ca_bundle"):
            if value[name] is not None and (not isinstance(value[name], str) or not Path(value[name]).is_absolute()):
                raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid placement configuration") from None
    return value


def validate_sip_destinations(destinations: tuple[str, ...], config: dict[str, object]) -> None:
    networks = tuple(ip_network(value) for value in config["sip_networks"])
    for destination in destinations:
        # The current native backend route explicitly uses its private UDP socket.
        # Reject a TLS/TCP target instead of silently changing its requested transport.
        if not destination.endswith(";transport=udp"):
            raise ValueError("native backend SIP transport is not qualified")
        target = urlsplit("//" + destination.removeprefix("sip:").partition(";")[0])
        address = ip_address(target.hostname)
        if target.port not in config["sip_ports"] or not any(address in network for network in networks):
            raise ValueError("SIP destination is outside placement scope")


def validate_instance_scope(config: dict[str, object], region: str, account: str) -> None:
    if not config["load_secret_prefix"].startswith(f"arn:aws:secretsmanager:{region}:{account}:secret:"):
        raise ValueError("load credentials must belong to the instance account and region")
