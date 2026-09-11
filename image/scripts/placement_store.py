"""Short PostgreSQL transactions for global placement reservations and observation leases."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re

import asyncpg

from gateway_load_polling import PlacementInventory, RoutingNode


class AllocationConflict(ValueError):
    """An allocation ID cannot acquire a different request or destination."""


@dataclass(frozen=True)
class AllocationRequest:
    allocation_id: str
    request_sha256: str
    product: str
    channels: int
    ttl_ms: int

    def __post_init__(self) -> None:
        for value in (self.allocation_id, self.request_sha256):
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("allocation identity must be a SHA-256 digest")
        if self.product not in {"siprec", "voice"}:
            raise ValueError("unsupported placement product")
        if type(self.channels) is not int or not 1 <= self.channels <= 10000:
            raise ValueError("invalid physical channel cost")
        if type(self.ttl_ms) is not int or not 1 <= self.ttl_ms <= 30000:
            raise ValueError("invalid allocation lifetime")


@dataclass(frozen=True)
class PollAuthority:
    owner: str
    epoch: int


@dataclass(frozen=True)
class ObservationTicket:
    authority: PollAuthority
    number: int
    issued_at: datetime


def identity(node: RoutingNode) -> str:
    return hashlib.sha256(json.dumps((node.node_id, node.incarnation_id,
                                     node.gateway_generation_id, node.freeswitch_generation_id)).encode()).hexdigest()


def routing_json(node: RoutingNode) -> str:
    # Persist routing identity, not secret values or the load-read reference.
    value = asdict(node)
    for field in ("load_secret_arn", "load_secret_version", "load_origin", "valid_for_ms"):
        value.pop(field)
    value["capabilities"] = sorted(node.capabilities)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class PlacementStore:
    """Use one bounded pool; a namespace lock serializes only short SQL decisions."""

    def __init__(self, pool: asyncpg.Pool, namespace: str, *, maximum_pending: int = 4096) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", namespace):
            raise ValueError("invalid placement namespace")
        if type(maximum_pending) is not int or not 1 <= maximum_pending <= 100000:
            raise ValueError("invalid placement capacity")
        self._pool, self._namespace, self._maximum_pending = pool, namespace, maximum_pending

    async def initialize(self) -> None:
        """Register this namespace in the pre-installed schema; runtime requires no DDL grant."""
        async with self._pool.acquire(timeout=2) as connection:
            await connection.execute("INSERT INTO opensips_placement.control(namespace) VALUES($1) ON CONFLICT DO NOTHING",
                                     self._namespace)

    async def _lock(self, connection: asyncpg.Connection) -> None:
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                                 "opensips-placement:" + self._namespace)

    async def claim_polling(self, owner: str) -> PollAuthority | None:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", owner):
            raise ValueError("invalid poll observer identity")
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            async with connection.transaction():
                await self._lock(connection)
                row = await connection.fetchrow("""
                    UPDATE opensips_placement.control
                    SET poll_owner=$2,
                        poll_epoch=poll_epoch + CASE WHEN poll_owner=$2 AND poll_until>clock_timestamp() THEN 0 ELSE 1 END,
                        poll_until=clock_timestamp()+interval '10 seconds'
                    WHERE namespace=$1 AND (poll_owner=$2 OR poll_until IS NULL OR poll_until<=clock_timestamp())
                    RETURNING poll_epoch
                """, self._namespace, owner)
                return None if row is None else PollAuthority(owner, row["poll_epoch"])

    async def ticket(self, authority: PollAuthority) -> ObservationTicket | None:
        """Anchor validity to database time before external I/O, never after a lock wait."""
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            row = await connection.fetchrow("""
                UPDATE opensips_placement.control SET next_ticket=next_ticket+1
                WHERE namespace=$1 AND poll_owner=$2 AND poll_epoch=$3 AND poll_until>clock_timestamp()
                RETURNING next_ticket, clock_timestamp() AS issued_at
            """, self._namespace, authority.owner, authority.epoch)
            return None if row is None else ObservationTicket(authority, row["next_ticket"], row["issued_at"])

    async def _current(self, connection: asyncpg.Connection, authority: PollAuthority) -> bool:
        return bool(await connection.fetchval("""
            SELECT EXISTS(SELECT 1 FROM opensips_placement.control
                          WHERE namespace=$1 AND poll_owner=$2 AND poll_epoch=$3 AND poll_until>clock_timestamp())
        """, self._namespace, authority.owner, authority.epoch))

    async def publish_inventory(self, ticket: ObservationTicket, inventory: PlacementInventory) -> bool:
        if len(inventory.nodes) > 256 or len({node.node_id for node in inventory.nodes}) != len(inventory.nodes):
            raise ValueError("invalid complete placement inventory")
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            async with connection.transaction():
                await self._lock(connection)
                if not await self._current(connection, ticket.authority):
                    return False
                installed = await connection.fetchval("SELECT installed_ticket FROM opensips_placement.control WHERE namespace=$1",
                                                      self._namespace)
                if installed >= ticket.number:
                    return False
                for node in inventory.nodes:
                    await connection.execute("""
                        INSERT INTO opensips_placement.nodes(namespace,node_id,identity_sha256,routing,channel_limit,eligible_until)
                        VALUES($1,$2,$3,$4::jsonb,$5,$6)
                        ON CONFLICT(namespace,node_id) DO UPDATE SET
                            identity_sha256=EXCLUDED.identity_sha256,routing=EXCLUDED.routing,
                            channel_limit=EXCLUDED.channel_limit,eligible_until=EXCLUDED.eligible_until,
                            load_until=CASE WHEN nodes.identity_sha256=EXCLUDED.identity_sha256 THEN nodes.load_until END,
                            load_valid=CASE WHEN nodes.identity_sha256=EXCLUDED.identity_sha256 THEN nodes.load_valid ELSE false END,
                            physical_channels=CASE WHEN nodes.identity_sha256=EXCLUDED.identity_sha256 THEN nodes.physical_channels END,
                            telemetry_generation=CASE WHEN nodes.identity_sha256=EXCLUDED.identity_sha256 THEN nodes.telemetry_generation END,
                            observation_sequence=CASE WHEN nodes.identity_sha256=EXCLUDED.identity_sha256 THEN nodes.observation_sequence END,
                            observed_at=CASE WHEN nodes.identity_sha256=EXCLUDED.identity_sha256 THEN nodes.observed_at END,
                            sample_not_before=CASE WHEN nodes.identity_sha256=EXCLUDED.identity_sha256 THEN nodes.sample_not_before END,
                            load_ticket=CASE WHEN nodes.identity_sha256=EXCLUDED.identity_sha256 THEN nodes.load_ticket ELSE 0 END
                    """, self._namespace, node.node_id, identity(node), routing_json(node), node.channel_limit,
                                             ticket.issued_at + timedelta(milliseconds=node.valid_for_ms))
                await connection.execute("UPDATE opensips_placement.nodes SET eligible_until=clock_timestamp() WHERE namespace=$1 AND eligible_until>clock_timestamp() AND NOT(node_id=ANY($2::text[]))",
                                         self._namespace, [node.node_id for node in inventory.nodes])
                await connection.execute("UPDATE opensips_placement.control SET installed_ticket=$2 WHERE namespace=$1",
                                         self._namespace, ticket.number)
                return True

    async def publish_load(
        self, ticket: ObservationTicket, node: RoutingNode, *, channels: int | None,
        telemetry: str = "", sequence: int = 0, observed_at: str = "", sample_age_ms: int = 0,
    ) -> bool:
        """Duplicate observations can shorten but never renew their first accepted lease."""
        if channels is not None and (
            type(channels) is not int or not 0 <= channels <= 10000
            or type(sequence) is not int or not 1 <= sequence <= 2**63-1
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", telemetry)
            or type(sample_age_ms) is not int or not 0 <= sample_age_ms < 3000
            or not isinstance(observed_at, str) or not 1 <= len(observed_at) <= 64
        ):
            raise ValueError("invalid load evidence")
        if channels is not None:
            try:
                if datetime.fromisoformat(observed_at).utcoffset() is None:
                    raise ValueError
            except ValueError:
                raise ValueError("invalid load observation time") from None
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            async with connection.transaction():
                await self._lock(connection)
                if not await self._current(connection, ticket.authority):
                    return False
                row = await connection.fetchrow("SELECT * FROM opensips_placement.nodes WHERE namespace=$1 AND node_id=$2 FOR UPDATE",
                                                self._namespace, node.node_id)
                if row is None or row["identity_sha256"] != identity(node) or row["load_ticket"] >= ticket.number:
                    return False
                expiry = ticket.issued_at + timedelta(milliseconds=3000-sample_age_ms)
                lower_bound = ticket.issued_at - timedelta(milliseconds=sample_age_ms)
                if channels is not None and row["telemetry_generation"] != telemetry:
                    first = await connection.fetchval("""
                        INSERT INTO opensips_placement.telemetry_history(namespace,node_id,identity_sha256,telemetry_generation,first_ticket)
                        VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING RETURNING true
                    """, self._namespace, node.node_id, identity(node), telemetry, ticket.number)
                    if not first:
                        channels = None
                if channels is not None and row["telemetry_generation"] == telemetry:
                    if sequence < row["observation_sequence"]:
                        channels = None
                    elif sequence == row["observation_sequence"]:
                        if row["physical_channels"] != channels or row["observed_at"] != observed_at:
                            channels = None
                        elif row["load_until"] is not None:
                            expiry = min(expiry, row["load_until"])
                            lower_bound = min(lower_bound, row["sample_not_before"])
                if channels is None:
                    await connection.execute("UPDATE opensips_placement.nodes SET load_valid=false,load_ticket=$3 WHERE namespace=$1 AND node_id=$2",
                                             self._namespace, node.node_id, ticket.number)
                else:
                    await connection.execute("""
                        UPDATE opensips_placement.nodes SET load_valid=true,physical_channels=$3,telemetry_generation=$4,
                            observation_sequence=$5,observed_at=$6,load_until=$7,sample_not_before=$8,load_ticket=$9
                        WHERE namespace=$1 AND node_id=$2
                    """, self._namespace, node.node_id, channels, telemetry, sequence, observed_at, expiry, lower_bound, ticket.number)
                return True

    async def reserve(self, request: AllocationRequest) -> dict[str, object] | None:
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            async with connection.transaction():
                await self._lock(connection)
                now = await connection.fetchval("SELECT clock_timestamp()")
                previous = await connection.fetchrow("SELECT * FROM opensips_placement.reservations WHERE namespace=$1 AND allocation_id=$2",
                                                     self._namespace, request.allocation_id)
                if previous is not None:
                    if previous["state"] == "released":
                        return None
                    if (previous["request_sha256"], previous["product"], previous["channels"]) != (
                        request.request_sha256, request.product, request.channels
                    ):
                        raise AllocationConflict("allocation identity was reused with a different request")
                    if previous["expires_at"] <= now:
                        return None
                    return {**json.loads(previous["routing"]), "allocation_fresh": False}
                count = await connection.fetchval("""
                    SELECT count(*) FROM opensips_placement.reservations
                    WHERE namespace=$1 AND state IN ('pending','confirmed') AND expires_at>$2
                """, self._namespace, now)
                if count >= self._maximum_pending:
                    return None
                selected = await connection.fetchrow("""
                    SELECT n.*, COALESCE(p.cost,0) AS pending_cost
                    FROM opensips_placement.nodes n
                    LEFT JOIN LATERAL (
                        SELECT sum(r.channels) AS cost FROM opensips_placement.reservations r
                        WHERE r.namespace=n.namespace AND r.node_id=n.node_id AND r.identity_sha256=n.identity_sha256
                          AND r.state IN ('pending','confirmed') AND r.expires_at>$2
                          AND (r.state='pending' OR r.confirmed_at>=n.sample_not_before)
                    ) p ON true
                    WHERE n.namespace=$1 AND n.eligible_until>$2 AND n.load_until>$2 AND n.load_valid
                      AND n.physical_channels IS NOT NULL AND (n.routing->'capabilities') ? $3
                      AND n.physical_channels+COALESCE(p.cost,0)+$4<=n.channel_limit
                    ORDER BY (n.physical_channels+COALESCE(p.cost,0))::numeric/n.channel_limit,
                             md5($5 || n.node_id), n.node_id LIMIT 1
                """, self._namespace, now, request.product, request.channels, request.allocation_id)
                if selected is None:
                    return None
                await connection.execute("""
                    INSERT INTO opensips_placement.reservations(namespace,allocation_id,request_sha256,product,channels,
                                                              node_id,identity_sha256,routing,state,expires_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,'pending',$9)
                """, self._namespace, request.allocation_id, request.request_sha256, request.product,
                                         request.channels, selected["node_id"], selected["identity_sha256"], selected["routing"],
                                         now + timedelta(milliseconds=request.ttl_ms))
                return {**json.loads(selected["routing"]), "allocation_fresh": True}

    async def release(self, allocation_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", allocation_id):
            raise ValueError("invalid allocation identity")
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            async with connection.transaction():
                await self._lock(connection)
                await connection.execute("""
                    INSERT INTO opensips_placement.reservations(namespace,allocation_id,state) VALUES($1,$2,'released')
                    ON CONFLICT(namespace,allocation_id) DO UPDATE SET state='released'
                """, self._namespace, allocation_id)

    async def confirm(self, allocation_id: str) -> bool:
        if not re.fullmatch(r"[a-f0-9]{64}", allocation_id):
            raise ValueError("invalid allocation identity")
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            async with connection.transaction():
                await self._lock(connection)
                return bool(await connection.fetchval("""
                    UPDATE opensips_placement.reservations SET state='confirmed',confirmed_at=COALESCE(confirmed_at,clock_timestamp())
                    WHERE namespace=$1 AND allocation_id=$2 AND state IN ('pending','confirmed') AND expires_at>clock_timestamp()
                    RETURNING true
                """, self._namespace, allocation_id))
