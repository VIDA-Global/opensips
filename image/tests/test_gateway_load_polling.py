"""Direct gateway reads pin every address and bound all credential-bearing I/O."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from ipaddress import ip_network
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "gateway_load_polling", Path(__file__).resolve().parents[1] / "scripts/gateway_load_polling.py"
)
assert SPEC and SPEC.loader
HTTP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HTTP
SPEC.loader.exec_module(HTTP)
TOKEN = "direct-observation-test-credential-0001"
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
SECRET_PREFIX = "arn:aws:secretsmanager:us-east-1:123456789012:secret:load/"


def placement_node(node_id="fs-001"):
    return {
        "node_id": node_id, "gateway_generation_id": "gateway-1", "freeswitch_generation_id": "fs-1",
        "state_version": 1, "valid_for_ms": 15000,
        "routing": {"incarnation_id": "instance-1", "gateway_load_origin": "https://gateway.example.test",
                    "load_secret": {"arn": SECRET_PREFIX + node_id, "version_id": "a" * 32},
                    "maximum_physical_channels": 100, "route_capabilities": ["siprec", "voice"],
                    "siprec_target": {"address": "10.1.0.2", "port": 5070, "transport": "udp"},
                    "voice_target": {"address": "10.1.0.2", "port": 5072, "transport": "tcp"}},
    }


def page(nodes, after=None, status=200):
    return HTTP.JsonReply(status, json.dumps({"schema_version": "1.1.0", "nodes": nodes, "next_after": after}).encode(), 100, 100.1)


class HeaderTests(unittest.TestCase):
    def test_exact_length_delimited_json_contract(self) -> None:
        self.assertEqual(HTTP._response_header(RESPONSE[:-2], 2), (200, 2))
        for response in (
            RESPONSE.replace(b"Length: 2", b"Length: 3"),
            RESPONSE.replace(b"Length: 2", b"Length: -1"),
            RESPONSE.replace(b"Length: 2", b"Length: 2\r\ncontent-length: 2"),
            RESPONSE.replace(b"Length: 2", b"Length: 2\r\nTransfer-Encoding: chunked"),
            RESPONSE.replace(b"Length: 2", b"Length: 2\r\nContent-Encoding: gzip"),
            RESPONSE.replace(b"application/json", b"text/html"),
            RESPONSE.replace(b"200 OK", b"100 Continue"),
            RESPONSE.replace(b"Content-Type:", b"Bad Header:"),
            RESPONSE.replace(b"Content-Type:", b" Content-Type:"),
            RESPONSE.replace(b"application/json", b"application/json\x00"),
            RESPONSE.replace(b"Content-Length: 2\r\n", b""),
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                HTTP._response_header(response[:-2], 2)

    def test_tls_and_role_scopes_cannot_be_disabled(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        with self.assertRaises(ValueError):
            HTTP.DirectJsonClient((ip_network("10.1.0.0/16"),), frozenset({443}), trust=context)
        for networks, ports, concurrency in (
            ((), {443}, 1), ((ip_network("0.0.0.0/0"),), {443}, 1),
            ((ip_network("10.1.0.0/16"),), set(), 1),
            ((ip_network("10.1.0.0/16"),), {True}, 1),
            ((ip_network("10.1.0.0/16"),), {443}, 33),
        ):
            with self.assertRaises(ValueError):
                HTTP.DirectJsonClient(networks, frozenset(ports), concurrency=concurrency)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = HTTP.DirectJsonClient((ip_network("10.1.0.0/16"),), frozenset({443}), concurrency=1)
        self.reader = asyncio.StreamReader()
        self.reader.feed_data(RESPONSE)
        self.reader.feed_eof()
        self.writer = Mock()
        self.writer.drain = AsyncMock()
        self.writer.wait_closed = AsyncMock()
        self.addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.0.2", 443))]
        self.resolve = patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=self.addresses))
        self.connect = patch.object(HTTP.asyncio, "open_connection", AsyncMock(return_value=(self.reader, self.writer)))
        self.resolve_mock, self.connect_mock = self.resolve.start(), self.connect.start()
        self.addCleanup(self.resolve.stop)
        self.addCleanup(self.connect.stop)

    async def get(self):
        return await self.client.get("https://gateway.example.test", "/v1/node-load", TOKEN,
                                     timeout=2, maximum_bytes=4096)

    async def test_request_pins_connection_preserving_host_sni_and_closure(self) -> None:
        reply = await self.get()
        self.assertEqual((reply.status, reply.body), (200, b"{}"))
        self.assertLessEqual(reply.request_started, reply.completed_at)
        self.assertEqual(self.connect_mock.call_args.args, ("10.1.0.2", 443))
        self.assertEqual(self.connect_mock.call_args.kwargs["server_hostname"], "gateway.example.test")
        request = self.writer.write.call_args.args[0]
        self.assertIn(b"Host: gateway.example.test:443\r\n", request)
        self.assertIn(("Authorization: Bearer " + TOKEN).encode(), request)
        self.writer.wait_closed.assert_awaited_once()
        self.writer.transport.abort.assert_called_once()
        self.assertEqual(self.client._active, 0)

    async def test_every_dns_address_must_be_approved_before_credentials_are_sent(self) -> None:
        for address in ("127.0.0.1", "169.254.169.254", "10.2.0.1", "224.0.0.1", "::1"):
            self.resolve_mock.return_value = [*self.addresses, (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with self.assertRaises(HTTP.ObservationUnavailable):
                await self.get()
        self.connect_mock.assert_not_called()
        self.writer.write.assert_not_called()

    async def test_failure_and_cancellation_release_capacity_and_abort_transport(self) -> None:
        for error in (TimeoutError(TOKEN), OSError(TOKEN), asyncio.CancelledError()):
            self.writer.drain.side_effect = error
            with self.assertRaises((HTTP.ObservationUnavailable, asyncio.CancelledError)) as raised:
                await self.get()
            self.assertNotIn(TOKEN, str(raised.exception))
            self.assertEqual(self.client._active, 0)
        self.assertEqual(self.writer.transport.abort.call_count, 3)

    async def test_concurrency_rejects_without_waiter_queue(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def drain() -> None:
            entered.set()
            await release.wait()

        self.writer.drain.side_effect = drain
        pending = asyncio.create_task(self.get())
        try:
            await entered.wait()
            with self.assertRaisesRegex(HTTP.ObservationUnavailable, "capacity"):
                await self.get()
        finally:
            release.set()
            await pending
        self.assertEqual(self.connect_mock.call_count, 1)

    async def test_cancelled_dns_keeps_its_slot_until_real_resolution_finishes(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def resolve(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.addresses

        self.resolve_mock.side_effect = resolve
        pending = asyncio.create_task(self.get())
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.client._active, 1)
        with self.assertRaisesRegex(HTTP.ObservationUnavailable, "capacity"):
            await self.get()
        release.set()
        await asyncio.gather(*self.client._pending_resolution)
        self.assertEqual(self.client._active, 0)
        self.connect_mock.assert_not_called()

    async def test_bad_origins_paths_tokens_and_bounds_never_reach_dns(self) -> None:
        defaults = dict(origin="https://gateway.example.test", path="/v1/node-load", token=TOKEN,
                        timeout=2, maximum_bytes=4096)
        for changes in (
            {"origin": "http://gateway.example.test"}, {"origin": "https://user@gateway.example.test"},
            {"origin": "https://gateway.example.test/path"}, {"origin": "https://gateway.example.test:22"},
            {"origin": "https://gateway.example.test:" + TOKEN},
            {"path": "//external.example.test"}, {"path": "/v1/node-load\r\n"},
            {"token": TOKEN + "\r\nInjected: yes"}, {"maximum_bytes": True}, {"timeout": float("nan")},
        ):
            with self.assertRaises(ValueError) as raised:
                await self.client.get(**{**defaults, **changes})
            self.assertNotIn(TOKEN, str(raised.exception))
        self.resolve_mock.assert_not_called()


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_page_cursor_advances_and_complete_native_inventory_is_returned(self) -> None:
        client = Mock()
        legacy = {**placement_node("fs-002"), "routing": None}
        client.get = AsyncMock(side_effect=[page([], "fs-000"), page([placement_node(), legacy])])
        inventory = await HTTP.read_inventory(client, "https://sage.example.test", TOKEN, SECRET_PREFIX)
        self.assertEqual([node.node_id for node in inventory.nodes], ["fs-001"])
        node = inventory.nodes[0]
        self.assertEqual(node.siprec_target, "sip:10.1.0.2:5070;transport=udp")
        self.assertEqual(node.capabilities, frozenset({"voice", "siprec"}))
        self.assertLessEqual(inventory.request_started, inventory.completed_at)
        self.assertEqual(client.get.call_args_list[1].args[1], "/internal/v1/node-placement?limit=100&after=fs-000")

    async def test_bad_later_page_never_returns_partial_inventory(self) -> None:
        for second in (
            page([placement_node()]), page([], "fs-001"), page([], status=503),
            HTTP.JsonReply(200, b'{"schema_version":"1.1.0","nodes":[],"nodes":[],"next_after":null}', 100, 101),
        ):
            client = Mock()
            client.get = AsyncMock(side_effect=[page([placement_node()], "fs-001"), second])
            with self.assertRaises(HTTP.ObservationUnavailable):
                await HTTP.read_inventory(client, "https://sage.example.test", TOKEN, SECRET_PREFIX)

    async def test_unbounded_pages_and_foreign_credential_scope_are_rejected(self) -> None:
        client = Mock()
        client.get = AsyncMock(side_effect=[page([], f"fs-{index:03}") for index in range(4)])
        with self.assertRaises(HTTP.ObservationUnavailable):
            await HTTP.read_inventory(client, "https://sage.example.test", TOKEN, SECRET_PREFIX)
        node = placement_node()
        node["routing"]["load_secret"]["arn"] = SECRET_PREFIX.replace("load/", "control/") + "fs-001"
        client.get = AsyncMock(return_value=page([node]))
        with self.assertRaises(HTTP.ObservationUnavailable):
            await HTTP.read_inventory(client, "https://sage.example.test", TOKEN, SECRET_PREFIX)

    def test_native_node_validation_rejects_ambiguous_boundaries(self) -> None:
        for change in (
            {"incarnation_id": "bad\r\n"}, {"maximum_physical_channels": True},
            {"route_capabilities": ["siprec", "siprec"]}, {"load_secret": {}},
            {"spool_origin": "https://private.example.test"},
            {"gateway_load_origin": "https://user@gateway.example.test"},
            {"siprec_target": {"address": "127.0.0.1", "port": 5070, "transport": "udp"}},
            {"voice_target": {"address": "10.1.0.2", "port": True, "transport": "udp"}},
        ):
            node = placement_node()
            node["routing"].update(change)
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                HTTP._routing_node(node, SECRET_PREFIX)


if __name__ == "__main__":
    unittest.main()
