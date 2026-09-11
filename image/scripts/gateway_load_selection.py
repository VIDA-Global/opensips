"""Single-owner placement policy for independently polled Sage/gateway projections.

This policy has no ESL, HTTP, admission or global reservation authority. Its caller
must authenticate bounded HTTPS responses and serialize ownership of this object.
All time arguments are from the caller's monotonic clock, including request start.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Target:
    node_id: str
    gateway_generation_id: str
    freeswitch_generation_id: str
    valid_for_ms: int
    channel_limit: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", self.node_id):
            raise ValueError("invalid node identity")
        for generation in (self.gateway_generation_id, self.freeswitch_generation_id):
            if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", generation):
                raise ValueError("invalid generation")
        _integer(self.valid_for_ms, 1, 15_000)
        _integer(self.channel_limit, 1, 10_000)

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.node_id, self.gateway_generation_id, self.freeswitch_generation_id


@dataclass(frozen=True)
class _Sample:
    incarnation: str
    telemetry: str
    sequence: int
    observed_at: str
    channels: int
    expires: float


@dataclass
class _Node:
    target: Target
    expires: float
    sample: _Sample | None = None
    last_request: float = -math.inf
    load_valid: bool = False


@dataclass
class _Reservation:
    identity: tuple[str, str, str]
    channels: int
    expires: float
    released: bool = False


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid bounded integer")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _text(value: object, maximum: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError("invalid bounded text")
    return value


class Selector:
    """Bounded per-owner pending allocations, never cluster-wide capacity claims."""

    def __init__(self, *, maximum_reservations: int = 4096) -> None:
        self._maximum_reservations = _integer(maximum_reservations, 1, 100_000)
        self._nodes: dict[str, _Node] = {}
        self._reservations: dict[str, _Reservation] = {}
        self._inventory_request = -math.inf

    def replace_inventory(
        self, targets: tuple[Target, ...], *, request_started: float, now: float
    ) -> None:
        """Install a complete validated inventory; HTTP/page time consumes its lease."""
        if not math.isfinite(now) or not math.isfinite(request_started) or now < request_started:
            raise ValueError("invalid monotonic time")
        if request_started <= self._inventory_request:
            return
        if len(targets) > 256 or len({target.node_id for target in targets}) != len(targets):
            raise ValueError("invalid inventory size or duplicate identity")
        replacement: dict[str, _Node] = {}
        for target in targets:
            expires = request_started + target.valid_for_ms / 1000
            if expires <= now:
                continue
            previous = self._nodes.get(target.node_id)
            node = previous if previous and previous.target.identity == target.identity else _Node(target, expires)
            node.target, node.expires = target, expires
            replacement[target.node_id] = node
        self._nodes = replacement
        self._inventory_request = request_started

    def observe_load(
        self, node_id: str, *, status: int, body: bytes, request_started: float, now: float
    ) -> bool:
        """Fail closed on unavailable, malformed, stale or conflicting load replies."""
        if not math.isfinite(now) or not math.isfinite(request_started) or now < request_started:
            raise ValueError("invalid monotonic time")
        node = self._nodes.get(node_id)
        if node is None or request_started <= node.last_request:
            return False
        node.last_request, node.load_valid = request_started, False
        if status != 200 or len(body) > 4096 or now - request_started >= 2 or node.expires <= now:
            return False
        try:
            value: object = json.loads(body, object_pairs_hook=_unique_object)
            if not isinstance(value, dict) or set(value) != {
                "schema_version", "node_id", "incarnation_id", "gateway_generation_id",
                "freeswitch_generation_id", "telemetry_generation_id", "observation_sequence",
                "observed_at", "sample_age_ms", "physical_active_channels", "esl_ready", "valid",
            }:
                return False
            if value["schema_version"] != "1.0.0" or value["valid"] is not True or value["esl_ready"] is not True:
                return False
            if (value["node_id"], value["gateway_generation_id"], value["freeswitch_generation_id"]) != node.target.identity:
                return False
            age = _integer(value["sample_age_ms"], 0, 2999)
            sample = _Sample(
                incarnation=_text(value["incarnation_id"]), telemetry=_text(value["telemetry_generation_id"]),
                sequence=_integer(value["observation_sequence"], 1, 2**63 - 1),
                observed_at=_text(value["observed_at"], 64),
                channels=_integer(value["physical_active_channels"], 0, 10_000),
                expires=request_started + (3000 - age) / 1000,
            )
            if datetime.fromisoformat(sample.observed_at).utcoffset() is None or sample.expires <= now:
                return False
            previous = node.sample
            if previous is not None:
                if previous.incarnation != sample.incarnation:
                    return False
                if previous.telemetry == sample.telemetry:
                    if sample.sequence < previous.sequence:
                        return False
                    if sample.sequence == previous.sequence:
                        if (sample.channels, sample.observed_at) != (previous.channels, previous.observed_at):
                            return False
                        sample = _Sample(sample.incarnation, sample.telemetry, sample.sequence,
                                         sample.observed_at, sample.channels, min(sample.expires, previous.expires))
            node.sample, node.load_valid = sample, sample.expires > now
            return node.load_valid
        except (ValueError, TypeError, RecursionError):
            return False

    def reserve(
        self, allocation_id: str, *, channels: int, deadline: float, now: float
    ) -> tuple[str, str, str] | None:
        """Select physical load plus local pending allocations with stable retry identity."""
        _text(allocation_id)
        _integer(channels, 1, 10_000)
        if not math.isfinite(now) or not math.isfinite(deadline) or not now < deadline <= now + 30:
            raise ValueError("invalid reservation deadline")
        self._reservations = {key: value for key, value in self._reservations.items() if value.expires > now}
        previous = self._reservations.get(allocation_id)
        if previous is not None:
            if previous.channels != channels:
                raise ValueError("allocation identity reused with different cost")
            return None if previous.released else previous.identity
        if len(self._reservations) >= self._maximum_reservations:
            return None
        pending_by_identity: dict[tuple[str, str, str], int] = {}
        for reservation in self._reservations.values():
            if not reservation.released:
                pending_by_identity[reservation.identity] = pending_by_identity.get(reservation.identity, 0) + reservation.channels
        candidates: list[tuple[float, bytes, _Node]] = []
        for node in self._nodes.values():
            sample = node.sample
            if not node.load_valid or sample is None or min(node.expires, sample.expires) <= now:
                continue
            pending = pending_by_identity.get(node.target.identity, 0)
            load = sample.channels + pending
            if load + channels <= node.target.channel_limit:
                tie = hashlib.sha256((allocation_id + "\0" + node.target.node_id).encode()).digest()
                candidates.append((load / node.target.channel_limit, tie, node))
        if not candidates:
            return None
        selected = min(candidates, key=lambda item: (item[0], item[1]))[2].target.identity
        self._reservations[allocation_id] = _Reservation(selected, channels, deadline)
        return selected

    def release(self, allocation_id: str) -> None:
        """Release pending cost while retaining a terminal retry tombstone until deadline."""
        reservation = self._reservations.get(allocation_id)
        if reservation is not None:
            reservation.released = True
