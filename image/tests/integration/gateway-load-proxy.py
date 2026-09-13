"""Test-only co-located HTTP forwarding of the gateway's loopback load endpoint."""

import asyncio


async def handle(reader, writer):
    upstream = None
    try:
        async with asyncio.timeout(2):
            header = await reader.readuntil(b"\r\n\r\n")
            if len(header) > 8192 or not header.startswith(b"GET /v1/node-load HTTP/1."):
                return
            source, upstream = await asyncio.open_connection("127.0.0.1", 8082, limit=8192)
            upstream.write(header)
            await upstream.drain()
            response = await source.readuntil(b"\r\n\r\n")
            length = int(next(line.split(b":", 1)[1] for line in response.split(b"\r\n") if line.lower().startswith(b"content-length:")))
            if not 0 <= length <= 4096:
                return
            writer.write(response + await source.readexactly(length))
            await writer.drain()
    except (OSError, ValueError, StopIteration, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        for stream in (writer, upstream):
            if stream is not None:
                stream.close()
                stream.transport.abort()


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 8083, limit=8192, backlog=16)
    async with server:
        # This fixture owns the shared network namespace so gateway stop/start
        # replaces only the gateway process, preserving the proxy's listener.
        await server.serve_forever()


asyncio.run(main())
