"""Sage-owned Compose E2E adapter for the native OpenSIPS policy.

Only the static fixture's missing directory metadata and cloud secret lookup are
supplied here. Eligibility, gateway load, PostgreSQL reservations and SIP routing
use their real services. This file is absent from native Packer installations.
"""

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import runpy
import signal
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from gateway_load_polling import JsonReply, ObservationUnavailable, _response_header
from placement_observer import PlacementObserver
from placement_service import PlacementServer
from placement_store import PlacementStore


async def plain_get(origin, path, token, maximum_bytes):
    """Explicit development HTTP on an isolated Compose network, never production fallback."""
    try:
        return await _plain_get(origin, path, token, maximum_bytes)
    except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise ObservationUnavailable("test observation transport unavailable") from None


async def _plain_get(origin, path, token, maximum_bytes):
    target = urlsplit(origin)
    start = asyncio.get_running_loop().time()
    async with asyncio.timeout(2):
        reader, writer = await asyncio.open_connection(target.hostname, target.port or 80, limit=8192)
        try:
            writer.write((f"GET {path} HTTP/1.1\r\nHost: {target.netloc}\r\nAuthorization: Bearer {token}\r\n"
                          "Connection: close\r\n\r\n").encode())
            await writer.drain()
            status, length = _response_header(await reader.readuntil(b"\r\n\r\n"), maximum_bytes)
            body = await reader.readexactly(length)
            return JsonReply(status, body, start, asyncio.get_running_loop().time())
        finally:
            writer.close()
            writer.transport.abort()


class FixtureHttp:
    async def get(self, origin, path, token, *, timeout, maximum_bytes):
        del origin, timeout
        if path.startswith("/internal/v1/node-placement"):
            reply = await plain_get("http://sage-a:8080", path, token, maximum_bytes)
            if reply.status != 200:
                return reply
            if os.environ.get("SAGE_NATIVE_DIRECTORY_TEST") == "1":
                return reply
            page = json.loads(reply.body)
            for node in page["nodes"]:
                if node["node_id"] != "fs-001" or node["routing"] is not None:
                    raise ValueError("native edge fixture requires the exact static test node")
                node["routing"] = {
                    "incarnation_id": "native-edge-fixture-1",
                    "siprec_target": {"address": os.environ["NATIVE_FS_IP"], "port": 5070, "transport": "udp"},
                    "voice_target": {"address": os.environ["NATIVE_FS_IP"], "port": 5072, "transport": "udp"},
                    "gateway_load_origin": "https://gateway-1:8083",
                    "load_secret": {"arn": "arn:aws:secretsmanager:us-east-2:123456789012:secret:load/fixture", "version_id": "a"*32},
                    "maximum_physical_channels": 100, "route_capabilities": ["siprec", "voice"],
                }
            return JsonReply(200, json.dumps(page).encode(), reply.request_started, reply.completed_at)
        if path != "/v1/node-load":
            raise ValueError("unsupported fixture read")
        return await plain_get("http://gateway-1:8083", path, token, maximum_bytes)


async def main():
    if os.environ.get("SAGE_NATIVE_EDGE_TEST") != "1":
        raise RuntimeError("native edge adapter is test-only")
    admin_url = os.environ["NATIVE_DATABASE_URL"]
    admin = await asyncpg.connect(admin_url, command_timeout=5)
    try:
        if not await admin.fetchval("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname='native_edge')"):
            await admin.execute("CREATE DATABASE native_edge")
    finally:
        await admin.close()
    parsed = urlsplit(admin_url)
    database_url = urlunsplit(parsed._replace(path="/native_edge"))
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4, command_timeout=3)
    edge_ip, media_ip = os.environ["NATIVE_EDGE_IP"], os.environ["NATIVE_MEDIA_IP"]
    async with pool.acquire() as connection:
        async with connection.transaction():
            if await connection.fetchval("SELECT to_regclass('public.version')") is None:
                for name in ("standard", "clusterer", "dialog", "b2b"):
                    await connection.execute(Path(f"/source/scripts/postgres/{name}-create.sql").read_text())
                await connection.execute("""INSERT INTO clusterer(cluster_id,node_id,url,state,no_ping_retries,priority,sip_addr,flags,description)
                    VALUES(10,1,$1,1,3,50,$2,'seed','native test edge')""",
                                         f"bin:{media_ip}:5566", f"sip:{media_ip}:5060")
            await connection.execute(Path("/tests/placement-schema.sql").read_text())
    store = PlacementStore(pool, "native-edge-test")
    await store.initialize()
    server = PlacementServer(store, "1"*64)
    await server.start()

    async def secret(_node):
        if os.environ.get("SAGE_NATIVE_DIRECTORY_TEST") == "1" and _node.load_secret_version == "b"*32:
            return os.environ["SAGE_NATIVE_TEST_LOAD_TOKEN_V2"]
        return os.environ["SAGE_GATEWAY_LOAD_TEST_TOKEN"]

    observer = PlacementObserver(store, FixtureHttp(), FixtureHttp(), "https://sage.fixture.invalid",
                                 os.environ["SAGE_NODE_PLACEMENT_TOKEN"],
                                 "arn:aws:secretsmanager:us-east-2:123456789012:secret:load/",
                                 "native-edge-test", secret)
    observer_task = asyncio.create_task(observer.run())
    render = runpy.run_path("/tests/opensips-runtime-config.py")["render_config"]
    deployment = {
        "node_id": 1, "cluster_id": 10, "private_ip": media_ip, "advertised_ip": media_ip,
        "state_owner": "active", "database_url": urlunsplit(urlsplit(database_url)._replace(scheme="postgres")),
        "voice_ingress_namespace": "development",
        "carrier_udp_ips": [os.environ["NATIVE_SOURCE_EDGE_IP"], os.environ["NATIVE_SOURCE_MEDIA_IP"], media_ip],
        "carrier_tls_ips": [os.environ["NATIVE_SOURCE_EDGE_IP"]],
        "rtpengine_nodes": [{"url": "udp:"+os.environ["NATIVE_RTP_IP"]+":2223", "weight": 1}],
        "placement": {
            "namespace": "native-edge-test", "database_url": database_url, "service_token": "1"*64,
            "sage_origin": "https://sage.fixture.invalid", "sage_token": os.environ["SAGE_NODE_PLACEMENT_TOKEN"],
            "load_secret_prefix": "arn:aws:secretsmanager:us-east-2:123456789012:secret:load/",
            "inventory_networks": ["10.0.0.0/8"], "inventory_ports": [443],
            "gateway_networks": ["10.0.0.0/8"], "gateway_ports": [8083],
            "sip_networks": ["10.0.0.0/8"], "sip_ports": [5070,5072], "ca_bundle": None, "database_ca_bundle": None,
        },
    }
    template = Path("/tests/opensips.cfg.template").read_text()
    if os.environ.get("SAGE_STOCK_PROXY_TEST") == "1":
        placement = template.split("# BEGIN POSTGRES PLACEMENT ROUTES\n", 1)[1].split(
            "# END POSTGRES PLACEMENT ROUTES", 1)[0]
        template = Path("/tests/proxy-proof.cfg.template").read_text().replace(
            "@@PLACEMENT_ROUTES@@", placement)
        replacement_ip = ipaddress.IPv4Address(os.environ["NATIVE_REPLACEMENT_RTP_IP"])
        template += '\nmodparam("rtpengine", "rtpengine_sock", "2 == udp:' + str(replacement_ip) + ':2223")\n'
    config = render(deployment, template)
    # This fixture is UDP-only and does not consume or expose development PKI.
    config = "\n".join(line for line in config.splitlines() if not line.startswith((
        "socket=tls:", 'loadmodule "proto_tls.so"', 'loadmodule "tls_mgm.so"',
        'loadmodule "tls_openssl.so"', 'modparam("tls_mgm",',
    )))
    config = config.replace('mpath="/usr/lib/aarch64-linux-gnu/opensips/modules/"', 'mpath="/modules/"')
    config = config.replace("stderror_enabled=no", "stderror_enabled=yes").replace("syslog_enabled=yes", "syslog_enabled=no")
    config += f"\nsocket=udp:{edge_ip}:5060 as {media_ip}:5060\n"
    path = Path("/tmp/native-edge.cfg")
    path.write_text(config)
    path.chmod(0o600)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    with Path("/tmp/native-edge.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec("opensips", "-F", "-f", str(path), stdout=log, stderr=log)
    stopped = asyncio.create_task(stop.wait())
    exited = asyncio.create_task(process.wait())
    try:
        done, _ = await asyncio.wait((stopped, exited, observer_task), return_when=asyncio.FIRST_COMPLETED)
        if stopped not in done:
            raise RuntimeError("native edge stopped; private evidence retained in /tmp/native-edge.log")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (stopped, exited, observer_task):
            task.cancel()
        await asyncio.gather(stopped, exited, observer_task, return_exceptions=True)
        await server.stop()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
