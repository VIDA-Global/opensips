"""Real TLS/stream/deadline probe on an isolated private Docker network."""

import asyncio
from contextlib import suppress
from ipaddress import ip_network
from pathlib import Path
import socket
import ssl
import subprocess
import sys

sys.path.insert(0, "/tests")
from gateway_load_polling import DirectJsonClient, ObservationUnavailable

TOKEN = "load-https-proof-synthetic-credential-0001"


async def run():
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=gateway.example.test", "-addext", "subjectAltName=DNS:gateway.example.test",
        "-keyout", "/tmp/load-test.key", "-out", "/tmp/load-test.crt",
    ], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path("/tmp/load-test.key").chmod(0o600)
    server_trust = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_trust.load_cert_chain("/tmp/load-test.crt", "/tmp/load-test.key")
    client_trust = ssl.create_default_context(cafile="/tmp/load-test.crt")
    address = socket.gethostbyname(socket.gethostname())
    network = ip_network(address + "/32")
    requests, tasks = [], set()
    mode = "normal"

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            assert ("Authorization: Bearer " + TOKEN + "\r\n").encode() in request
            requests.append(request)
            if mode == "stall":
                await asyncio.Event().wait()
            elif mode == "oversize":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 4097\r\n\r\n")
            elif mode == "redirect":
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: https://never-contact.invalid\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            writer.transport.abort()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "0.0.0.0", 0, ssl=server_trust)
    port = server.sockets[0].getsockname()[1]
    client = DirectJsonClient((network,), frozenset({port}), trust=client_trust)
    origin = f"https://gateway.example.test:{port}"
    try:
        result = await client.get(origin, "/v1/node-load", TOKEN, timeout=2, maximum_bytes=4096)
        assert result.status == 200 and result.body == b"{}"
        assert len(requests) == 1
        untrusted = DirectJsonClient((network,), frozenset({port}))
        with_error = False
        try:
            await untrusted.get(origin, "/v1/node-load", TOKEN, timeout=2, maximum_bytes=4096)
        except ObservationUnavailable:
            with_error = True
        assert with_error and len(requests) == 1, "untrusted TLS received credentials"
        mode = "redirect"
        result = await client.get(origin, "/v1/node-load", TOKEN, timeout=2, maximum_bytes=4096)
        assert result.status == 302 and len(requests) == 2, "redirect was followed"
        for mode in ("oversize", "stall"):
            try:
                await client.get(origin, "/v1/node-load", TOKEN, timeout=0.2, maximum_bytes=4096)
            except ObservationUnavailable as error:
                assert TOKEN not in str(error)
            else:
                raise AssertionError("response bound was not enforced")
        assert client._active == 0
        print("Verified private-IP-pinned TLS, trusted CA, no redirects, response bounds and timeout closure")
    finally:
        server.close()
        await server.wait_closed()
        remaining = tuple(tasks)
        for task in remaining:
            task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*remaining, return_exceptions=True)


asyncio.run(run())
