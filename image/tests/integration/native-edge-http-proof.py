"""Exercise the test adapter's real truncated HTTP failure boundary."""

import asyncio
from pathlib import Path
import runpy

from gateway_load_polling import ObservationUnavailable


async def main():
    adapter = runpy.run_path(str(Path(__file__).with_name("native-edge-stack.py")))

    async def truncated(reader, writer):
        try:
            async with asyncio.timeout(2):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(truncated, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        try:
            await adapter["plain_get"](f"http://127.0.0.1:{port}", "/v1/node-load", "fixture-token", 4096)
        except ObservationUnavailable:
            pass
        else:
            raise AssertionError("truncated HTTP must be classified as unavailable")
    print("native test HTTP truncation is recoverable observation unavailability")


if __name__ == "__main__":
    asyncio.run(main())
