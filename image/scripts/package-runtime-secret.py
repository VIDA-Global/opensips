#!/usr/bin/env python3
"""Package structured deployment values and TLS files for the runtime secret."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path


MAX_SECRET_BYTES = 65536
DEPLOYMENT_FIELDS = {
    "node_id",
    "cluster_id",
    "private_ip",
    "advertised_ip",
    "state_owner",
    "database_url",
    "carrier_udp_ips",
    "carrier_tls_ips",
    "rtpengine_nodes",
}


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON contains duplicate object keys")
        value[key] = item
    return value


def read_nonempty(path: Path, label: str) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if metadata.st_size > MAX_SECRET_BYTES:
            raise ValueError(f"{label} exceeds maximum of {MAX_SECRET_BYTES} bytes")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = stream.read(MAX_SECRET_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be non-empty UTF-8 text without NUL bytes")
    return value


def validate_deployment(deployment: dict[str, object]) -> None:
    if set(deployment) != DEPLOYMENT_FIELDS:
        raise ValueError("deployment configuration must contain exactly the schema-v1 fields")
    for field in ("node_id", "cluster_id"):
        value = deployment[field]
        if type(value) is not int or not 1 <= value <= 2147483647:
            raise ValueError(f"{field} must be a positive integer")
    for field in ("private_ip", "advertised_ip"):
        try:
            if str(ipaddress.IPv4Address(deployment[field])) != deployment[field]:
                raise ValueError
        except (ipaddress.AddressValueError, ValueError, TypeError) as exc:
            raise ValueError(f"{field} must be a canonical IPv4 address") from exc
    if not isinstance(deployment["state_owner"], str) or deployment["state_owner"] not in {
        "active",
        "backup",
    }:
        raise ValueError("state_owner must be active or backup")
    database_url = deployment["database_url"]
    if (
        not isinstance(database_url, str)
        or not database_url.startswith("postgres://")
        or len(database_url) > 1024
        or any(char.isspace() or char in {'"', "\\", "\x00"} for char in database_url)
    ):
        raise ValueError("database_url must be a safe postgres:// URL")
    for field in ("carrier_udp_ips", "carrier_tls_ips"):
        value = deployment[field]
        if not isinstance(value, list) or not value:
            raise ValueError(f"{field} must be a non-empty array")
        try:
            canonical = [str(ipaddress.IPv4Address(item)) for item in value]
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise ValueError(f"{field} must contain IPv4 addresses") from exc
        if canonical != value or len(canonical) != len(set(canonical)):
            raise ValueError(f"{field} must contain unique canonical IPv4 addresses")
    nodes = deployment["rtpengine_nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("rtpengine_nodes must be a non-empty array")
    urls = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"url", "weight"}:
            raise ValueError("each RTPengine node must contain only url and weight")
        url = node["url"]
        match = re.fullmatch(
            r"udp:([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?):([0-9]{1,5})",
            url if isinstance(url, str) else "",
        )
        if not match or not 1 <= int(match.group(2)) <= 65535 or url in urls:
            raise ValueError("RTPengine node URLs must be unique udp:host:port values")
        urls.add(url)
        weight = node["weight"]
        if type(weight) is not int or not 1 <= weight <= 1000:
            raise ValueError("RTPengine node weight must be an integer from 1 through 1000")


def read_deployment(path: Path) -> dict[str, object]:
    try:
        deployment = json.loads(
            read_nonempty(path, "deployment configuration"), object_pairs_hook=reject_duplicate_keys
        )
    except json.JSONDecodeError as exc:
        raise ValueError("deployment configuration must be valid JSON") from exc
    if not isinstance(deployment, dict):
        raise ValueError("deployment configuration must be a JSON object")
    validate_deployment(deployment)
    return deployment


def package_secret(deployment: Path, certificate: Path, private_key: Path, ca_bundle: Path) -> bytes:
    payload = {
        "schema_version": 1,
        "deployment": read_deployment(deployment),
        "tls": {
            "certificate": read_nonempty(certificate, "TLS certificate"),
            "private_key": read_nonempty(private_key, "TLS private key"),
            "ca_bundle": read_nonempty(ca_bundle, "TLS CA bundle"),
        },
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    output_size = len(encoded) + 1
    if output_size > MAX_SECRET_BYTES:
        raise ValueError(f"serialized output is {output_size} bytes; maximum is {MAX_SECRET_BYTES}")
    return encoded


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write a compact schema-v1 OpenSIPS runtime secret to stdout."
    )
    parser.add_argument("deployment", type=Path)
    parser.add_argument("certificate", type=Path)
    parser.add_argument("private_key", type=Path)
    parser.add_argument("ca_bundle", type=Path)
    args = parser.parse_args()

    try:
        encoded = package_secret(args.deployment, args.certificate, args.private_key, args.ca_bundle)
    except ValueError as exc:
        print(f"package-runtime-secret: {exc}", file=sys.stderr)
        return 64

    sys.stdout.buffer.write(encoded + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
