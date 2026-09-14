"""PostgreSQL authority with independent physical fencing before promotion.

There is intentionally no time-expired ownership lease. A live old node can keep
running during a database partition, but a successor cannot become authoritative
until an independent provider has fenced that exact old EC2 instance. Retired
instance IDs never rejoin, including after an operator restarts a stopped instance.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    import asyncpg


class OwnershipConflict(ValueError):
    """The desired transition disagrees with current immutable authority."""


def instance_id(value: str) -> str:
    if not re.fullmatch(r"i-[a-f0-9]{17}", value):
        raise ValueError("invalid immutable EC2 instance identity")
    return value


@dataclass(frozen=True)
class Transfer:
    namespace: str
    epoch: int
    previous: str | None
    candidate: str
    operation_id: UUID
    completed: bool = False


class InstanceFencer(Protocol):
    async def fence(self, target: str) -> None:
        """Return only after the exact instance cannot execute or transmit.

        Unreachable, requested-to-stop, lease-expired, and self-reported-demoted
        are not success. Uncertainty must raise and leave SQL in fencing phase.
        """
        ...


class OwnershipStore:
    def __init__(self, pool: asyncpg.Pool, namespace: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", namespace):
            raise ValueError("invalid ownership namespace")
        self._pool = pool
        self._namespace = namespace

    async def begin(
        self, candidate: str, expected_epoch: int, operation_id: UUID
    ) -> Transfer:
        instance_id(candidate)
        if type(expected_epoch) is not int or expected_epoch < 0:
            raise ValueError("invalid expected ownership epoch")
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM opensips_ownership.authority WHERE namespace=$1 FOR UPDATE",
                    self._namespace,
                )
                if row is None:
                    raise OwnershipConflict("ownership namespace is not provisioned")
                if (row["phase"] == "active" and row["owner_instance"] == candidate
                        and row["operation_id"] == operation_id and row["epoch"] == expected_epoch + 1):
                    return Transfer(self._namespace, expected_epoch, None, candidate,
                                    operation_id, completed=True)
                if row["epoch"] != expected_epoch:
                    raise OwnershipConflict("stale ownership epoch")
                if row["phase"] == "fencing":
                    if row["candidate_instance"] != candidate or row["operation_id"] != operation_id:
                        raise OwnershipConflict("another fencing operation is pending")
                else:
                    if row["owner_instance"] == candidate:
                        raise OwnershipConflict("candidate already owns this namespace")
                    await connection.execute(
                        "INSERT INTO opensips_ownership.instance_namespaces(instance_id,namespace) "
                        "VALUES($1,$2) ON CONFLICT DO NOTHING", candidate, self._namespace,
                    )
                    namespace = await connection.fetchval(
                        "SELECT namespace FROM opensips_ownership.instance_namespaces WHERE instance_id=$1",
                        candidate,
                    )
                    if namespace != self._namespace:
                        raise OwnershipConflict("instance belongs to another ownership namespace")
                    retired = await connection.fetchval(
                        "SELECT 1 FROM opensips_ownership.retired_instances "
                        "WHERE instance_id=$1", candidate,
                    )
                    if retired:
                        raise OwnershipConflict("retired instances cannot regain ownership")
                    await connection.execute(
                        "UPDATE opensips_ownership.authority SET phase='fencing', "
                        "candidate_instance=$2, operation_id=$3 WHERE namespace=$1",
                        self._namespace, candidate, operation_id,
                    )
                return Transfer(self._namespace, expected_epoch, row["owner_instance"],
                                candidate, operation_id)

    async def complete(self, transfer: Transfer) -> int:
        if transfer.namespace != self._namespace:
            raise OwnershipConflict("wrong ownership namespace")
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM opensips_ownership.authority WHERE namespace=$1 FOR UPDATE",
                    self._namespace,
                )
                if row is None or row["operation_id"] != transfer.operation_id:
                    raise OwnershipConflict("fencing operation is no longer current")
                if (row["phase"] == "active" and row["owner_instance"] == transfer.candidate
                        and row["epoch"] == transfer.epoch + 1):
                    return row["epoch"]
                if (row["phase"] != "fencing" or row["epoch"] != transfer.epoch
                        or row["owner_instance"] != transfer.previous
                        or row["candidate_instance"] != transfer.candidate):
                    raise OwnershipConflict("fencing authority changed")
                if transfer.previous is not None:
                    await connection.execute(
                        "INSERT INTO opensips_ownership.retired_instances(namespace,instance_id) "
                        "VALUES($1,$2)", self._namespace, transfer.previous,
                    )
                await connection.execute(
                    "UPDATE opensips_ownership.authority SET phase='active', "
                    "owner_instance=candidate_instance, candidate_instance=NULL, epoch=epoch+1 "
                    "WHERE namespace=$1", self._namespace,
                )
                return transfer.epoch + 1

    async def authorized(self, candidate: str) -> bool:
        instance_id(candidate)
        async with asyncio.timeout(3), self._pool.acquire(timeout=2) as connection:
            return bool(await connection.fetchval(
                "SELECT 1 FROM opensips_ownership.authority a "
                "WHERE namespace=$1 AND owner_instance=$2 AND phase='active' "
                "AND NOT EXISTS (SELECT 1 FROM opensips_ownership.retired_instances r "
                "WHERE r.instance_id=$2)",
                self._namespace, candidate,
            ))


async def transfer_ownership(
    store: OwnershipStore, fencer: InstanceFencer, *, candidate: str,
    expected_epoch: int, operation_id: UUID,
) -> int:
    transfer = await store.begin(candidate, expected_epoch, operation_id)
    if transfer.completed:
        return transfer.epoch + 1
    # begin() has committed and returned its connection. Never fence inside SQL.
    if transfer.previous is not None:
        async with asyncio.timeout(120):
            await fencer.fence(transfer.previous)
    return await store.complete(transfer)
