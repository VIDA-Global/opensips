"""Real PostgreSQL, a severed SQL transport, and stock OpenSIPS process fencing."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
from uuid import uuid4

import asyncpg

from ownership import OwnershipConflict, OwnershipStore, transfer_ownership
from ownership_fencing import FencingUncertain

FIRST = "i-" + "1" * 17
SECOND = "i-" + "2" * 17
DSN = "postgresql://ownership:ownership-proof-only@postgres/ownership"


class SqlLink:
    def __init__(self) -> None:
        self.writers: set[asyncio.StreamWriter] = set()

    async def forward(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        try:
            while data := await reader.read(4096):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            self.writers.discard(writer)

    async def connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        remote_reader, remote_writer = await asyncio.open_connection("postgres", 5432)
        with suppress(ConnectionError):
            await asyncio.gather(self.forward(reader, remote_writer), self.forward(remote_reader, writer))

    async def partition(self, server: asyncio.Server) -> None:
        server.close()
        for writer in tuple(self.writers):
            writer.close()
        await asyncio.wait_for(server.wait_closed(), 3)


class MediaControl(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.deletes = 0
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        cookie, separator, body = data.partition(b" ")
        if not separator or len(data) > 65507:
            return
        if b"7:command6:delete" in body:
            self.deletes += 1
        assert self.transport is not None
        result = b"4:pong" if b"7:command4:ping" in body else b"2:ok"
        self.transport.sendto(cookie + b" d6:result" + result + b"e", address)


class ProcessFencer:
    def __init__(self, process: asyncio.subprocess.Process, *, available: bool) -> None:
        self.process = process
        self.available = available

    async def fence(self, target: str) -> None:
        assert target == FIRST
        if not self.available:
            raise FencingUncertain("independent fence provider unavailable")
        if self.process.returncode is None:
            os.killpg(self.process.pid, signal.SIGKILL)
        await asyncio.wait_for(self.process.wait(), 5)
        async with asyncio.timeout(5):
            while self._live_group():
                await asyncio.sleep(0.01)

    def _live_group(self) -> bool:
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rpartition(") ")[2].split()
            except FileNotFoundError:
                continue
            if len(fields) >= 3 and int(fields[2]) == self.process.pid and fields[0] not in {"Z", "X"}:
                return True
        return False


async def start_owner(store: OwnershipStore, identity: str, port: int, root: Path) -> asyncio.subprocess.Process:
    assert await store.authorized(identity), "unauthorized owner must not execute OpenSIPS"
    config = root / f"{identity}.cfg"
    config.write_text(f'''log_level=1
stderror_enabled=yes
syslog_enabled=no
socket=udp:127.0.0.1:{port}
mpath="/modules/"
loadmodule "proto_udp.so"
loadmodule "signaling.so"
loadmodule "sl.so"
loadmodule "tm.so"
loadmodule "sipmsgops.so"
loadmodule "rtpengine.so"
modparam("rtpengine", "rtpengine_sock", "udp:127.0.0.1:2223")
route {{
    if (is_method("INVITE")) {{
        rtpengine_delete("call-id=ownership-proof from-tag=source");
        sl_send_reply(486, "Ownership Probe");
        exit;
    }}
    sl_send_reply(200, "OK");
}}
''')
    return await asyncio.create_subprocess_exec(
        "opensips", "-F", "-f", str(config), start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )


async def sip(port: int, method: str = "OPTIONS") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.setblocking(False)
        peer.bind(("127.0.0.1", 0))
        local_port = peer.getsockname()[1]
        packet = (
            f"{method} sip:proof@127.0.0.1:{port} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP 127.0.0.1:{local_port};branch=z9hG4bK{uuid4().hex}\r\n"
            f"From: <sip:source@example.test>;tag=source\r\nTo: <sip:proof@example.test>\r\n"
            f"Call-ID: {uuid4().hex}\r\nCSeq: 1 {method}\r\nMax-Forwards: 16\r\n"
            f"Contact: <sip:source@127.0.0.1:{local_port}>\r\nContent-Length: 0\r\n\r\n"
        ).encode()
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(peer, packet, ("127.0.0.1", port))
        try:
            response = await asyncio.wait_for(loop.sock_recv(peer, 4096), 0.2)
        except TimeoutError:
            return False
        return response.startswith(b"SIP/2.0 " + (b"486" if method == "INVITE" else b"200"))


async def ready(port: int, process: asyncio.subprocess.Process) -> None:
    async with asyncio.timeout(10):
        while not await sip(port):
            if process.returncode is not None:
                assert process.stderr is not None
                detail = await process.stderr.read(4096)
                raise AssertionError("synthetic OpenSIPS startup failed: " + detail.decode())
            await asyncio.sleep(0.05)


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=1, command_timeout=2)
    source_pool = None
    processes: list[asyncio.subprocess.Process] = []
    link = SqlLink()
    server = await asyncio.start_server(link.connect, "127.0.0.1", 15432)
    transport, media = await asyncio.get_running_loop().create_datagram_endpoint(
        MediaControl, local_addr=("127.0.0.1", 2223),
    )
    try:
        async with pool.acquire() as connection:
            await connection.execute(Path("/tests/ownership-schema.sql").read_text())
            await connection.execute("INSERT INTO opensips_ownership.authority(namespace) VALUES('proof')")
        store = OwnershipStore(pool, "proof")
        class NoPreviousOwner:
            async def fence(self, target: str) -> None:
                raise AssertionError("bootstrap must not fence an arbitrary instance")
        assert await transfer_ownership(store, NoPreviousOwner(), candidate=FIRST,
                                        expected_epoch=0, operation_id=uuid4()) == 1
        source_pool = await asyncpg.create_pool(
            DSN.replace("postgres/ownership", "127.0.0.1:15432/ownership"),
            min_size=1, max_size=1, timeout=1, command_timeout=2,
        )
        source_store = OwnershipStore(source_pool, "proof")
        with tempfile.TemporaryDirectory() as directory:
            first = await start_owner(source_store, FIRST, 15060, Path(directory))
            processes.append(first)
            await ready(15060, first)
            assert await sip(15060, "INVITE")
            before = media.deletes
            assert before > 0
            await link.partition(server)
            try:
                await source_store.authorized(FIRST)
            except (OSError, asyncpg.PostgresError, TimeoutError):
                pass
            else:
                raise AssertionError("source SQL transport was not partitioned")
            assert first.returncode is None
            assert await sip(15060, "INVITE")
            assert media.deletes > before
            operation = uuid4()
            try:
                await transfer_ownership(store, ProcessFencer(first, available=False),
                                         candidate=SECOND, expected_epoch=1, operation_id=operation)
            except FencingUncertain:
                pass
            else:
                raise AssertionError("uncertain fence promoted a replacement")
            assert not await store.authorized(SECOND)
            assert first.returncode is None
            assert await sip(15060, "INVITE")
            # A fresh controller replays the durable operation. A size-one SQL pool
            # proves the external fencer is not called while a transaction is held.
            class CheckedFencer(ProcessFencer):
                async def fence(self, target: str) -> None:
                    assert not await store.authorized(SECOND)
                    await super().fence(target)
            assert await transfer_ownership(OwnershipStore(pool, "proof"), CheckedFencer(first, available=True),
                                            candidate=SECOND, expected_epoch=1, operation_id=operation) == 2
            assert first.returncode is not None
            assert await transfer_ownership(
                store, ProcessFencer(first, available=False), candidate=SECOND,
                expected_epoch=1, operation_id=operation,
            ) == 2
            after_fence = media.deletes
            assert not await sip(15060, "INVITE")
            assert media.deletes == after_fence
            second = await start_owner(store, SECOND, 15070, Path(directory))
            processes.append(second)
            await ready(15070, second)
            assert await sip(15070, "INVITE")
            assert media.deletes > after_fence
            assert not await store.authorized(FIRST)
            try:
                await store.begin(FIRST, 2, uuid4())
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("retired instance regained ownership")
            async with pool.acquire() as connection:
                await connection.execute(
                    "INSERT INTO opensips_ownership.authority(namespace) VALUES('other'),('race')"
                )
            try:
                await OwnershipStore(pool, "other").begin(SECOND, 0, uuid4())
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("instance acquired another namespace")
            contender_pool = await asyncpg.create_pool(DSN, min_size=1, max_size=1, command_timeout=2)
            try:
                competing = await asyncio.gather(
                    OwnershipStore(pool, "race").begin("i-" + "3" * 17, 0, uuid4()),
                    OwnershipStore(contender_pool, "race").begin("i-" + "4" * 17, 0, uuid4()),
                    return_exceptions=True,
                )
                assert sum(isinstance(value, OwnershipConflict) for value in competing) == 1
                assert sum(not isinstance(value, BaseException) for value in competing) == 1
            finally:
                contender_pool.terminate()
            async with pool.acquire() as connection:
                await connection.execute(
                    "CREATE ROLE ownership_reader LOGIN PASSWORD 'ownership-reader-proof-only'; "
                    "GRANT USAGE ON SCHEMA opensips_ownership TO ownership_reader; "
                    "GRANT SELECT ON opensips_ownership.authority, "
                    "opensips_ownership.retired_instances TO ownership_reader"
                )
            reader_pool = await asyncpg.create_pool(
                "postgresql://ownership_reader:ownership-reader-proof-only@postgres/ownership",
                min_size=1, max_size=1, command_timeout=2,
            )
            try:
                reader = OwnershipStore(reader_pool, "proof")
                assert await reader.authorized(SECOND)
                try:
                    await reader.begin("i-" + "5" * 17, 2, uuid4())
                except asyncpg.InsufficientPrivilegeError:
                    pass
                else:
                    raise AssertionError("runtime reader could mutate ownership")
            finally:
                reader_pool.terminate()
            print(json.dumps({"partitioned_owner_alive_before_fence": True,
                              "uncertain_fence_blocks_promotion": True,
                              "post_fence_old_sip_and_media_commands": 0,
                              "replacement_sip_and_media_commands": True,
                              "retired_instance_rejected": True, "epoch": 2,
                              "independent_session_contenders_serialized": True,
                              "runtime_role_cannot_promote": True,
                              "completed_operation_replay_idempotent": True}))
    finally:
        await link.partition(server)
        for process in processes:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        transport.close()
        if source_pool is not None:
            source_pool.terminate()
        pool.terminate()


async def bounded_main() -> None:
    async with asyncio.timeout(60):
        await main()


if __name__ == "__main__":
    asyncio.run(bounded_main())
