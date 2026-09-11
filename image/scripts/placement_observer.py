"""Supervised direct gateway observations published under PostgreSQL observer epochs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from pathlib import Path
import random
import sys

import asyncpg

from gateway_load_polling import DirectJsonClient, ObservationUnavailable, RoutingNode, read_inventory
from gateway_load_selection import Target, parse_load
from placement_store import PlacementStore, PollAuthority

logger = logging.getLogger(__name__)


class SecretResolver:
    """Kill the SDK child on timeout; no ambient AWS profile/static key fallback or thread leak."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    async def resolve(self, node: RoutingNode) -> str:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("placement_secret.py")),
            node.load_secret_arn, node.load_secret_version, self._prefix,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "PYTHONNOUSERSITE": "1"},
        )
        try:
            async with asyncio.timeout(3):
                if process.stdout is None:
                    raise ObservationUnavailable("secret reader unavailable")
                try:
                    value = await process.stdout.readexactly(4097)
                except asyncio.IncompleteReadError as error:
                    value = error.partial
                if len(value) > 4096 or await process.wait() != 0:
                    raise ObservationUnavailable("load credential unavailable")
                token = value.decode("ascii")
                if not 32 <= len(token) <= 4096 or any(not 33 <= ord(char) <= 126 for char in token):
                    raise ObservationUnavailable("load credential invalid")
                return token
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()


class PlacementObserver:
    def __init__(self, store: PlacementStore, inventory_http: DirectJsonClient, load_http: DirectJsonClient,
                 sage_origin: str, sage_token: str, secret_prefix: str, owner: str,
                 resolve_secret: Callable[[RoutingNode], Awaitable[str]], *,
                 validate_node: Callable[[RoutingNode], None] | None = None) -> None:
        self._store, self._inventory_http, self._load_http = store, inventory_http, load_http
        self._sage_origin, self._sage_token, self._prefix = sage_origin, sage_token, secret_prefix
        self._owner, self._resolve_secret = owner, resolve_secret
        self._validate_node = validate_node
        self._authority: PollAuthority | None = None
        self._nodes: tuple[RoutingNode, ...] = ()
        self._secrets: dict[tuple[str, str], str] = {}

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._heartbeat())
            tasks.create_task(self._inventory())
            tasks.create_task(self._loads())

    async def _heartbeat(self) -> None:
        while True:
            try:
                authority = await self._store.claim_polling(self._owner)
                if authority != self._authority:
                    self._nodes, self._secrets = (), {}
                self._authority = authority
            except (asyncpg.PostgresError, OSError, TimeoutError):
                self._authority, self._nodes, self._secrets = None, (), {}
                logger.warning("placement_observer_authority_unavailable_retry")
            await asyncio.sleep(2)

    async def refresh_inventory(self) -> bool:
        authority = self._authority
        if authority is None:
            return False
        ticket = await self._store.ticket(authority)
        if ticket is None:
            return False
        inventory = await read_inventory(self._inventory_http, self._sage_origin, self._sage_token, self._prefix)
        if self._validate_node is not None:
            for node in inventory.nodes:
                self._validate_node(node)
        if not await self._store.publish_inventory(ticket, inventory) or self._authority != authority:
            return False
        self._nodes = inventory.nodes
        references = {(node.load_secret_arn, node.load_secret_version) for node in self._nodes}
        self._secrets = {key: value for key, value in self._secrets.items() if key in references}
        return True

    async def _inventory(self) -> None:
        while True:
            try:
                await self.refresh_inventory()
            except (ObservationUnavailable, asyncpg.PostgresError, OSError, TimeoutError, ValueError):
                # Existing SQL leases expire; partial pages never replace the completed snapshot.
                logger.warning("placement_inventory_unavailable_retry")
            await asyncio.sleep(1)

    async def observe(self, node: RoutingNode) -> None:
        authority = self._authority
        if authority is None:
            return
        ticket = await self._store.ticket(authority)
        if ticket is None:
            return
        sample = None
        try:
            reference = node.load_secret_arn, node.load_secret_version
            token = self._secrets.get(reference)
            if token is None:
                token = await self._resolve_secret(node)
                if self._authority == authority and any(
                    active.node_id == node.node_id and (active.load_secret_arn, active.load_secret_version) == reference
                    for active in self._nodes
                ):
                    self._secrets[reference] = token
            # The DB ticket for the actual HTTP observation follows any potentially slow secret read.
            ticket = await self._store.ticket(authority)
            if ticket is None:
                return
            reply = await self._load_http.get(node.load_origin, "/v1/node-load", token, timeout=1.8, maximum_bytes=4096)
            sample = parse_load(Target(node.node_id, node.gateway_generation_id, node.freeswitch_generation_id,
                                       node.valid_for_ms, node.channel_limit), status=reply.status, body=reply.body,
                                request_started=reply.request_started, now=reply.completed_at, incarnation_id=node.incarnation_id)
        except (ObservationUnavailable, OSError, TimeoutError, ValueError):
            logger.warning("gateway_load_unavailable_withdraw")
        if sample is None:
            await self._store.publish_load(ticket, node, channels=None)
        else:
            await self._store.publish_load(ticket, node, channels=sample.channels, telemetry=sample.telemetry,
                                           sequence=sample.sequence, observed_at=sample.observed_at,
                                           sample_age_ms=sample.sample_age_ms)

    async def _loads(self) -> None:
        while True:
            nodes = self._nodes
            for start in range(0, len(nodes), 8):
                results = await asyncio.gather(*(self.observe(node) for node in nodes[start:start+8]), return_exceptions=True)
                for result in results:
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    if isinstance(result, (ObservationUnavailable, asyncpg.PostgresError, OSError, TimeoutError, ValueError)):
                        logger.warning("gateway_load_publication_deferred")
                    elif isinstance(result, BaseException):
                        raise result
            await asyncio.sleep(random.uniform(0.8, 1.0))
