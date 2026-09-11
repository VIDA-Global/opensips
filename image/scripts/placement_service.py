"""Node-local placement HTTP service; durable decisions and pending capacity live in PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
from hmac import compare_digest
from ipaddress import ip_network
import json
import logging
from pathlib import Path
import re
import signal
import ssl
from uuid import uuid4

import asyncpg

from gateway_load_polling import DirectJsonClient, _unique_object
from placement_observer import PlacementObserver, SecretResolver
from placement_store import AllocationConflict, AllocationRequest, PlacementStore
from placement_config import validate_placement, validate_sip_destinations

logger = logging.getLogger(__name__)


class PlacementServer:
    def __init__(self, store: PlacementStore, token: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise ValueError("placement service credential must be 32 random bytes encoded as hex")
        self._store, self._token = store, token.encode()
        self._tasks: set[asyncio.Task[None]] = set()
        self._server: asyncio.Server | None = None

    async def start(self, port: int = 8095) -> int:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", port, limit=8192, backlog=32)
        return self._server.sockets[0].getsockname()[1]

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._tasks) >= 32:
            writer.close()
            writer.transport.abort()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(4):
                try:
                    status, response = await self._request(reader)
                except AllocationConflict:
                    status, response = 409, {"error": "allocation_conflict"}
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, asyncio.LimitOverrunError):
                    status, response = 422, {"error": "invalid_request"}
                except (asyncpg.PostgresError, OSError, TimeoutError):
                    status, response = 503, {"error": "placement_unavailable"}
                body = json.dumps(response, separators=(",", ":")).encode()
                writer.write(f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".encode()+body)
                await writer.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            writer.transport.abort()

    async def _request(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, object]]:
        header = await reader.readuntil(b"\r\n\r\n")
        if len(header) > 8192:
            raise ValueError("request header too large")
        lines = header[:-4].split(b"\r\n")
        request_line = re.fullmatch(rb"(GET|POST) ([/a-z0-9-]+) HTTP/1\.[01]", lines[0])
        if request_line is None:
            raise ValueError("unsupported request line")
        fields: dict[bytes, bytes] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            name = name.lower()
            if not separator or name in fields or not re.fullmatch(rb"[a-z0-9-]+", name):
                raise ValueError("ambiguous request header")
            fields[name] = value.strip(b" \t")
        if not compare_digest(fields.get(b"authorization", b""), b"Bearer "+self._token):
            return 401, {"error": "unauthorized"}
        method, path = request_line[1].decode(), request_line[2].decode()
        if method == "GET" and path == "/readyz":
            await self._store.initialize()
            return 200, {"ready": True}
        if method != "POST":
            return 404, {"error": "not_found"}
        length = fields.get(b"content-length", b"")
        if b"transfer-encoding" in fields or not re.fullmatch(rb"[0-9]{1,4}", length) or int(length) > 2048:
            raise ValueError("invalid body framing")
        if fields.get(b"content-type", b"").partition(b";")[0] != b"application/json":
            raise ValueError("JSON required")
        body = json.loads(await reader.readexactly(int(length)), object_pairs_hook=_unique_object)
        if path == "/v1/reservations":
            if not isinstance(body, dict) or set(body) != {"allocation_id", "request_sha256", "product", "channels", "ttl_ms"}:
                raise ValueError("invalid reservation")
            request = AllocationRequest(**body)
            result = await self._store.reserve(request)
            if result is None:
                return 503, {"error": "no_capacity_or_expired_allocation"}
            return 200, {"schema_version": "1.0.0", "allocation_id": request.allocation_id,
                         "fresh": result["allocation_fresh"],
                         "node_id": result["node_id"], "incarnation_id": result["incarnation_id"],
                         "gateway_generation_id": result["gateway_generation_id"],
                         "freeswitch_generation_id": result["freeswitch_generation_id"],
                         "destination": result[request.product+"_target"]}
        match = re.fullmatch(r"/v1/reservations/([a-f0-9]{64})/(confirm|release)", path)
        if match is None or body != {}:
            return 404, {"error": "not_found"}
        if match[2] == "release":
            await self._store.release(match[1])
        elif not await self._store.confirm(match[1]):
            return 409, {"error": "allocation_not_pending"}
        return 200, {"accepted": True}

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        pending = tuple(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def run(config_path: Path) -> None:
    with config_path.open("rb") as stream:
        encoded = stream.read(16385)
    if len(encoded) > 16384:
        raise ValueError("placement configuration too large")
    config = validate_placement(json.loads(encoded, object_pairs_hook=_unique_object))
    database_trust = ssl.create_default_context(cafile=config["database_ca_bundle"] or None)
    trust = ssl.create_default_context(cafile=config["ca_bundle"] or None)
    inventory_http = DirectJsonClient(tuple(ip_network(value) for value in config["inventory_networks"]),
                                     frozenset(config["inventory_ports"]), trust=trust, concurrency=1)
    load_http = DirectJsonClient(tuple(ip_network(value) for value in config["gateway_networks"]),
                                frozenset(config["gateway_ports"]), trust=trust)
    pool = await asyncpg.create_pool(config["database_url"], min_size=1, max_size=4, timeout=3, command_timeout=2,
                                    ssl=database_trust, server_settings={"statement_timeout": "2000", "lock_timeout": "1500"})
    server = None
    observer_task = None
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    try:
        store = PlacementStore(pool, config["namespace"])
        await store.initialize()
        server = PlacementServer(store, config["service_token"])
        await server.start()
        observer = PlacementObserver(store, inventory_http, load_http, config["sage_origin"], config["sage_token"],
                                     config["load_secret_prefix"], str(uuid4()), SecretResolver(config["load_secret_prefix"]).resolve,
                                     validate_node=lambda node: validate_sip_destinations((node.siprec_target, node.voice_target), config))
        observer_task = asyncio.create_task(observer.run())
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait((observer_task, stopped), return_when=asyncio.FIRST_COMPLETED)
            if observer_task in done:
                await observer_task
                raise RuntimeError("placement observer stopped unexpectedly")
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
    finally:
        if observer_task is not None:
            observer_task.cancel()
            await asyncio.gather(observer_task, return_exceptions=True)
        if server is not None:
            await server.stop()
        try:
            async with asyncio.timeout(3):
                await pool.close()
        except TimeoutError:
            pool.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args.config))
    except Exception:
        logger.error("placement_service_failed")
        raise SystemExit(1) from None
