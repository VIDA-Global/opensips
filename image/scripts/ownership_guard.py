"""Fail-closed native process startup against immutable EC2/SQL ownership."""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
from pathlib import Path
import ssl

import asyncpg

from ownership import OwnershipStore, instance_id
from placement_config import validate_placement


def observed_instance() -> str:
    """Read only the local IMDSv2 instance ID, with no redirect or proxy support."""
    connection = http.client.HTTPConnection("169.254.169.254", timeout=2)
    try:
        connection.request("PUT", "/latest/api/token", headers={
            "X-aws-ec2-metadata-token-ttl-seconds": "60",
        })
        response = connection.getresponse()
        token = response.read(257)
        if response.status != 200 or not 1 <= len(token) <= 256:
            raise ValueError("instance metadata token unavailable")
        connection.request("GET", "/latest/meta-data/instance-id", headers={
            "X-aws-ec2-metadata-token": token.decode("ascii"),
        })
        response = connection.getresponse()
        identity = response.read(33)
        if response.status != 200:
            raise ValueError("instance identity unavailable")
        return instance_id(identity.decode("ascii"))
    finally:
        connection.close()


async def check() -> None:
    identity = await asyncio.to_thread(observed_instance)
    with Path("/run/opensips-secure/config/placement.json").open("rb") as source:
        data = source.read(16385)
    if len(data) > 16384:
        raise ValueError("ownership startup configuration too large")
    config = validate_placement(json.loads(data))
    trust = ssl.create_default_context(cafile=config["database_ca_bundle"] or None)
    pool = await asyncpg.create_pool(
        config["database_url"], min_size=1, max_size=1, timeout=3, command_timeout=2,
        ssl=trust, server_settings={"statement_timeout": "2000", "lock_timeout": "1500"},
    )
    try:
        if not await OwnershipStore(pool, config["namespace"]).authorized(identity):
            raise ValueError("instance is not the active SIP owner")
    finally:
        pool.terminate()


if __name__ == "__main__":
    try:
        asyncio.run(check())
    except Exception:
        # Database/SDK errors can include credentials: report only the outcome.
        logging.error("opensips_ownership_start_denied")
        raise SystemExit(1) from None
