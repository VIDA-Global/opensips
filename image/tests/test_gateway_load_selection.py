"""Deterministic policy tests; these do not assert installed OpenSIPS integration."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

SPEC = importlib.util.spec_from_file_location(
    "gateway_load_selection", Path(__file__).resolve().parents[1] / "scripts/gateway_load_selection.py"
)
assert SPEC and SPEC.loader
POLICY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = POLICY
SPEC.loader.exec_module(POLICY)


def document(**changes: object) -> bytes:
    return json.dumps({
        "schema_version": "1.0.0", "node_id": "fs-i-first", "incarnation_id": "cell-1",
        "gateway_generation_id": "gateway-1", "freeswitch_generation_id": "freeswitch-1",
        "telemetry_generation_id": "telemetry-1", "observation_sequence": 1,
        "observed_at": "2026-09-10T12:00:00+00:00", "sample_age_ms": 100,
        "physical_active_channels": 3, "esl_ready": True, "valid": True, **changes,
    }).encode()


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selector = POLICY.Selector(maximum_reservations=2)
        self.target = POLICY.Target("fs-i-first", "gateway-1", "freeswitch-1", 15000, 10)
        self.selector.replace_inventory((self.target,), request_started=100, now=100)

    def observe(self, body: bytes, *, started: float = 100.1, now: float = 100.2) -> bool:
        return self.selector.observe_load("fs-i-first", status=200, body=body, request_started=started, now=now)

    def reserve(self, key: str, channels: int = 1, now: float = 100.3) -> object:
        return self.selector.reserve(key, channels=channels, deadline=now + 10, now=now)

    def test_pending_cost_retry_identity_release_and_bound(self) -> None:
        self.assertIsNone(self.reserve("unknown"))
        self.assertTrue(self.observe(document()))
        first = self.reserve("first", channels=6)
        self.assertEqual(first, self.target.identity)
        self.assertEqual(self.reserve("first", channels=6), first)
        self.assertIsNone(self.reserve("second", channels=2))
        with self.assertRaises(ValueError):
            self.reserve("first", channels=5)
        self.selector.release("first")
        self.assertIsNone(self.reserve("first", channels=6))
        self.assertEqual(self.reserve("second", channels=2), first)
        self.assertIsNone(self.reserve("bounded"))

    def test_same_observation_cannot_renew_its_original_deadline(self) -> None:
        self.assertTrue(self.observe(document()))
        self.assertTrue(self.observe(document(), started=102, now=102.1))
        self.assertIsNone(self.reserve("expired", now=103.01))

    def test_network_time_and_remote_sample_age_both_consume_freshness(self) -> None:
        self.assertFalse(self.observe(document(sample_age_ms=2900), started=100.1, now=100.3))
        self.assertFalse(self.observe(document(), started=101, now=103))

    def test_malformed_or_unready_samples_never_become_zero_load(self) -> None:
        for changes in (
            {"valid": False}, {"valid": "true"}, {"esl_ready": False},
            {"physical_active_channels": None}, {"physical_active_channels": False},
            {"physical_active_channels": -1}, {"physical_active_channels": 10001},
            {"sample_age_ms": 3000}, {"observation_sequence": 0},
            {"freeswitch_generation_id": "old"}, {"gateway_generation_id": "old"},
            {"node_id": "wrong"}, {"schema_version": "2.0.0"},
            {"observed_at": "2026-09-10T12:00:00"}, {"extra": "unknown"},
        ):
            with self.subTest(changes=changes):
                self.setUp()
                self.assertTrue(self.observe(document()))
                self.assertFalse(self.observe(document(**changes), started=100.4, now=100.5))
                self.assertIsNone(self.reserve("unavailable", now=100.6))
        for body in (b"[", b"[]", b'{"valid":true,"valid":false}', b"x" * 4097):
            self.setUp()
            self.assertFalse(self.observe(body))

    def test_withdrawal_and_generation_change_remove_new_placement(self) -> None:
        self.assertTrue(self.observe(document()))
        self.selector.replace_inventory((), request_started=100.4, now=100.5)
        self.assertIsNone(self.reserve("withdrawn", now=100.6))
        self.selector.replace_inventory((self.target,), request_started=100, now=100.7)
        self.assertIsNone(self.reserve("old-inventory", now=100.8))
        changed = POLICY.Target("fs-i-first", "gateway-1", "freeswitch-2", 15000, 10)
        self.selector.replace_inventory((changed,), request_started=101, now=101.1)
        self.assertFalse(self.observe(document(), started=101.2, now=101.3))
        self.assertTrue(self.observe(document(freeswitch_generation_id="freeswitch-2"), started=101.4, now=101.5))
        self.assertEqual(self.reserve("new", now=101.6), changed.identity)

    def test_late_http_reply_cannot_overwrite_a_newer_observation(self) -> None:
        self.assertTrue(self.observe(document(observation_sequence=2), started=100.2, now=100.3))
        self.assertFalse(self.observe(document(), started=100.1, now=100.4))
        self.assertIsNotNone(self.reserve("latest", now=100.5))
        self.assertFalse(self.observe(document(), started=100.6, now=100.7))
        self.assertIsNone(self.reserve("rollback", now=100.8))

    def test_normalized_load_selects_the_less_loaded_node(self) -> None:
        other = POLICY.Target("fs-i-second", "gateway-1", "freeswitch-1", 15000, 20)
        self.selector.replace_inventory((self.target, other), request_started=100.01, now=100.02)
        self.assertTrue(self.observe(document(physical_active_channels=6)))
        self.assertTrue(self.selector.observe_load(
            other.node_id, status=200, body=document(node_id=other.node_id, physical_active_channels=4),
            request_started=100.1, now=100.2,
        ))
        self.assertEqual(self.reserve("least-loaded"), other.identity)

    def test_failure_and_expired_inventory_exclude_new_work(self) -> None:
        self.assertTrue(self.observe(document()))
        self.assertFalse(self.selector.observe_load(
            self.target.node_id, status=503, body=document(), request_started=100.4, now=100.5,
        ))
        self.assertIsNone(self.reserve("failed", now=100.6))
        self.selector.replace_inventory((self.target,), request_started=101, now=117)
        self.assertFalse(self.observe(document(), started=117.1, now=117.2))

    def test_configuration_and_monotonic_bounds(self) -> None:
        for limit in (0, 10001, True):
            with self.assertRaises(ValueError):
                POLICY.Target("fs-i-first", "gateway-1", "freeswitch-1", 15000, limit)
        with self.assertRaises(ValueError):
            self.selector.replace_inventory((self.target, self.target), request_started=101, now=101)
        with self.assertRaises(ValueError):
            self.selector.replace_inventory((), request_started=float("nan"), now=101)
        with self.assertRaises(ValueError):
            self.selector.reserve("invalid", channels=1, deadline=132, now=100)


if __name__ == "__main__":
    unittest.main()
