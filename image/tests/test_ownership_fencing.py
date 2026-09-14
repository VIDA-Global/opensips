"""Fencing observations must prove cessation, not mere request acceptance."""

import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ownership_fencing import Ec2InstanceFencer, FencingUncertain

INSTANCE = "i-" + "1" * 17


class Ec2:
    def __init__(self, states: list[str], *, account: str = "123456789012") -> None:
        self.states = states
        self.account = account
        self.stops: list[str] = []
        self.observations = 0

    def describe_instances(self, *, InstanceIds: list[str]) -> dict[str, object]:
        self.observations += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"Reservations": [{"OwnerId": self.account, "Instances": [{
            "InstanceId": InstanceIds[0], "State": {"Name": state},
        }]}]}

    def terminate_instances(self, *, InstanceIds: list[str]) -> dict[str, object]:
        self.stops.extend(InstanceIds)
        return {"TerminatingInstances": []}


class FencingTests(unittest.TestCase):
    def fencer(self, client: Ec2) -> Ec2InstanceFencer:
        elapsed = [0.0]

        def sleep(seconds: float) -> None:
            elapsed[0] += seconds

        return Ec2InstanceFencer(
            client, account="123456789012", allowed_instances=frozenset({INSTANCE}),
            clock=lambda: elapsed[0], sleep=sleep,
        )

    def test_waits_for_independent_terminal_observation(self) -> None:
        client = Ec2(["running", "shutting-down", "terminated"])
        asyncio.run(self.fencer(client).fence(INSTANCE))
        self.assertEqual(client.stops, [INSTANCE])
        self.assertEqual(client.observations, 3)

    def test_request_acceptance_never_proves_fencing(self) -> None:
        client = Ec2(["running", "shutting-down"])
        with self.assertRaises(FencingUncertain):
            asyncio.run(self.fencer(client).fence(INSTANCE))
        self.assertEqual(client.stops, [INSTANCE])

    def test_scope_rejection_precedes_destructive_effects(self) -> None:
        client = Ec2(["running"], account="999999999999")
        with self.assertRaises(FencingUncertain):
            asyncio.run(self.fencer(client).fence(INSTANCE))
        self.assertEqual(client.stops, [])
        with self.assertRaises(FencingUncertain):
            asyncio.run(self.fencer(client).fence("i-" + "2" * 17))
        self.assertEqual(client.observations, 1)

    def test_already_terminated_is_idempotent(self) -> None:
        client = Ec2(["terminated"])
        asyncio.run(self.fencer(client).fence(INSTANCE))
        self.assertEqual(client.stops, [])

    def test_stopped_instance_requires_irreversible_fencing(self) -> None:
        client = Ec2(["stopped", "terminated"])
        asyncio.run(self.fencer(client).fence(INSTANCE))
        self.assertEqual(client.stops, [INSTANCE])


if __name__ == "__main__":
    unittest.main()
