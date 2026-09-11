"""Independent PostgreSQL sessions verify distributed placement correctness."""

import asyncio
import hashlib
import json
import socket
from dataclasses import replace
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

import asyncpg

from gateway_load_polling import JsonReply, PlacementInventory, RoutingNode
from placement_observer import PlacementObserver
from placement_service import PlacementServer
import placement_secret
from placement_config import validate_sip_destinations
from placement_store import AllocationConflict, AllocationRequest, PlacementStore

DSN = "postgres://placement:placement-proof-only@postgres/placement"
PREFIX = "arn:aws:secretsmanager:us-east-1:123456789012:secret:load/"


def node(name, *, limit=10):
    return RoutingNode(name, "instance-"+name, "gateway-"+name, "fs-"+name, 1, 15000, limit,
                       "https://gateway.example.test", PREFIX+name, "a"*32,
                       "sip:10.1.0.2:5070;transport=udp", "sip:10.1.0.2:5072;transport=udp",
                       frozenset({"siprec", "voice"}))


def request(name, channels=1):
    digest = hashlib.sha256(name.encode()).hexdigest()
    return AllocationRequest(digest, digest, "siprec", channels, 30000)


async def seed(store, nodes, channels):
    await store.initialize()
    authority = await store.claim_polling("owner-a")
    assert authority is not None
    ticket = await store.ticket(authority)
    assert ticket is not None
    assert await store.publish_inventory(ticket, PlacementInventory(tuple(nodes), 0, 0))
    for item in nodes:
        await load(store, authority, item, channels, 1)
    return authority


async def load(store, authority, item, channels, sequence, *, ticket=None):
    ticket = ticket or await store.ticket(authority)
    assert ticket is not None
    assert await store.publish_load(ticket, item, channels=channels, telemetry="telemetry-1", sequence=sequence,
                                    observed_at="2026-09-11T00:00:00+00:00")
    return ticket


async def api(port, path, body, *, token="1"*64, send_body=True):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        encoded = json.dumps(body).encode()
        writer.write(f"POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {len(encoded)}\r\n\r\n".encode())
        if send_body:
            writer.write(encoded)
        await writer.drain()
        async with asyncio.timeout(2):
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(line.split(b":", 1)[1] for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:")))
            payload = await reader.readexactly(length)
        return int(header.split()[1]), json.loads(payload)
    finally:
        writer.close()
        await writer.wait_closed()


async def observer_and_http(pool):
    store = PlacementStore(pool, "http-observer")
    await store.initialize()
    item = node("fs-http", limit=10)
    reference_version = "a"*32
    load_status = 200
    calls = []

    class Http:
        async def get(self, origin, path, token, **bounds):
            assert pool.get_idle_size() == pool.get_size(), "HTTP ran inside a SQL checkout"
            calls.append((origin, path))
            if "node-placement" in path:
                value = {"schema_version": "1.1.0", "next_after": None, "nodes": [{
                    "node_id": item.node_id, "gateway_generation_id": item.gateway_generation_id,
                    "freeswitch_generation_id": item.freeswitch_generation_id, "state_version": 1,
                    "valid_for_ms": 15000, "routing": {
                        "incarnation_id": item.incarnation_id, "gateway_load_origin": item.load_origin,
                        "load_secret": {"arn": item.load_secret_arn, "version_id": reference_version},
                        "maximum_physical_channels": 10, "route_capabilities": ["siprec"],
                        "siprec_target": {"address": "10.1.0.2", "port": 5070, "transport": "udp"},
                        "voice_target": {"address": "10.1.0.2", "port": 5072, "transport": "udp"},
                    }}]}
                status = 200
            else:
                assert origin == item.load_origin and path == "/v1/node-load", "load did not come directly from gateway"
                value = {"schema_version": "1.0.0", "node_id": item.node_id,
                         "gateway_generation_id": item.gateway_generation_id,
                         "freeswitch_generation_id": item.freeswitch_generation_id,
                         "incarnation_id": item.incarnation_id, "telemetry_generation_id": "telemetry-http",
                         "observation_sequence": 1, "observed_at": "2026-09-11T00:00:00+00:00",
                         "sample_age_ms": 0, "physical_active_channels": 3, "esl_ready": True, "valid": True}
                status = load_status
            return JsonReply(status, json.dumps(value).encode(), 100, 100.01)

    resolved = []

    async def secret(target):
        assert pool.get_idle_size() == pool.get_size(), "secret lookup ran inside SQL"
        resolved.append(target.load_secret_version)
        if len(resolved) == 1:
            observer._nodes = tuple(replace(active, valid_for_ms=14900) for active in observer._nodes)
        return "test-load-observer-credential-0001"

    observer = PlacementObserver(store, Http(), Http(), "https://sage.example.test",
                                 "test-inventory-reader-credential-0001", PREFIX, "owner-http", secret,
                                 validate_node=lambda target: validate_sip_destinations(
                                     (target.siprec_target, target.voice_target),
                                     {"sip_networks": ["10.1.0.0/16"], "sip_ports": [5070,5072]}))
    observer._authority = await store.claim_polling("owner-http")
    assert await observer.refresh_inventory()
    await observer.observe(observer._nodes[0])
    await observer.observe(observer._nodes[0])
    assert resolved == ["a"*32], "immutable version was not cached"
    reference_version = "b"*32
    assert await observer.refresh_inventory()
    await observer.observe(observer._nodes[0])
    assert resolved == ["a"*32, "b"*32], "credential rotation was not discovered"
    server = PlacementServer(store, "1"*64)
    port = await server.start(0)
    try:
        assert (await api(port, "/v1/reservations", {}, token="wrong", send_body=False))[0] == 401
        reservation = request("http-replay")
        body = {"allocation_id": reservation.allocation_id, "request_sha256": reservation.request_sha256,
                "product": "siprec", "channels": 1, "ttl_ms": 30000}
        replies = await asyncio.gather(*[api(port, "/v1/reservations", body) for _ in range(8)])
        assert all(status == 200 for status, _ in replies)
        assert sum(value["fresh"] for _, value in replies) == 1
        assert len({json.dumps({key: item for key, item in value.items() if key != "fresh"}, sort_keys=True) for _, value in replies}) == 1
        assert replies[0][1]["destination"] == item.siprec_target
        async with pool.acquire() as connection:
            assert await connection.fetchval("SELECT count(*) FROM opensips_placement.reservations WHERE namespace='http-observer'") == 1
        assert (await api(port, f"/v1/reservations/{reservation.allocation_id}/confirm", {}))[0] == 200
        load_status = 503
        await observer.observe(observer._nodes[0])
        another = {**body, "allocation_id": "f"*64}
        assert (await api(port, "/v1/reservations", another))[0] == 503
        assert (await api(port, f"/v1/reservations/{reservation.allocation_id}/release", {}))[0] == 200
        assert (await api(port, "/v1/reservations", body))[0] == 503
    finally:
        await server.stop()
    print("Verified direct observer publication, SQL-free HTTP/secret scopes, version rotation and real local HTTP idempotency")


async def sip_selection(pool):
    store = PlacementStore(pool, "sip-selection")
    address = socket.gethostbyname(socket.gethostname())
    target = replace(node("fs-sip"), voice_target=f"sip:{address}:15064;transport=udp")
    await seed(store, [target], 0)
    server = PlacementServer(store, "1"*64)
    port = await server.start(0)
    try:
        process = await asyncio.create_subprocess_exec("python3", "/tests/ua-proof.py", "--placement-port", str(port))
        async with asyncio.timeout(25):
            assert await process.wait() == 0, "OpenSIPS asynchronous placement route failed"
        async with pool.acquire() as connection:
            row = await connection.fetchrow("SELECT node_id,channels,product,state FROM opensips_placement.reservations WHERE namespace='sip-selection'")
            assert tuple(row.values()) == ("fs-sip", 1, "voice", "confirmed")
        print("Real OpenSIPS async SIP routing used and confirmed the PostgreSQL reservation destination")
        replay = await asyncio.create_subprocess_exec("python3", "/tests/ua-proof.py", "--placement-port", str(port), "--expect-rejection", "503")
        async with asyncio.timeout(25):
            assert await replay.wait() == 0, "replayed allocation created another SIP dialog"
        async with pool.acquire() as connection:
            assert await connection.fetchval("SELECT count(*) FROM opensips_placement.reservations WHERE namespace='sip-selection'") == 1
        await server.stop()
        unavailable = await asyncio.create_subprocess_exec("python3", "/tests/ua-proof.py", "--placement-port", str(port), "--expect-rejection", "503")
        async with asyncio.timeout(25):
            assert await unavailable.wait() == 0, "placement outage did not reject SIP"
    finally:
        if 'process' in locals() and process.returncode is None:
            process.kill()
            await process.wait()
        await server.stop()


def secret_reader_boundary():
    # Exercise the installed SDK constructor without sending an IMDS or AWS request.
    placement_secret.InstanceMetadataFetcher(timeout=1, num_attempts=1, base_url="http://169.254.169.254/",
                                             env={}, config={"ec2_metadata_v1_disabled": True})
    frozen = SimpleNamespace(access_key="synthetic-key", secret_key="synthetic-secret", token="synthetic-session")
    provider = Mock()
    provider.load.return_value.get_frozen_credentials.return_value = frozen
    client = Mock()
    client.get_secret_value.return_value = {"ARN": PREFIX+"fs-test", "VersionId": "a"*32,
                                           "SecretString": "synthetic-load-token-that-is-long-enough"}
    session = Mock(return_value=SimpleNamespace(client=Mock(return_value=client)))
    with patch.object(placement_secret, "InstanceMetadataProvider", return_value=provider), \
         patch.object(placement_secret.boto3, "Session", session), \
         patch.object(placement_secret.sys, "argv", ["reader", PREFIX+"fs-test", "a"*32, PREFIX]):
        output = io.StringIO()
        with redirect_stdout(output):
            assert placement_secret.main() == 0
        assert output.getvalue() == "synthetic-load-token-that-is-long-enough"
        assert session.call_args.kwargs["aws_access_key_id"] == frozen.access_key
        client.close.assert_called_once()
        client.get_secret_value.return_value["VersionId"] = "b"*32
        with redirect_stdout(io.StringIO()) as output:
            assert placement_secret.main() == 1
        assert output.getvalue() == ""
        client.get_secret_value.side_effect = RuntimeError("synthetic secret must not escape")
        with redirect_stdout(io.StringIO()) as output:
            assert placement_secret.main() == 1
        assert output.getvalue() == ""
    print("Verified SDK identity-only composition, immutable secret version checks and secret-safe errors without AWS contact")


async def run():
    secret_reader_boundary()
    first = await asyncpg.create_pool(DSN, min_size=1, max_size=4, command_timeout=2)
    second = await asyncpg.create_pool(DSN, min_size=1, max_size=4, command_timeout=2)
    try:
        async with first.acquire() as connection:
            await connection.execute(Path("/tests/placement-schema.sql").read_text())
        a, b = PlacementStore(first, "capacity"), PlacementStore(second, "capacity")
        nodes = [node("fs-a"), node("fs-b")]
        authority = await seed(a, nodes, 8)
        assert await b.claim_polling("owner-b") is None
        requests = [request(f"simultaneous-{index}") for index in range(12)]
        results = await asyncio.gather(*[(a if index % 2 else b).reserve(item) for index, item in enumerate(requests)])
        accepted = [(item, result) for item, result in zip(requests, results) if result is not None]
        assert len(accepted) == 4, "concurrent helpers oversubscribed PostgreSQL pending capacity"
        assert sorted(result["node_id"] for _, result in accepted) == ["fs-a", "fs-a", "fs-b", "fs-b"]
        original, destination = accepted[0]
        replay = await b.reserve(original)
        assert not replay["allocation_fresh"]
        assert {key: value for key, value in replay.items() if key != "allocation_fresh"} == {
            key: value for key, value in destination.items() if key != "allocation_fresh"}
        try:
            await b.reserve(AllocationRequest(original.allocation_id, "a"*64, "voice", 1, 30000))
        except AllocationConflict:
            pass
        else:
            raise AssertionError("conflicting allocation reuse was accepted")
        cancelled = request("cancel-before-allocate")
        await b.release(cancelled.allocation_id)
        assert await a.reserve(cancelled) is None
        await b.release(original.allocation_id)
        assert await a.reserve(original) is None
        async with first.acquire() as connection:
            await connection.execute("UPDATE opensips_placement.reservations SET expires_at=clock_timestamp()-interval '1 second' WHERE namespace='capacity'")
        assert await a.reserve(accepted[1][0]) is None, "expired identity was reallocated"

        stale = await a.ticket(authority)
        newer = await a.ticket(authority)
        assert await a.publish_inventory(newer, PlacementInventory(tuple(nodes), 0, 0))
        assert not await a.publish_inventory(stale, PlacementInventory((), 0, 0))
        async with first.acquire() as connection:
            await connection.execute("UPDATE opensips_placement.control SET poll_until=clock_timestamp()-interval '1 second' WHERE namespace='capacity'")
        replacement = await b.claim_polling("owner-b")
        assert replacement.epoch > authority.epoch
        assert not await a.publish_load(stale, nodes[0], channels=None)
        assert not await a.publish_inventory(stale, PlacementInventory((), 0, 0))
        assert await a.ticket(authority) is None

        c = PlacementStore(first, "confirmation")
        target = node("fs-confirm", limit=3)
        current = await seed(c, [target], 0)
        allocation = request("two-channels", 2)
        assert await c.reserve(allocation) is not None
        before_confirmation = await c.ticket(current)
        assert await c.confirm(allocation.allocation_id)
        await load(c, current, target, 2, 2, ticket=before_confirmation)
        assert await c.reserve(request("too-early")) is None, "confirmation released unobserved pending capacity"
        await load(c, current, target, 2, 3)
        assert await c.reserve(request("observed")) is not None

        d = PlacementStore(first, "freshness")
        target = node("fs-fresh")
        current = await seed(d, [target], 0)
        async with first.acquire() as connection:
            original_expiry = await connection.fetchval("SELECT load_until FROM opensips_placement.nodes WHERE namespace='freshness'")
        await load(d, current, target, 0, 1)
        async with first.acquire() as connection:
            assert await connection.fetchval("SELECT load_until FROM opensips_placement.nodes WHERE namespace='freshness'") == original_expiry
        await d.publish_load(await d.ticket(current), target, channels=None)
        assert await d.reserve(request("unknown-is-not-zero")) is None
        async with first.acquire() as connection:
            await connection.execute("UPDATE opensips_placement.nodes SET load_until=clock_timestamp()-interval '1 second' WHERE namespace='freshness'")
        await load(d, current, target, 0, 1)
        assert await d.reserve(request("repeated-expired-sample")) is None

        await load(d, current, target, 0, 2)
        async with first.acquire() as blocker:
            async with blocker.transaction():
                await d._lock(blocker)
                expiry = await blocker.fetchval("UPDATE opensips_placement.nodes SET load_until=clock_timestamp()+interval '200 milliseconds' WHERE namespace='freshness' RETURNING load_until")
                contender = asyncio.create_task(d.reserve(request("lock-wait")))
                for _ in range(100):
                    async with second.acquire() as observer:
                        blocked = await observer.fetchval("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event='advisory')")
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
                assert blocked
                await blocker.execute("SELECT pg_sleep(0.25)")
                assert await blocker.fetchval("SELECT clock_timestamp()>$1", expiry)
            assert await contender is None, "load freshness was checked before the SQL lock wait"
        print("Verified independent-session capacity, replay/tombstones, observer fencing, confirmation and post-lock freshness")
        generations = PlacementStore(first, "telemetry-history")
        generation_node = node("fs-generation")
        leader = await seed(generations, [generation_node], 0)
        await generations.publish_load(await generations.ticket(leader), generation_node, channels=0,
                                       telemetry="telemetry-new", sequence=1, observed_at="2026-09-11T00:00:01+00:00")
        await load(generations, leader, generation_node, 0, 2)
        assert await generations.reserve(request("retired-telemetry")) is None, "retired telemetry generation regained authority"
        await generations.publish_inventory(await generations.ticket(leader), PlacementInventory((), 0, 0))
        await generations.publish_inventory(await generations.ticket(leader), PlacementInventory((generation_node,), 0, 0))
        await load(generations, leader, generation_node, 0, 3)
        assert await generations.reserve(request("withdrawn-retired-telemetry")) is None
        await generations.publish_load(await generations.ticket(leader), generation_node, channels=0,
                                       telemetry="telemetry-new", sequence=2, observed_at="2026-09-11T00:00:02+00:00")
        assert await generations.reserve(request("current-telemetry")) is not None
        await observer_and_http(first)
        await sip_selection(first)
    finally:
        await first.close()
        await second.close()


asyncio.run(run())
