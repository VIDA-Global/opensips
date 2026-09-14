"""Execute one retryable fence-before-promote operation from an approved manifest."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit
from uuid import UUID

from ownership import OwnershipStore, instance_id, transfer_ownership
from ownership_fencing import Ec2InstanceFencer


@dataclass(frozen=True)
class ControllerConfiguration:
    namespace: str
    database_url: str
    database_ca_bundle: str | None
    region: str
    account: str
    instances: frozenset[str]


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate ownership controller setting")
        document[key] = value
    return document


def load_configuration(path: Path) -> ControllerConfiguration:
    with path.open("rb") as source:
        encoded = source.read(16385)
    if len(encoded) > 16384:
        raise ValueError("ownership controller configuration too large")
    configuration: object = json.loads(encoded, object_pairs_hook=unique_object)
    if not isinstance(configuration, dict) or set(configuration) != {
        "namespace", "database_url", "database_ca_bundle", "region", "account", "instances"
    }:
        raise ValueError("invalid ownership controller configuration")
    region = configuration["region"]
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+){1,2}-[1-9][0-9]?", region):
        raise ValueError("invalid ownership controller region")
    namespace = configuration["namespace"]
    account = configuration["account"]
    database = configuration["database_url"]
    ca_bundle = configuration["database_ca_bundle"]
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", namespace):
        raise ValueError("invalid ownership controller namespace")
    if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("invalid ownership controller account")
    if not isinstance(database, str) or len(database) > 4096 or any(ord(char) < 32 for char in database):
        raise ValueError("invalid ownership database URL")
    parsed = urlsplit(database)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path:
        raise ValueError("invalid ownership database URL")
    if ca_bundle is not None and (not isinstance(ca_bundle, str) or not Path(ca_bundle).is_absolute()):
        raise ValueError("ownership database CA must be an absolute path")
    instances = configuration["instances"]
    if (not isinstance(instances, list) or not 1 <= len(instances) <= 64
            or not all(isinstance(item, str) for item in instances)):
        raise ValueError("invalid ownership controller instance scope")
    allowed = frozenset(instance_id(item) for item in instances)
    if len(allowed) != len(instances):
        raise ValueError("duplicate ownership controller instances")
    return ControllerConfiguration(namespace, database, ca_bundle, region, account, allowed)


async def run(path: Path, candidate: str, epoch: int, operation: UUID) -> int:
    configuration = load_configuration(path)
    if candidate not in configuration.instances or type(epoch) is not int or epoch < 0:
        raise ValueError("candidate is outside the approved instance scope")
    # Validate the complete manifest before SDK credential discovery or any I/O.
    import asyncpg
    import boto3
    from botocore.config import Config

    region = configuration.region
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    trust = ssl.create_default_context(cafile=configuration.database_ca_bundle)
    client = boto3.client(
        "ec2", region_name=region, endpoint_url=f"https://ec2.{region}.{suffix}",
        verify=True,
        config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2}, proxies={}),
    )
    fencer = Ec2InstanceFencer(
        client, account=configuration.account, allowed_instances=configuration.instances,
    )
    pool = await asyncpg.create_pool(
        configuration.database_url, min_size=1, max_size=2, timeout=3,
        command_timeout=2, ssl=trust,
        server_settings={"statement_timeout": "2000", "lock_timeout": "1500"},
    )
    try:
        return await transfer_ownership(
            OwnershipStore(pool, configuration.namespace), fencer,
            candidate=candidate, expected_epoch=epoch, operation_id=operation,
        )
    finally:
        pool.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--candidate", type=instance_id, required=True)
    parser.add_argument("--expected-epoch", type=int, required=True)
    parser.add_argument("--operation-id", type=UUID, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.config, args.candidate, args.expected_epoch, args.operation_id))
    except Exception:
        logging.error("ownership_transfer_incomplete")
        raise SystemExit(1) from None
    print(json.dumps({"phase": "active", "epoch": result}))
