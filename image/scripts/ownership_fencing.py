"""Independent irreversible EC2 fencing; no AWS calls at import time."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import re
import time
from typing import Protocol

from ownership import instance_id


class Ec2Control(Protocol):
    def describe_instances(self, *, InstanceIds: list[str]) -> dict[str, object]: ...

    def terminate_instances(self, *, InstanceIds: list[str]) -> dict[str, object]: ...


class FencingUncertain(RuntimeError):
    """The provider did not establish the required physical fence."""


def _single(value: object) -> dict[str, object]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise FencingUncertain("ambiguous EC2 fencing observation")
    return value[0]


class Ec2InstanceFencer:
    """Restrict destructive effects to provisioned instance IDs in one account.

    The caller creates the client in the approved region with bounded botocore
    transport/retries. The IAM role must independently enforce this same scope.
    A successful TerminateInstances request is not a completed fence. Stopped
    instances are insufficient: hibernation or restart must not revive an old owner.
    """

    def __init__(
        self, client: Ec2Control, *, account: str, allowed_instances: frozenset[str],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not re.fullmatch(r"[0-9]{12}", account):
            raise ValueError("invalid fencing account")
        if not allowed_instances or len(allowed_instances) > 64:
            raise ValueError("invalid fencing instance scope")
        for value in allowed_instances:
            instance_id(value)
        self._client = client
        self._account = account
        self._allowed = allowed_instances
        self._clock = clock
        self._sleep = sleep

    def _state(self, target: str) -> str:
        response = self._client.describe_instances(InstanceIds=[target])
        reservation = _single(response.get("Reservations"))
        if reservation.get("OwnerId") != self._account:
            raise FencingUncertain("EC2 fencing observation has the wrong account")
        instance = _single(reservation.get("Instances"))
        if instance.get("InstanceId") != target:
            raise FencingUncertain("EC2 fencing observation has the wrong instance")
        state = instance.get("State")
        if not isinstance(state, dict) or state.get("Name") not in {
            "pending", "running", "stopping", "stopped", "shutting-down", "terminated"
        }:
            raise FencingUncertain("invalid EC2 fencing state")
        return str(state["Name"])

    def _fence(self, target: str) -> None:
        deadline = self._clock() + 110
        state = self._state(target)
        if state == "terminated":
            return
        if state != "shutting-down":
            self._client.terminate_instances(InstanceIds=[target])
        while self._clock() < deadline:
            if self._state(target) == "terminated":
                return
            self._sleep(1)
        raise FencingUncertain("EC2 fencing did not reach a terminated state")

    async def fence(self, target: str) -> None:
        instance_id(target)
        if target not in self._allowed:
            raise FencingUncertain("fencing target is outside the approved scope")
        await asyncio.to_thread(self._fence, target)
